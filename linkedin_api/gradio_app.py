#!/usr/bin/env python3
"""
Gradio MVP UI: run pipeline (collect → enrich → summarize) and query GraphRAG.

Tab 1: Pipeline — period, from-cache, optional limit; run and get report with progress.
Tab 2: GraphRAG query — lazy-init Neo4j and Vertex AI on demand.
"""

import html
import json
import logging
import os
import queue
import re
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

import dotenv

dotenv.load_dotenv()
from typing import TYPE_CHECKING

import gradio as gr
from neo4j import GraphDatabase
from neo4j_graphrag.generation.graphrag import GraphRAG

if TYPE_CHECKING:
    from neo4j import Driver

from linkedin_api.activity_csv import get_data_dir, get_default_csv_path
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.content_store import (
    _ms_to_iso,
    list_summarized_metadata,
    load_content,
)
from linkedin_api.llm_config import (
    create_embedder,
    create_llm,
    get_default_provider_model,
    get_report_model_id,
)
from linkedin_api.llm_models import fetch_all_provider_models, fetch_models_for_provider
from linkedin_api.query_graphrag import (
    NEO4J_DATABASE,
    NEO4J_PASSWORD,
    NEO4J_URI,
    NEO4J_USERNAME,
    create_vector_cypher_retriever,
    create_vector_retriever,
)
from linkedin_api.run_pipeline import run_pipeline_ui_streaming
from linkedin_api.summarize_activity import _parse_last, collect_from_csv
from linkedin_api.utils.linkedin_snowflake import post_created_at_from_urn

_REPORT_SYSTEM = (
    "You are a concise analyst. Summarize the user's LinkedIn activity globally. "
    "Highlight main themes, recurring topics, technologies, and any patterns. "
    "Output a short report in markdown (sections, bullet points). No preamble."
)
REPORT_MAX_POSTS = 50  # fallback when max_posts not set
REPORT_MAX_POSTS_MINIMAL = 100
REPORT_MAX_POSTS_SUMMARY = 50
REPORT_MAX_POSTS_FULL = 20
REPORT_BATCH_CHAR_LIMIT = 4000
REPORT_MAX_FULL_POST_CHARS_DEFAULT = 1500

# Order defines report sections. "other" gets summaries + links only (no LLM).
REPORT_CATEGORIES = (
    "product_announcement",
    "tutorial",
    "opinion",
    "paper",
    "experiment",
    "job_news",
    "other",
)
CATEGORY_LABELS = {
    "product_announcement": "Product announcements",
    "tutorial": "Tutorials & how-to",
    "opinion": "Opinion & hot takes",
    "paper": "Papers & research",
    "experiment": "Experiments & benchmarks",
    "job_news": "Job & career",
    "other": "Other (uncategorized — review to improve categorization)",
}

REPORT_MODE_PER_CATEGORY = "per_category"
REPORT_MODE_SINGLE_PASS = "single_pass"
REPORT_MODE_LABEL_PER_CATEGORY = "Per category summary"
REPORT_MODE_LABEL_SINGLE_PASS = "Single pass (all posts)"
REPORT_MODE_CHOICES = [
    (REPORT_MODE_LABEL_PER_CATEGORY, REPORT_MODE_PER_CATEGORY),
    (REPORT_MODE_LABEL_SINGLE_PASS, REPORT_MODE_SINGLE_PASS),
]

CONTENT_LEVEL_MINIMAL = "minimal"
CONTENT_LEVEL_SUMMARY = "summary"
CONTENT_LEVEL_FULL = "full"
CONTENT_LEVEL_LABEL_MINIMAL = "Minimal (link + tags)"
CONTENT_LEVEL_LABEL_SUMMARY = "Summary (minimal + post summary)"
CONTENT_LEVEL_LABEL_FULL = "Full (minimal + full post content)"
CONTENT_LEVEL_CHOICES = [
    (CONTENT_LEVEL_LABEL_MINIMAL, CONTENT_LEVEL_MINIMAL),
    (CONTENT_LEVEL_LABEL_SUMMARY, CONTENT_LEVEL_SUMMARY),
    (CONTENT_LEVEL_LABEL_FULL, CONTENT_LEVEL_FULL),
]

ReportSignature = tuple[str, int, tuple[str, ...], str, str, int, int, str]


def _default_max_posts(content_level: str) -> int:
    """Default max posts per report by content level."""
    if content_level == CONTENT_LEVEL_MINIMAL:
        return REPORT_MAX_POSTS_MINIMAL
    if content_level == CONTENT_LEVEL_FULL:
        return REPORT_MAX_POSTS_FULL
    return REPORT_MAX_POSTS_SUMMARY


def _truncate(s: str, max_len: int) -> str:
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _get_posts_for_period(
    period: str,
    max_posts: int,
    csv_path: Path | None = None,
) -> tuple[list[dict], str | None]:
    """Posts scoped to period (activity timestamp in range). Sorted by activity time."""
    start_ms = _parse_last(period)
    if start_ms is None:
        return [], None
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    period_dates = (
        f"{datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc):%Y-%m-%d} to "
        f"{datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc):%Y-%m-%d}"
    )

    path = csv_path or get_default_csv_path()
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.now(timezone.utc)
    urn_to_activity: dict[str, EnrichedRecord] = {}
    try:
        for a in collect_from_csv(start=start_dt, end=end_dt, csv_path=path):
            urn = (a.post_urn or "").strip()
            ts_ms = int(a.timestamp) if a.timestamp is not None else None
            if urn and ts_ms is not None and start_ms <= ts_ms <= end_ms:
                urn_to_activity[urn] = a
    except OSError:
        pass

    if not urn_to_activity:
        return [], period_dates

    scoped = [
        m
        for m in list_summarized_metadata()
        if (m.get("urn") or "").strip() in urn_to_activity
    ]
    for m in scoped:
        act = urn_to_activity.get((m.get("urn") or "").strip())
        if act and act.timestamp is not None:
            m["activity_time_iso"] = _ms_to_iso(int(act.timestamp))

    def _activity_ts(meta: dict) -> int:
        act = urn_to_activity.get((meta.get("urn") or "").strip())
        if act and act.timestamp is not None:
            return int(act.timestamp)
        return 0

    scoped.sort(key=_activity_ts)
    return scoped[:max_posts], period_dates


def _format_post_for_prompt(
    m: dict,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
) -> str:
    """Format post for LLM. Always includes link. Minimal=link+tags, Summary=+full summary, Full=+truncated content.
    When activity_time_iso/post_time are present (period-scoped), includes them."""
    url = (m.get("post_url") or "").strip()
    cat = (m.get("category") or "").strip() or "other"
    parts: list[str] = []
    if m.get("topics"):
        parts.append(f"Topics: {', '.join(m['topics'])}")
    if m.get("technologies"):
        parts.append(f"Tech: {', '.join(m['technologies'])}")
    tag_part = " | ".join(parts) if parts else ""
    tag_part = f"Category: {cat}" + (f" | {tag_part}" if tag_part else "")
    # Temporal context when available (enables "late news" assessment)
    activity_time = (m.get("activity_time_iso") or m.get("reaction_time") or "").strip()
    post_time = (
        (m.get("post_time") or m.get("post_created_at") or "").strip()
        or post_created_at_from_urn(m.get("urn") or "")
        or "unknown"
    )
    if activity_time or post_time != "unknown":
        time_part = f"Activity: {activity_time or 'unknown'} | Posted: {post_time}"
        tag_part = f"{tag_part} | {time_part}" if tag_part else time_part

    if content_level == CONTENT_LEVEL_MINIMAL:
        body = tag_part
    elif content_level == CONTENT_LEVEL_FULL and m.get("urn"):
        content = load_content(m["urn"])
        full_text = (
            _truncate(content, max_full_post_chars) if content else (m["summary"] or "")
        )
        body = (
            f"{tag_part} | Content: {full_text}"
            if tag_part
            else f"Content: {full_text}"
        )
    else:
        summary = (m["summary"] or "").strip()
        body = f"{tag_part} | Summary: {summary}" if tag_part else f"Summary: {summary}"

    if url:
        return f"- [{url}]: {body}"
    return f"- (no link): {body}"


def _batches_by_char_limit(
    metas: list[dict],
    char_limit: int,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
) -> list[list[dict]]:
    """Split metas into batches; start a new batch when adding the next post would exceed char_limit."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_len = 0
    for m in metas:
        block = _format_post_for_prompt(m, content_level, max_full_post_chars)
        if current and current_len + len(block) > char_limit:
            batches.append(current)
            current = []
            current_len = 0
        current.append(m)
        current_len += len(block)
    if current:
        batches.append(current)
    return batches


_BATCH_SYSTEM = (
    "You are a concise analyst. Summarize the following LinkedIn posts in 2–4 sentences. "
    "Highlight main themes, recurring topics, and any patterns. "
    "Each post includes 'Activity: <date>' and 'Posted: <date>' (or 'unknown'). "
    "Take temporality into account: if a post was published several weeks before the user reacted to it, "
    "add a brief note that it is 'late news' and assess whether it is still relevant. "
    "Posts are ordered by reaction time (earliest first). Output plain text, no preamble."
)


def _summarize_batch(
    llm,
    metas: list[dict],
    category_label: str,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    prompts_out: list[str] | None = None,
    period_dates: str | None = None,
) -> str:
    """One LLM call for this batch. Returns 2–4 sentence summary."""
    block = "\n\n".join(
        _format_post_for_prompt(m, content_level, max_full_post_chars) for m in metas
    )
    header = f"Period: {period_dates}\n\n" if period_dates else ""
    prompt = f"{header}Posts in '{category_label}' ({len(metas)}):\n\n---\n{block}\n---"
    if prompts_out is not None:
        prompts_out.append(prompt)
    response = llm.invoke(prompt, system_instruction=_BATCH_SYSTEM)
    return (response.content if hasattr(response, "content") else str(response)).strip()


def _format_other_section(
    metas: list[dict],
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
) -> str:
    """Format 'other' category. Always includes link."""
    lines = [
        _format_post_for_prompt(m, content_level, max_full_post_chars) for m in metas
    ]
    return "\n".join(lines) if lines else "_No posts in this category._"


_SINGLE_PASS_SYSTEM = (
    "You are a concise analyst. The user shares LinkedIn posts from a specific period. "
    "Each post includes 'Activity: <date>' and 'Posted: <date>' (or 'unknown'), plus URL, category/tags, "
    "and optionally summary or full content.\n\n"
    """Produce a markdown report with:
1. One section per category: Product announcements, Tutorials & how-to, Opinion & hot takes,
   Papers & research, Experiments & benchmarks, Company & career news, Other. Put each post into the right category
   based on content.
2. For each section: bullet points with the most important news for the category, with inline **links**
   to the relevant content as well as the source. See the example below for how links can be inserted.
   A bullet point may group articles if they cite the same product/idea/technology/etc. or are closely related
   (eg competing models, similar subdomains, etc).
3. Cite the post as the source of the information, with the Author name if it's a person, otherwise it can be inlined
   with the entity name making the announcement.
4. Take temporality into account: if a post was published several weeks before the user reacted to it,
   add a brief note that it is 'late news' and assess whether it is still relevant.
   Posts are ordered by reaction time (earliest first).
5. Output valid markdown only. No preamble.

Example input (keeping only the links for brevity):
**Posts**:
- https://www.linkedin.com/feed/update/urn:li:ugcPost:7432397932697481216
- https://www.linkedin.com/feed/update/urn:li:ugcPost:7432135776701693952
- https://www.linkedin.com/feed/update/urn:li:activity:7430628329965137920
- https://www.linkedin.com/feed/update/urn:li:activity:7427799631356473344
- https://www.linkedin.com/feed/update/urn:li:activity:7432043070642290688
- https://www.linkedin.com/feed/update/urn:li:activity:7432412067652943872

Example output (showing only the Product Announcements section):
The period saw several significant product launches and infrastructure releases.

- **Data visualization**: It looks like there is a new player in town in the datavis world!
  [Graphy](https://graphy.dev/) launched its Developer Platform as the first charting
  infrastructure for AI-native products, targeting the growing need for data visualization in AI applications
  ([from Andrey Vinitsky](https://www.linkedin.com/feed/update/urn:li:ugcPost:7432397932697481216)).
- **Agentic coding**:
    * [Cursor announced](https://www.linkedin.com/feed/update/urn:li:ugcPost:7432135776701693952/)
      a major capability upgrade allowing agents to setup their own VM and control their own computers and send videos
      of their work (get started [here](https://cursor.com/onboard)).
    * [LightOn](https://lighton.ai/) [released](https://huggingface.co/blog/lightonai/colgrep-lateon-code) 2 new
      LateOn-code models on HuggingFace, and ColGREP, a Rust-based multi-vector search tool for coding agents (from
      [Tom Aarsen](https://www.linkedin.com/feed/update/urn:li:activity:7427799631356473344/)).
- **Data and MCPs**: [data.gouv.fr](https://www.linkedin.com/feed/update/urn:li:activity:7432412067652943872/)
  announced an experimental MCP server to make public datasets accessible to AI chatbots.
  Check it out on Github: https://github.com/datagouv/datagouv-mcp.


https://www.linkedin.com/feed/update/urn:li:activity:7432043070642290688/ should be either in the Company & career
news or with Research news.
https://www.linkedin.com/feed/update/urn:li:activity:7430628329965137920/ can be skipped because it's not not so
relevant, an inference provider adding a new model must happen regularly.
"""
)


def _generate_single_pass_report(
    metas: list[dict],
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    report_provider: str | None = None,
    report_model: str | None = None,
    period_dates: str | None = None,
    sig: ReportSignature | None = None,
) -> str:
    """One LLM call: all posts with links; prompt asks for categorized report with links to key items."""
    blocks = "\n\n".join(
        _format_post_for_prompt(m, content_level, max_full_post_chars) for m in metas
    )
    header = f"Period: {period_dates}\n\n" if period_dates else ""
    prompt = f"{header}Posts ({len(metas)} total):\n\n{blocks}"
    if sig is not None:
        _save_report_prompt_debug("single-pass", _SINGLE_PASS_SYSTEM, [prompt], sig)
    llm = create_llm(
        stage="report",
        json_mode=False,
        provider_override=report_provider,
        model_override=report_model,
    )
    response = llm.invoke(prompt, system_instruction=_SINGLE_PASS_SYSTEM)
    return (response.content if hasattr(response, "content") else str(response)).strip()


def _resolve_max_posts(max_posts: int | None, content_level: str) -> int:
    """Use max_posts if set, else default for content level."""
    if max_posts is not None and max_posts > 0:
        return int(max_posts)
    return _default_max_posts(content_level)


def _report_signature(
    report_mode: str = REPORT_MODE_PER_CATEGORY,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_posts: int | None = None,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    report_provider: str | None = None,
    report_model: str | None = None,
    period: str = "7d",
    activities_csv_path: Path | None = None,
) -> tuple[str, int, tuple[str, ...], str, str, int, int, str] | None:
    """Signature of post set + report model + report mode + content_level + max_posts + max_full_post_chars + period."""
    limit = _resolve_max_posts(max_posts, content_level)
    metas, _ = _get_posts_for_period(
        period or "7d", limit, csv_path=activities_csv_path
    )
    if not metas:
        return None
    model_id = get_report_model_id(
        provider_override=report_provider,
        model_override=report_model,
    )
    return (
        model_id,
        len(metas),
        tuple((m.get("summarized_at") or "") for m in metas),
        report_mode,
        content_level,
        limit,
        max_full_post_chars,
        period or "7d",
    )


_REPORT_CACHE_FILE = "report_cache.json"
_REPORT_CACHE_VERSION = 4
_REPORT_PROMPT_DEBUG_FILE = "report_prompt_last.md"


def _get_report_cache_max_entries() -> int:
    """Max cached reports and prompts. Configurable via REPORT_CACHE_MAX_ENTRIES (default 100)."""
    try:
        return max(1, int(os.environ.get("REPORT_CACHE_MAX_ENTRIES", "100")))
    except (TypeError, ValueError):
        return 100


def _sig_to_key(sig: ReportSignature) -> dict:
    """Serialize signature to JSON-serializable dict for disk persistence."""
    d = {
        "model_id": sig[0],
        "n": sig[1],
        "summarized_at": list(sig[2]),
        "report_mode": sig[3],
        "content_level": sig[4],
        "max_posts": sig[5],
        "max_full_post_chars": sig[6],
    }
    if len(sig) > 7:
        d["period"] = sig[7]
    return d


def _sig_to_cache_key(sig: ReportSignature) -> str:
    """Canonical string key for O(1) dict lookup. Stable JSON serialization."""
    return json.dumps(_sig_to_key(sig), sort_keys=True)


def _key_matches(key: dict, sig: ReportSignature) -> bool:
    """True if key matches signature."""
    base = (
        key.get("model_id") == sig[0]
        and key.get("n") == sig[1]
        and tuple(key.get("summarized_at", [])) == sig[2]
        and key.get("report_mode") == sig[3]
        and key.get("content_level") == sig[4]
        and key.get("max_posts") == sig[5]
        and key.get("max_full_post_chars") == sig[6]
    )
    if len(sig) > 7:
        return base and key.get("period", "7d") == sig[7]
    return base


def _format_prompt_debug_content(mode: str, system: str, prompts: list[str]) -> str:
    """Format prompt debug as Markdown."""
    parts = [
        f"# Report prompt ({mode})\n\n## System instruction\n\n```\n{system}\n```\n"
    ]
    for i, p in enumerate(prompts):
        sep = "\n\n---\n\n" if i > 0 else ""
        parts.append(
            f"{sep}## User prompt{' ' + str(i + 1) if len(prompts) > 1 else ''}\n\n```\n{p}\n```"
        )
    return "\n".join(parts)


def _prompts_list_to_dict(prompts_list: list) -> dict:
    """Migrate v3 list format to dict keyed by canonical cache key."""
    out: dict = {}
    for e in prompts_list:
        key = e.get("key", {})
        ck = json.dumps(key, sort_keys=True)
        out[ck] = {
            "mode": e.get("mode", "?"),
            "system": e.get("system", ""),
            "prompts": e.get("prompts", []),
            "hits": e.get("hits", 0),
        }
    return out


def _reports_list_to_dict(reports_list: list) -> dict:
    """Migrate v3 list format to dict keyed by canonical cache key."""
    out: dict = {}
    for e in reports_list:
        key = e.get("key", {})
        ck = json.dumps(key, sort_keys=True)
        out[ck] = {"report": e.get("report", ""), "hits": e.get("hits", 0)}
    return out


def _save_report_prompt_debug(
    mode: str, system: str, prompts: list[str], sig: ReportSignature
) -> None:
    """Save report prompt to multi-entry cache keyed by signature (O(1) lookup)."""
    path = get_data_dir() / _REPORT_CACHE_FILE
    max_entries = _get_report_cache_max_entries()
    cache_key = _sig_to_cache_key(sig)
    try:
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        if data.get("report_cache_version", 0) < _REPORT_CACHE_VERSION:
            data = {"report_cache_version": _REPORT_CACHE_VERSION, "prompts": {}}
        prompts_dict = data.get("prompts")
        if isinstance(prompts_dict, list):
            prompts_dict = _prompts_list_to_dict(prompts_dict)
        prompts_dict = prompts_dict or {}
        existing_hits = prompts_dict.get(cache_key, {}).get("hits", 0)
        prompts_dict[cache_key] = {
            "mode": mode,
            "system": system,
            "prompts": prompts,
            "hits": existing_hits,
        }
        while len(prompts_dict) > max_entries:
            loser = min(prompts_dict, key=lambda k: prompts_dict[k].get("hits", 0))
            del prompts_dict[loser]
        data["prompts"] = prompts_dict
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        logger.info("Report prompt saved to cache (key=%s)", sig[0])
    except OSError as e:
        logger.warning("Could not save report prompt: %s", e)


def _load_report_prompt_debug(
    signature: ReportSignature | None = None,
) -> str:
    """Load prompt by signature. If None, returns placeholder."""
    if signature is None:
        return "_No report loaded. Run the pipeline, then view prompt._"
    path = get_data_dir() / _REPORT_CACHE_FILE
    if not path.exists():
        return "_No prompt cached for these params. Run the pipeline first._"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("report_cache_version", 0) < _REPORT_CACHE_VERSION:
            return "_No prompt cached for these params. Run the pipeline first._"
        prompts_data = data.get("prompts")
        if isinstance(prompts_data, list):
            prompts_data = _prompts_list_to_dict(prompts_data)
        if isinstance(prompts_data, dict):
            cache_key = _sig_to_cache_key(signature)
            entry = prompts_data.get(cache_key)
            if entry:
                entry["hits"] = entry.get("hits", 0) + 1
                try:
                    data["prompts"] = prompts_data
                    path.write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    pass
                return _format_prompt_debug_content(
                    entry.get("mode", "?"),
                    entry.get("system", ""),
                    entry.get("prompts", []),
                )
        return "_No prompt cached for these params. Run the pipeline first._"
    except (json.JSONDecodeError, OSError):
        return "_Could not read prompt cache._"


def _load_report_cache(
    sig: ReportSignature,
) -> tuple[str, ReportSignature] | None:
    """Load cached report by full signature (O(1) lookup). Returns (report, sig) or None."""
    path = get_data_dir() / _REPORT_CACHE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("report_cache_version", 0) < _REPORT_CACHE_VERSION:
            return _load_report_cache_v2(path, sig)
        reports_data = data.get("reports")
        if isinstance(reports_data, list):
            reports_data = _reports_list_to_dict(reports_data)
        if isinstance(reports_data, dict):
            cache_key = _sig_to_cache_key(sig)
            entry = reports_data.get(cache_key)
            if entry:
                entry["hits"] = entry.get("hits", 0) + 1
                try:
                    data["reports"] = reports_data
                    path.write_text(
                        json.dumps(data, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError:
                    pass
                report = entry.get("report", "")
                return (report, sig) if report else None
        return None
    except (json.JSONDecodeError, OSError):
        return None


def _load_report_cache_v2(
    path: Path, sig: ReportSignature
) -> tuple[str, ReportSignature] | None:
    """Backward compat: load from v2 single-entry format if key matches."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("report_cache_version", 0) != 2:
            return None
        model_id = data.get("model_id", "legacy")
        n = data.get("n", 0)
        at = tuple(data.get("summarized_at", []))
        cached_mode = data.get("report_mode", REPORT_MODE_PER_CATEGORY)
        cached_level = data.get("content_level")
        if cached_level is None:
            use_full = data.get("use_full_posts", True)
            cached_level = CONTENT_LEVEL_FULL if use_full else CONTENT_LEVEL_SUMMARY
        cached_max = data.get("max_posts", REPORT_MAX_POSTS)
        cached_full_chars = data.get(
            "max_full_post_chars", REPORT_MAX_FULL_POST_CHARS_DEFAULT
        )
        cached_period = data.get("period", "7d")
        cached_sig: ReportSignature = (
            model_id,
            n,
            at,
            cached_mode,
            cached_level,
            cached_max,
            cached_full_chars,
            cached_period,
        )
        if not _key_matches(_sig_to_key(cached_sig), sig):
            return None
        report = data.get("report", "")
        return (report, sig) if report else None
    except (json.JSONDecodeError, OSError):
        return None


def _save_report_cache(report: str, sig: ReportSignature) -> None:
    """Persist report to multi-entry cache keyed by signature (O(1) lookup). Evicts lowest-hit when at max."""
    path = get_data_dir() / _REPORT_CACHE_FILE
    max_entries = _get_report_cache_max_entries()
    cache_key = _sig_to_cache_key(sig)
    try:
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        if data.get("report_cache_version", 0) < _REPORT_CACHE_VERSION:
            data = {
                "report_cache_version": _REPORT_CACHE_VERSION,
                "reports": {},
                "prompts": data.get("prompts", {}),
            }
        reports_dict = data.get("reports")
        if isinstance(reports_dict, list):
            reports_dict = _reports_list_to_dict(reports_dict)
        reports_dict = reports_dict or {}
        existing_hits = reports_dict.get(cache_key, {}).get("hits", 0)
        reports_dict[cache_key] = {"report": report, "hits": existing_hits}
        while len(reports_dict) > max_entries:
            loser = min(reports_dict, key=lambda k: reports_dict[k].get("hits", 0))
            del reports_dict[loser]
        data["reports"] = reports_dict
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def generate_activity_report(
    report_mode: str = REPORT_MODE_PER_CATEGORY,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_posts: int | None = None,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    report_provider: str | None = None,
    report_model: str | None = None,
    period: str = "7d",
    activities_csv_path: Path | None = None,
) -> str:
    """Build report. Per-category: batches per category; 'other' is summaries+links.
    Single-pass: one LLM call with all links. Content: Minimal, Summary, or Full.
    When period is set, scopes to posts with activity rows in that period in activities.csv.
    """
    setup_gcp_credentials()
    limit = _resolve_max_posts(max_posts, content_level)
    csv_path = activities_csv_path or get_default_csv_path()
    metas, period_dates = _get_posts_for_period(
        period or "7d", limit, csv_path=csv_path
    )
    if not metas:
        return "No summarized posts found. Run the pipeline first (collect → enrich → summarize)."
    sig = _report_signature(
        report_mode=report_mode,
        content_level=content_level,
        max_posts=max_posts,
        max_full_post_chars=max_full_post_chars,
        report_provider=report_provider,
        report_model=report_model,
        period=period or "7d",
        activities_csv_path=csv_path,
    )
    if report_mode == REPORT_MODE_SINGLE_PASS:
        try:
            return _generate_single_pass_report(
                metas,
                content_level,
                max_full_post_chars=max_full_post_chars,
                report_provider=report_provider,
                report_model=report_model,
                period_dates=period_dates,
                sig=sig,
            )
        except Exception as e:
            logger.exception("Single-pass report failed")
            return _report_error_message(e)
    by_category: dict[str, list[dict]] = {}
    for m in metas:
        cat = (m.get("category") or "").strip().lower() or "other"
        if cat not in REPORT_CATEGORIES:
            cat = "other"
        by_category.setdefault(cat, []).append(m)
    try:
        llm = create_llm(
            stage="report",
            json_mode=False,
            provider_override=report_provider,
            model_override=report_model,
        )
        parts = []
        prompts_collected: list[str] = []
        for cat in REPORT_CATEGORIES:
            category_metas = by_category.get(cat)
            if not category_metas:
                continue
            label = CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
            if cat == "other":
                parts.append(
                    f"## {label}\n\n{_format_other_section(category_metas, content_level, max_full_post_chars)}"
                )
                continue
            batches = _batches_by_char_limit(
                category_metas,
                REPORT_BATCH_CHAR_LIMIT,
                content_level,
                max_full_post_chars,
            )
            batch_summaries = [
                _summarize_batch(
                    llm,
                    batch,
                    label,
                    content_level,
                    max_full_post_chars=max_full_post_chars,
                    prompts_out=prompts_collected,
                    period_dates=period_dates,
                )
                for batch in batches
            ]
            parts.append(f"## {label}\n\n" + "\n\n".join(batch_summaries))
        if prompts_collected and sig is not None:
            _save_report_prompt_debug(
                "per-category", _BATCH_SYSTEM, prompts_collected, sig
            )
        if not parts:
            return "No posts to summarize."
        return "\n\n".join(parts)
    except Exception as e:
        logger.exception("Report generation failed")
        msg = str(e)
        if "504" in msg or "Gateway time-out" in msg or "timeout" in msg.lower():
            return (
                "❌ The LLM request timed out (504). Try again in a few minutes, "
                "or run the pipeline with a **limit** to use fewer posts."
            )
        if "<!DOCTYPE" in msg or "<html" in msg.lower() or "<span" in msg:
            return "❌ The LLM provider returned an error page. Try again later or check your API/network."
        return f"❌ Error generating report: {msg[:200]}"


def _report_error_message(e: Exception) -> str:
    """Turn an exception into a short, UI-safe message (no HTML)."""
    msg = str(e)
    if "504" in msg or "Gateway time-out" in msg or "timeout" in msg.lower():
        return (
            "❌ The LLM request timed out (504). Try again in a few minutes, "
            "or run the pipeline with a limit to use fewer posts."
        )
    if any(tag in msg for tag in ("<!DOCTYPE", "<html", "<span", "<div")):
        return "❌ The LLM provider returned an error page. Try again later or check your API/network."
    return f"❌ Error: {msg[:200]}"


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)
PIPELINE_HINT_TEXT = "Click Get latest news report to refresh data and get a summary."
MIN_PROGRESS_VISIBILITY_SECONDS = 0.6
# Scalingo frontal servers drop idle WebSocket connections after ~30s; keepalive
# yields must arrive more often during long enrich/report steps.
WS_KEEPALIVE_SECONDS = 20.0
PERIOD_SYNTAX = "e.g. 1d, 7d, 14d, 30d, 1w, 2w, 1m"

_T = TypeVar("_T")
KEEPALIVE_TICK = object()


def _stream_with_keepalive(
    iterator: Iterator[_T],
    keepalive: Callable[[], _T],
    *,
    interval: float = WS_KEEPALIVE_SECONDS,
) -> Iterator[_T]:
    """Yield from *iterator*; emit *keepalive()* if no item arrives within *interval*."""
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def feed() -> None:
        try:
            for item in iterator:
                q.put(item)
        except Exception as e:
            q.put(e)
        finally:
            q.put(sentinel)

    threading.Thread(target=feed, daemon=True).start()
    while True:
        try:
            item = q.get(timeout=interval)
        except queue.Empty:
            yield keepalive()
            continue
        if item is sentinel:
            break
        if isinstance(item, Exception):
            raise item
        yield item


_ANGLE_BRACKET_URL_RE = re.compile(r"<(https?://[^>\s]+)>")


def _normalize_report_markdown(report: str) -> str:
    """Prepare LLM report text for Gradio Markdown (strip fences, ensure non-empty).

    Angle-bracket autolinks (``<https://…>``) and stray ``<`` confuse Gradio's HTML
    sanitizer after markdown→HTML conversion, which can blank the report panel even
    when the run succeeded server-side.
    """
    text = (report or "").strip()
    if not text:
        return "_Report was empty. Try again or check Scalingo logs._"
    fence = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text:
        return "_Report was empty. Try again or check Scalingo logs._"
    lower = text.lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return (
            "_Report looked like an HTML error page, not markdown. "
            "Check Scalingo logs or try again._"
        )
    text = _ANGLE_BRACKET_URL_RE.sub(r"[\1](\1)", text)
    return text


def _render_pipeline_status(
    step_label: str | None = None,
    stage_progress: tuple[int, float] | None = None,
) -> str:
    """
    Render status below the run button: hint when idle, stage label + 5-segment bar while running.
    stage_progress: (stage_index 0-4, progress_in_stage 0-1). Bar splits into 5 segments.
    """
    if step_label is None or stage_progress is None:
        return (
            '<div style="color: #6b7280; margin: 0.25rem 0 0.5rem 0;">'
            f"{html.escape(PIPELINE_HINT_TEXT)}"
            "</div>"
        )
    stage_idx, prog = stage_progress
    stage_idx = max(0, min(4, stage_idx))
    prog = max(0.0, min(1.0, prog))
    segments: list[float] = []
    for i in range(5):
        if i < stage_idx:
            segments.append(1.0)
        elif i == stage_idx:
            segments.append(prog)
        else:
            segments.append(0.0)
    pct = [int(round(s * 100)) for s in segments]
    segment_css = (
        "flex: 1; min-width: 0; height: 100%; background: #e5e7eb; "
        "overflow: hidden; display: flex;"
    )
    fill_css = "height: 100%; background: #f97316; transition: width 200ms ease;"
    return (
        f'<div style="margin: 0.25rem 0; color: #111827;">{html.escape(step_label)}</div>'
        '<div style="display: flex; width: 100%; height: 10px; gap: 2px; '
        'border-radius: 4px; overflow: hidden;">'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[0]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[1]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[2]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[3]}%;"></div></div>'
        f'<div style="{segment_css}">'
        f'<div style="{fill_css} width: {pct[4]}%;"></div></div>'
        "</div>"
    )


def _parse_fraction(line: str, prefix: str) -> tuple[int, int] | None:
    """Extract (done, total) from lines like 'Enriching 3/10…' or 'Summarizing batch 2/5…'."""
    if not line.startswith(prefix) or "/" not in line:
        return None
    try:
        done_str, total_str = line.removeprefix(prefix).rstrip("…").split("/")
        return int(done_str), int(total_str)
    except (ValueError, IndexError):
        return None


def _status_from_pipeline_line(line: str) -> tuple[tuple[int, float], str] | None:
    """
    Map pipeline stream lines to (stage_index, progress_in_stage), label.
    Stages: 0=fetching, 1=enriching, 2=fetch linked URLs, 3=summarizing, 4=preparing report.
    """
    if line.startswith("Starting pipeline"):
        return (0, 0.0), "fetching…"
    if "Collected" in line:
        return (0, 1.0), "fetching…"
    frac = _parse_fraction(line, "Enriching ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (1, p), f"enriching [{done}/{total}]…"
    if "Enriched" in line:
        return (1, 1.0), "enriching…"
    frac = _parse_fraction(line, "Fetching linked URLs ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (2, p), f"fetching linked URLs [{done}/{total}]…"
    if "Fetched" in line and "URL" in line:
        return (2, 1.0), "fetching linked URLs…"
    frac = _parse_fraction(line, "Summarizing batch ")
    if frac is not None:
        done, total = frac
        p = (done / total) if total > 0 else 1.0
        return (3, p), f"summarizing [{done}/{total}]…"
    if "Summarized" in line:
        return (3, 1.0), "summarizing…"
    if "✅ Done" in line:
        return (4, 0.0), "preparing report…"
    if line.startswith("❌"):
        return (4, 1.0), "Failed."
    return None


class GraphRAGServices:
    """Container for initialized GraphRAG services."""

    def __init__(self, driver: "Driver", embedder, llm):
        self.driver = driver
        self.embedder = embedder
        self.llm = llm


def setup_gcp_credentials() -> str | None:
    creds_json = os.getenv("GOOGLE_APPLICATION_CREDENTIALS_JSON")
    if not creds_json or os.getenv("GOOGLE_APPLICATION_CREDENTIALS"):
        return None
    try:
        creds_data = json.loads(creds_json)
        fd, creds_path = tempfile.mkstemp(suffix=".json", prefix="gcp_credentials_")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(creds_data, f)
            os.chmod(creds_path, 0o600)
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = creds_path
            project_id: str | None = creds_data.get("project_id")
            logger.info(f"Loaded GCP credentials to {creds_path} (0600)")
            return str(project_id) if project_id else None
        except (OSError, json.JSONDecodeError) as e:
            try:
                os.unlink(creds_path)
            except OSError:
                pass
            logger.warning(f"Failed to write credentials file: {e}")
            return None
    except json.JSONDecodeError as e:
        logger.warning(f"Failed to parse GOOGLE_APPLICATION_CREDENTIALS_JSON: {e}")
        return None


def resolve_vertex_project(project_id_from_creds: str | None) -> str:
    vertex_project = os.getenv("VERTEX_PROJECT") or project_id_from_creds
    if not vertex_project:
        try:
            import google.auth

            _, vertex_project = google.auth.default()
        except (ImportError, Exception):
            pass
    if not vertex_project:
        raise RuntimeError(
            "Vertex AI project not found. Set VERTEX_PROJECT or ensure "
            "GOOGLE_APPLICATION_CREDENTIALS_JSON contains project_id."
        )
    return vertex_project


def initialize_services() -> GraphRAGServices:
    project_id = setup_gcp_credentials()
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
        driver.verify_connectivity()
        logger.info("Connected to Neo4j successfully")
    except Exception as e:
        raise RuntimeError(f"Failed to connect to Neo4j: {e}") from e
    try:
        if os.getenv("LLM_PROVIDER", "openai") == "vertexai":
            try:
                import vertexai

                vertex_project = resolve_vertex_project(project_id)
                vertex_location = os.getenv("VERTEX_LOCATION", "[REDACTED]")
                vertexai.init(project=vertex_project, location=vertex_location)
            except ImportError:
                pass
        embedder = create_embedder()
        llm = create_llm()
        logger.info("Initialized LLM and embedder successfully")
    except Exception as e:
        raise RuntimeError(f"Failed to initialize LLM/embedder: {e}") from e
    return GraphRAGServices(driver, embedder, llm)


def query_linkedin_graphrag(
    services: GraphRAGServices,
    query_text: str,
    use_cypher: bool = False,
    top_k: int = 5,
) -> str:
    if not query_text.strip():
        return "⚠️ Please enter a query."
    try:
        retriever = (
            create_vector_cypher_retriever(services.driver, services.embedder)
            if use_cypher
            else create_vector_retriever(services.driver, services.embedder)
        )
        rag = GraphRAG(llm=services.llm, retriever=retriever)
        response = rag.search(
            query_text=query_text,
            retriever_config={"top_k": top_k},
            return_context=True,
        )
        answer = response.answer
        if hasattr(response, "retriever_result") and response.retriever_result:
            if len(response.retriever_result.items) > 0:
                answer += f"\n\n---\n\n📊 **Retrieved {len(response.retriever_result.items)} relevant chunks**"
        return answer
    except Exception as e:
        logger.error(f"Query error: {e}", exc_info=True)
        return f"❌ Error: {e}\n\nCheck configuration and that content is indexed."


def get_database_stats(services: GraphRAGServices) -> str:
    try:
        with services.driver.session(database=NEO4J_DATABASE) as session:
            chunk_result = session.run(
                "MATCH (c:Chunk) RETURN count(c) as count"
            ).single()
            chunk_count = chunk_result["count"] if chunk_result else 0
            embedding_result = session.run(
                "MATCH (c:Chunk) WHERE c.embedding IS NOT NULL RETURN count(c) as count"
            ).single()
            chunk_with_embedding = embedding_result["count"] if embedding_result else 0
            post_result = session.run(
                "MATCH (p:Post) RETURN count(p) as count"
            ).single()
            post_count = post_result["count"] if post_result else 0
            comment_result = session.run(
                "MATCH (c:Comment) RETURN count(c) as count"
            ).single()
            comment_count = comment_result["count"] if comment_result else 0
            enrichment_counts = {}
            for label in [
                "Resource",
                "Technology",
                "Concept",
                "Process",
                "Challenge",
                "Benefit",
                "Example",
            ]:
                result = session.run(
                    f"MATCH (n:{label}) RETURN count(n) as count"
                ).single()
                count = result["count"] if result else 0
                if count > 0:
                    enrichment_counts[label] = count
            output = "**Database Statistics**\n\n"
            output += f"- Total Chunks: {chunk_count}\n"
            output += f"- Chunks with Embeddings: {chunk_with_embedding}\n"
            output += f"- Posts: {post_count}\n"
            output += f"- Comments: {comment_count}\n"
            if enrichment_counts:
                output += "\n**Enrichment Nodes:**\n"
                for label, count in enrichment_counts.items():
                    output += f"- {label}: {count}\n"
            if chunk_with_embedding == 0:
                output += (
                    "\n⚠️ **Warning**: No embeddings found. Run `index_content` first."
                )
            elif chunk_with_embedding < chunk_count:
                output += f"\n⚠️ **Warning**: {chunk_count - chunk_with_embedding} chunks missing embeddings."
            return output
    except Exception as e:
        logger.error(f"Error getting database stats: {e}", exc_info=True)
        return f"❌ Error getting stats: {str(e)}"


def create_pipeline_interface():
    """Pipeline tab: single button runs collect → enrich → summarize → report."""
    with gr.Blocks(
        title="Pipeline",
        css=(
            "#report-output { min-height: 24em; overflow-y: auto; }"
            "#pipeline-status { min-height: 2.8em; }"
        ),
    ) as block:
        gr.Markdown(
            "# Pipeline\nOne run: fetch/enrich/summarize (using caches when possible), then generate report."
        )
        with gr.Row():
            period = gr.Dropdown(
                choices=["1d", "2d", "7d", "14d", "30d", "1w", "2w", "1m"],
                value="7d",
                label="Period",
                info=PERIOD_SYNTAX,
            )
            from_cache = gr.Checkbox(
                value=False,
                label="Skip fetch (use cached data only)",
                info="No LinkedIn API call; use only previously fetched data.",
            )
            limit = gr.Number(value=None, label="Limit (optional)", precision=0)
            report_mode = gr.Dropdown(
                choices=REPORT_MODE_CHOICES,
                value=REPORT_MODE_SINGLE_PASS,
                label="Report mode",
            )
            content_level = gr.Dropdown(
                choices=CONTENT_LEVEL_CHOICES,
                value=CONTENT_LEVEL_MINIMAL,
                label="Content level",
            )
            max_posts_report = gr.Number(
                value=REPORT_MAX_POSTS_MINIMAL,
                label="Max posts (report)",
                minimum=1,
                maximum=500,
                precision=0,
                info="Defaults: 100 (minimal), 50 (summary), 20 (full).",
            )
            max_full_post_chars = gr.Number(
                value=REPORT_MAX_FULL_POST_CHARS_DEFAULT,
                label="Max post content length",
                minimum=100,
                maximum=10000,
                precision=0,
                info="Chars per post when Full. Ignored for Minimal/Summary.",
            )

        def suggest_max_posts(content_lvl: str):
            return _default_max_posts(content_lvl or CONTENT_LEVEL_SUMMARY)

        content_level.change(
            fn=suggest_max_posts,
            inputs=[content_level],
            outputs=[max_posts_report],
        )
        with gr.Accordion("Model selection", open=False):
            sp, sm = get_default_provider_model("summary")
            rp, rm = get_default_provider_model("report")
            provider_choices = ["ollama", "anthropic", "mammouth"]

            models_by_provider = fetch_all_provider_models()
            for prov in (sp, rp):
                if not models_by_provider.get(prov, []):
                    models_by_provider[prov] = fetch_models_for_provider(prov) or []

            def _choice_ids(choices: list[tuple[str, str]]) -> list[str]:
                """Extract model ids from (label, model_id) choices."""
                return [c[1] for c in choices]

            def _resolve_model_value(
                stage: str,
                provider: str,
                default_model: str,
                choices: list[tuple[str, str]],
            ) -> str:
                ids = _choice_ids(choices)
                if default_model in ids:
                    return default_model
                fallback = ids[0] if ids else ""
                if default_model:
                    logger.warning(
                        "Configured %s model %r is not in the %s provider list; "
                        "using %r instead. Check LLM_*_MODEL / LLM_MODEL and provider.",
                        stage,
                        default_model,
                        provider,
                        fallback,
                    )
                return fallback

            def _choices_for(
                d: dict, provider: str, default_model: str, stage: str
            ) -> tuple[list[tuple[str, str]], str]:
                models = d.get(provider, [])
                choices = models if models else [(default_model, default_model)]
                value = _resolve_model_value(stage, provider, default_model, choices)
                return choices, value

            s_choices, s_val = _choices_for(models_by_provider, sp, sm, "summary")
            r_choices, r_val = _choices_for(models_by_provider, rp, rm, "report")

            with gr.Row():
                with gr.Column():
                    gr.Markdown("**Summary** (categorization & short summary)")
                    summary_provider = gr.Dropdown(
                        choices=provider_choices,
                        value=sp,
                        label="Provider",
                    )
                    summary_model = gr.Dropdown(
                        choices=s_choices,
                        value=s_val,
                        label="Model",
                    )
                with gr.Column():
                    gr.Markdown("**Report** (batch summaries & final report)")
                    report_provider = gr.Dropdown(
                        choices=provider_choices,
                        value=rp,
                        label="Provider",
                    )
                    report_model = gr.Dropdown(
                        choices=r_choices,
                        value=r_val,
                        label="Model",
                    )

            def refresh_summary_models(provider):
                choices = fetch_models_for_provider(provider) or [(sm, sm)]
                value = _resolve_model_value("summary", provider, sm, choices)
                return gr.update(choices=choices, value=value)

            def refresh_report_models(provider):
                choices = fetch_models_for_provider(provider) or [(rm, rm)]
                value = _resolve_model_value("report", provider, rm, choices)
                return gr.update(choices=choices, value=value)

            summary_provider.change(
                refresh_summary_models,
                inputs=[summary_provider],
                outputs=[summary_model],
            )
            report_provider.change(
                refresh_report_models,
                inputs=[report_provider],
                outputs=[report_model],
            )
        run_btn = gr.Button("Get latest news report", variant="primary")
        pipeline_status = gr.HTML(
            value=_render_pipeline_status(), elem_id="pipeline-status"
        )
        report_output = gr.Markdown(
            value="Report will appear here after the pipeline run.",
            label="Report",
            elem_id="report-output",
            # Self-generated LLM output; default sanitizer strips <https://…> autolinks.
            sanitize_html=False,
        )
        with gr.Accordion("Debug: Last report prompt", open=False):
            prompt_debug_btn = gr.Button("View last prompt", size="sm")
            prompt_debug_output = gr.Markdown(
                value=_load_report_prompt_debug(),
                label="Prompt",
                elem_id="prompt-debug-output",
            )
        report_cache_state = gr.State(value=None)  # (report_text, signature) or None

        def load_prompt_debug(cache_state):
            sig = cache_state[1] if cache_state else None
            return _load_report_prompt_debug(sig)

        prompt_debug_btn.click(
            fn=load_prompt_debug,
            inputs=[report_cache_state],
            outputs=[prompt_debug_output],
        )

        def prepare_run(cache):
            return (
                _render_pipeline_status("fetching…", (0, 0.0)),
                gr.update(value="_Running pipeline… report will appear when ready._"),
                cache,
                gr.update(interactive=False),
                gr.update(),
            )

        def run_all(
            last: str,
            from_cache: bool,
            lim,
            mode: str,
            content_lvl: str,
            max_posts_val,
            max_full_chars_val,
            sum_prov,
            sum_mod,
            rep_prov,
            rep_mod,
            cache,
        ):
            logger.info(
                "Pipeline & report started: last=%s from_cache=%s limit=%s",
                last,
                from_cache,
                lim,
            )
            last_clean = (last or "").strip()
            if _parse_last(last_clean) is None:
                err = f"Invalid period '{last}'. {PERIOD_SYNTAX}"
                yield _render_pipeline_status(
                    "Invalid period", (0, 0.0)
                ), err, cache, gr.update(interactive=True), gr.update()
                return
            started_at = time.monotonic()

            def _ensure_min_progress_visibility() -> None:
                elapsed = time.monotonic() - started_at
                remaining = MIN_PROGRESS_VISIBILITY_SECONDS - elapsed
                if remaining > 0:
                    time.sleep(remaining)

            try:
                lim_int = int(lim) if lim not in (None, "", float("nan")) else None
            except (TypeError, ValueError):
                lim_int = None

            stage_progress: tuple[int, float] = (0, 0.0)
            step_label = "fetching…"

            def _pipeline_keepalive_outputs():
                return (
                    _render_pipeline_status(step_label, stage_progress),
                    gr.update(),
                    cache,
                    gr.update(interactive=False),
                    gr.update(),
                )

            try:
                pipeline = run_pipeline_ui_streaming(
                    last=last_clean,
                    from_cache=from_cache,
                    limit=lim_int,
                    summary_provider=sum_prov or None,
                    summary_model=sum_mod or None,
                )
                for chunk in _stream_with_keepalive(pipeline, lambda: KEEPALIVE_TICK):
                    if chunk is KEEPALIVE_TICK:
                        yield _pipeline_keepalive_outputs()
                        continue
                    last = chunk.strip().split("\n")[-1] if chunk.strip() else ""
                    status_update = _status_from_pipeline_line(last)
                    if status_update is not None:
                        stage_progress, step_label = status_update
                    if last.startswith("❌"):
                        error_text = last
                        if error_text.startswith("❌ "):
                            error_text = error_text[2:].strip()
                        if not error_text:
                            error_text = "Pipeline failed."
                        _ensure_min_progress_visibility()
                        yield _render_pipeline_status(
                            step_label, stage_progress
                        ), f"⚠️ {error_text}", cache, gr.update(
                            interactive=True
                        ), gr.update()
                        return
                    yield _pipeline_keepalive_outputs()
            except Exception as e:
                logger.exception("Pipeline failed")
                err_msg = str(e)[:200]
                _ensure_min_progress_visibility()
                yield _render_pipeline_status(
                    "Failed.", (4, 1.0)
                ), f"⚠️ Pipeline failed: {err_msg}", cache, gr.update(
                    interactive=True
                ), gr.update()
                return

            stage_progress, step_label = (4, 0.0), "preparing report…"
            yield (
                _render_pipeline_status(step_label, stage_progress),
                gr.update(value="_Generating report…_"),
                cache,
                gr.update(interactive=False),
                gr.update(),
            )
            report_mode_val = mode or REPORT_MODE_PER_CATEGORY
            content_level_val = content_lvl or CONTENT_LEVEL_SUMMARY
            try:
                max_posts_int = (
                    int(max_posts_val)
                    if max_posts_val not in (None, "", float("nan"))
                    else None
                )
            except (TypeError, ValueError):
                max_posts_int = None
            try:
                max_full_chars_int = (
                    int(max_full_chars_val)
                    if max_full_chars_val not in (None, "", float("nan"))
                    else REPORT_MAX_FULL_POST_CHARS_DEFAULT
                )
            except (TypeError, ValueError):
                max_full_chars_int = REPORT_MAX_FULL_POST_CHARS_DEFAULT
            max_full_chars_int = max(
                100,
                min(10000, max_full_chars_int or REPORT_MAX_FULL_POST_CHARS_DEFAULT),
            )
            max_posts_resolved = _resolve_max_posts(max_posts_int, content_level_val)
            logger.info(
                "Report mode: raw=%r → %s; content: raw=%r → %s; max_posts=%s; max_full=%s",
                mode,
                report_mode_val,
                content_lvl,
                content_level_val,
                max_posts_resolved,
                max_full_chars_int,
            )
            sig = _report_signature(
                report_mode=report_mode_val,
                content_level=content_level_val,
                max_posts=max_posts_int,
                max_full_post_chars=max_full_chars_int,
                report_provider=rep_prov or None,
                report_model=rep_mod or None,
                period=last_clean,
                activities_csv_path=get_default_csv_path(),
            )
            disk = _load_report_cache(sig) if sig else None

            def _is_cache_valid(cached_sig: tuple) -> bool:
                if cached_sig != sig:
                    return False
                if len(cached_sig) > 4 and cached_sig[4] != content_level_val:
                    logger.info(
                        "Cache invalid: content_level mismatch %r vs %r",
                        cached_sig[4],
                        content_level_val,
                    )
                    return False
                return True

            if disk is not None and _is_cache_valid(disk[1]):
                result = disk[0]
                logger.info("Report cache hit (disk)")
                cache = (result, sig)
            elif cache is not None and _is_cache_valid(cache[1]):
                result = cache[0]
                logger.info("Report cache hit (session)")
            else:
                report_q: queue.Queue = queue.Queue()
                sentinel = object()

                def _generate_report_worker() -> None:
                    try:
                        report_q.put(
                            generate_activity_report(
                                report_mode=report_mode_val,
                                content_level=content_level_val,
                                max_posts=max_posts_int,
                                max_full_post_chars=max_full_chars_int,
                                report_provider=rep_prov or None,
                                report_model=rep_mod or None,
                                period=last_clean,
                                activities_csv_path=get_default_csv_path(),
                            )
                        )
                    except Exception as e:
                        report_q.put(e)
                    finally:
                        report_q.put(sentinel)

                threading.Thread(target=_generate_report_worker, daemon=True).start()
                report_item = None
                while report_item is None:
                    try:
                        item = report_q.get(timeout=WS_KEEPALIVE_SECONDS)
                    except queue.Empty:
                        logger.info("Report generation still in progress…")
                        yield (
                            _render_pipeline_status("preparing report…", (4, 0.5)),
                            gr.update(value="_Generating report…_"),
                            cache,
                            gr.update(interactive=False),
                            gr.update(),
                        )
                        continue
                    if item is sentinel:
                        break
                    report_item = item
                if report_item is None:
                    result = "⚠️ Report generation ended unexpectedly."
                    cache = None
                elif isinstance(report_item, Exception):
                    logger.error(
                        "Report generation failed: %s", report_item, exc_info=True
                    )
                    result = _report_error_message(report_item)
                    cache = None
                else:
                    result = report_item
                    cache = (result, sig) if sig else None
                    if sig is not None:
                        _save_report_cache(result, sig)
            display_result = _normalize_report_markdown(result)
            # Do not push the full prompt debug in the final WS message (can be huge);
            # user loads it on demand via "View last prompt".
            yield (
                _render_pipeline_status(None, None),
                display_result,
                cache,
                gr.update(interactive=True),
                gr.update(),
            )

        run_btn.click(
            fn=prepare_run,
            inputs=[report_cache_state],
            outputs=[
                pipeline_status,
                report_output,
                report_cache_state,
                run_btn,
                prompt_debug_output,
            ],
            queue=False,
        ).then(
            fn=run_all,
            inputs=[
                period,
                from_cache,
                limit,
                report_mode,
                content_level,
                max_posts_report,
                max_full_post_chars,
                summary_provider,
                summary_model,
                report_provider,
                report_model,
                report_cache_state,
            ],
            outputs=[
                pipeline_status,
                report_output,
                report_cache_state,
                run_btn,
                prompt_debug_output,
            ],
            show_progress="hidden",
        )
    return block


def create_query_interface():
    """GraphRAG query tab; lazy-init on first use."""
    with gr.Blocks(title="GraphRAG Query") as block:
        gr.Markdown(
            "# GraphRAG Query\nQuery indexed LinkedIn content. Click Initialize to connect."
        )
        services_state = gr.State(value=None)

        def init_fn():
            try:
                services = initialize_services()
                return services, "Connected to Neo4j and Vertex AI. You can query now."
            except RuntimeError as e:
                logger.exception("GraphRAG init failed")
                return None, f"Initialization failed: {e}"

        init_btn = gr.Button("Initialize GraphRAG", variant="secondary")
        init_status = gr.Markdown(
            value="GraphRAG not initialized. Click the button to connect."
        )
        with gr.Row():
            with gr.Column(scale=2):
                query_input = gr.Textbox(
                    label="Your Question",
                    placeholder="What are the main themes in my LinkedIn posts?",
                    lines=3,
                )
                with gr.Row():
                    use_cypher = gr.Checkbox(
                        label="Use Graph Traversal (Cypher)",
                        value=False,
                        info="Include related entities in the search",
                    )
                    top_k = gr.Slider(1, 20, value=5, step=1, label="top_k")
                submit_btn = gr.Button("Search", variant="primary", size="lg")
            with gr.Column(scale=1):
                stats_output = gr.Markdown(value="Initialize GraphRAG to load stats.")
                refresh_stats = gr.Button("Refresh Stats", size="sm")
        answer_output = gr.Markdown(label="Answer")
        gr.Examples(
            examples=[
                ["What topics do I post about most frequently?", False, 5],
                ["Show me posts about AI or machine learning", False, 10],
                ["Who are the most active commenters on my posts?", True, 10],
            ],
            inputs=[query_input, use_cypher, top_k],
        )

        init_btn.click(
            fn=init_fn,
            inputs=[],
            outputs=[services_state, init_status],
        )

        def do_query(svc, q, cypher, k):
            if svc is None:
                return "Click **Initialize GraphRAG** first."
            return query_linkedin_graphrag(svc, q, cypher, k)

        def do_stats(svc):
            if svc is None:
                return "Click **Initialize GraphRAG** to load stats."
            return get_database_stats(svc)

        submit_btn.click(
            fn=do_query,
            inputs=[services_state, query_input, use_cypher, top_k],
            outputs=answer_output,
        )
        query_input.submit(
            fn=do_query,
            inputs=[services_state, query_input, use_cypher, top_k],
            outputs=answer_output,
        )
        refresh_stats.click(
            fn=do_stats,
            inputs=[services_state],
            outputs=stats_output,
        )
    return block


def main():
    pipeline_demo = create_pipeline_interface()
    query_demo = create_query_interface()
    demo = gr.TabbedInterface(
        [pipeline_demo, query_demo],
        ["Pipeline", "GraphRAG query"],
        title="LinkedIn MVP",
    )
    port = int(os.getenv("PORT", 7860))
    host = os.getenv("HOST", "0.0.0.0")
    logger.info(
        "LLM_PROVIDER=%s (embedding: %s)",
        os.getenv("LLM_PROVIDER", "<unset>"),
        os.getenv("EMBEDDING_PROVIDER", "<unset>"),
    )
    logger.info(f"Starting Gradio app on {host}:{port}")
    demo.queue(default_concurrency_limit=1)
    demo.launch(server_name=host, server_port=port, share=False, show_error=True)


if __name__ == "__main__":
    main()
