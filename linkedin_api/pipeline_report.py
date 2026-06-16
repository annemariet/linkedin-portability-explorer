"""LinkedIn activity report: post selection, LLM generation, and disk cache."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from linkedin_api.activity_csv import get_data_dir, get_default_csv_path
from linkedin_api.content_store import (
    _ms_to_iso,
    list_summarized_metadata,
    load_content,
)
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.llm_config import create_llm, get_report_model_id
from linkedin_api.summarize_activity import _parse_last, collect_from_csv
from linkedin_api.utils.linkedin_snowflake import post_created_at_from_urn

logger = logging.getLogger(__name__)

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


class PipelineCancelledError(Exception):
    """User clicked Stop in the Gradio pipeline UI."""


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


def _llm_response_text(response) -> str:
    return (response.content if hasattr(response, "content") else str(response)).strip()


def _group_metas_by_category(metas: list[dict]) -> dict[str, list[dict]]:
    by_category: dict[str, list[dict]] = {}
    for m in metas:
        cat = (m.get("category") or "").strip().lower() or "other"
        if cat not in REPORT_CATEGORIES:
            cat = "other"
        by_category.setdefault(cat, []).append(m)
    return by_category


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


def _summarize_report_batch(
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
    return _llm_response_text(response)


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


def _is_llm_timeout_error(exc: BaseException) -> bool:
    """True for proxy/origin timeouts (e.g. Cloudflare 524, gateway 504)."""
    msg = str(exc).lower()
    return (
        "524" in msg
        or "504" in msg
        or "timeout" in msg
        or "gateway time-out" in msg
        or "origin_response_timeout" in msg
    )


def _generate_single_pass_report(
    llm,
    metas: list[dict],
    *,
    content_level: str,
    max_full_post_chars: int,
    period_dates: str | None,
    prompts_out: list[str] | None = None,
) -> str:
    """One LLM call: all posts; prompt asks for a categorized markdown digest."""
    blocks = "\n\n".join(
        _format_post_for_prompt(m, content_level, max_full_post_chars) for m in metas
    )
    header = f"Period: {period_dates}\n\n" if period_dates else ""
    prompt = f"{header}Posts ({len(metas)} total):\n\n{blocks}"
    if prompts_out is not None:
        prompts_out.append(prompt)
    response = llm.invoke(prompt, system_instruction=_SINGLE_PASS_SYSTEM)
    return _llm_response_text(response)


def _resolve_max_posts(max_posts: int | None, content_level: str) -> int:
    """Use max_posts if set, else default for content level."""
    if max_posts is not None and max_posts > 0:
        return int(max_posts)
    return _default_max_posts(content_level)


def build_report_signature(
    metas: list[dict],
    *,
    report_mode: str,
    content_level: str,
    max_posts: int | None,
    max_full_post_chars: int,
    report_provider: str | None,
    report_model: str | None,
    period: str,
) -> ReportSignature:
    """Signature from already-loaded post metas (no CSV re-scan)."""
    limit = _resolve_max_posts(max_posts, content_level)
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
    return build_report_signature(
        metas,
        report_mode=report_mode,
        content_level=content_level,
        max_posts=max_posts,
        max_full_post_chars=max_full_post_chars,
        report_provider=report_provider,
        report_model=report_model,
        period=period or "7d",
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
    return {
        "model_id": sig[0],
        "n": sig[1],
        "summarized_at": list(sig[2]),
        "report_mode": sig[3],
        "content_level": sig[4],
        "max_posts": sig[5],
        "max_full_post_chars": sig[6],
        "period": sig[7],
    }


def _sig_to_cache_key(sig: ReportSignature) -> str:
    """Canonical string key for O(1) dict lookup. Stable JSON serialization."""
    return json.dumps(_sig_to_key(sig), sort_keys=True)


def _key_matches(key: dict, sig: ReportSignature) -> bool:
    """True if key matches signature."""
    return (
        key.get("model_id") == sig[0]
        and key.get("n") == sig[1]
        and tuple(key.get("summarized_at", [])) == sig[2]
        and key.get("report_mode") == sig[3]
        and key.get("content_level") == sig[4]
        and key.get("max_posts") == sig[5]
        and key.get("max_full_post_chars") == sig[6]
        and key.get("period", "7d") == sig[7]
    )


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
        if len(prompts_dict) > max_entries:
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
        if len(reports_dict) > max_entries:
            loser = min(reports_dict, key=lambda k: reports_dict[k].get("hits", 0))
            del reports_dict[loser]
        data["reports"] = reports_dict
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _check_run_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel and should_cancel():
        raise PipelineCancelledError()


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
            logger.info("Loaded GCP credentials to %s (0600)", creds_path)
            return str(project_id) if project_id else None
        except (OSError, json.JSONDecodeError) as e:
            try:
                os.unlink(creds_path)
            except OSError:
                pass
            logger.warning("Failed to write credentials file: %s", e)
            return None
    except json.JSONDecodeError as e:
        logger.warning("Failed to parse GOOGLE_APPLICATION_CREDENTIALS_JSON: %s", e)
        return None


@dataclass(frozen=True)
class ReportProgress:
    label: str
    frac: float


@dataclass(frozen=True)
class ReportComplete:
    text: str
    signature: ReportSignature | None


def _generate_per_category_events(
    metas: list[dict],
    *,
    content_level: str,
    max_full_post_chars: int,
    llm,
    period_dates: str | None,
    sig: ReportSignature | None,
    should_cancel: Callable[[], bool] | None,
) -> Iterator[ReportProgress | ReportComplete]:
    """Per-category report: one 2–4 sentence summary batch per category section."""
    by_category = _group_metas_by_category(metas)
    cat_batches: dict[str, list[list[dict]]] = {}
    for cat in REPORT_CATEGORIES:
        category_metas = by_category.get(cat)
        if not category_metas or cat == "other":
            continue
        cat_batches[cat] = _batches_by_char_limit(
            category_metas,
            REPORT_BATCH_CHAR_LIMIT,
            content_level,
            max_full_post_chars,
        )
    total_batches = sum(len(batches) for batches in cat_batches.values())
    if by_category.get("other"):
        total_batches += 1

    prompts_collected: list[str] = []
    parts: list[str] = []
    batch_idx = 0

    for cat in REPORT_CATEGORIES:
        category_metas = by_category.get(cat)
        if not category_metas:
            continue
        label = CATEGORY_LABELS.get(cat, cat.replace("_", " ").title())
        if cat == "other":
            _check_run_cancelled(should_cancel)
            parts.append(
                f"## {label}\n\n"
                f"{_format_other_section(category_metas, content_level, max_full_post_chars)}"
            )
            batch_idx += 1
            if total_batches:
                yield ReportProgress(f"report: {label}", batch_idx / total_batches)
            continue

        section_parts: list[str] = []
        for batch in cat_batches[cat]:
            _check_run_cancelled(should_cancel)
            batch_idx += 1
            if total_batches:
                yield ReportProgress(
                    f"report: {label} [{batch_idx}/{total_batches}]",
                    batch_idx / total_batches,
                )
            section_parts.append(
                _summarize_report_batch(
                    llm,
                    batch,
                    label,
                    content_level,
                    max_full_post_chars=max_full_post_chars,
                    prompts_out=prompts_collected,
                    period_dates=period_dates,
                )
            )
        parts.append(f"## {label}\n\n" + "\n\n".join(section_parts))

    if sig is not None and prompts_collected:
        _save_report_prompt_debug("per-category", _BATCH_SYSTEM, prompts_collected, sig)

    if not parts:
        yield ReportComplete("No posts to summarize.", sig)
        return
    intro = f"_Period: {period_dates}_\n\n" if period_dates else ""
    yield ReportComplete(intro + "\n\n".join(parts), sig)


def _generate_single_pass_events(
    metas: list[dict],
    *,
    content_level: str,
    max_full_post_chars: int,
    llm,
    period_dates: str | None,
    sig: ReportSignature | None,
    should_cancel: Callable[[], bool] | None,
) -> Iterator[ReportProgress | ReportComplete]:
    """Single-pass report: one LLM call with all posts."""
    if not metas:
        yield ReportComplete("No posts to summarize.", sig)
        return
    _check_run_cancelled(should_cancel)
    yield ReportProgress("report: generating (single pass)…", 0.0)
    prompts_collected: list[str] = []
    text = _generate_single_pass_report(
        llm,
        metas,
        content_level=content_level,
        max_full_post_chars=max_full_post_chars,
        period_dates=period_dates,
        prompts_out=prompts_collected,
    )
    if sig is not None and prompts_collected:
        _save_report_prompt_debug(
            "single-pass", _SINGLE_PASS_SYSTEM, prompts_collected, sig
        )
    intro = f"_Period: {period_dates}_\n\n" if period_dates else ""
    yield ReportComplete(intro + text, sig)


def _generate_report_events(
    metas: list[dict],
    *,
    report_mode: str,
    content_level: str,
    max_full_post_chars: int,
    report_provider: str | None,
    report_model: str | None,
    period_dates: str | None,
    sig: ReportSignature | None,
    should_cancel: Callable[[], bool] | None,
) -> Iterator[ReportProgress | ReportComplete]:
    """Stream report progress + final text; mode selects single-pass vs per-category."""
    llm = create_llm(
        stage="report",
        json_mode=False,
        provider_override=report_provider,
        model_override=report_model,
    )
    if report_mode == REPORT_MODE_SINGLE_PASS:
        yield from _generate_single_pass_events(
            metas,
            content_level=content_level,
            max_full_post_chars=max_full_post_chars,
            llm=llm,
            period_dates=period_dates,
            sig=sig,
            should_cancel=should_cancel,
        )
        return
    yield from _generate_per_category_events(
        metas,
        content_level=content_level,
        max_full_post_chars=max_full_post_chars,
        llm=llm,
        period_dates=period_dates,
        sig=sig,
        should_cancel=should_cancel,
    )


def generate_report_events(
    *,
    report_mode: str = REPORT_MODE_PER_CATEGORY,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_posts: int | None = None,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    report_provider: str | None = None,
    report_model: str | None = None,
    period: str = "7d",
    activities_csv_path: Path | None = None,
    metas: list[dict] | None = None,
    period_dates: str | None = None,
    signature: ReportSignature | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> Iterator[ReportProgress | ReportComplete]:
    """Load posts (unless metas provided) and stream report progress + final text."""
    setup_gcp_credentials()
    csv_path = activities_csv_path or get_default_csv_path()
    if metas is None:
        limit = _resolve_max_posts(max_posts, content_level)
        metas, period_dates = _get_posts_for_period(
            period or "7d", limit, csv_path=csv_path
        )
    if not metas:
        yield ReportComplete(
            "No summarized posts found. Run the pipeline first (collect → enrich → summarize).",
            None,
        )
        return
    sig = signature or build_report_signature(
        metas,
        report_mode=report_mode,
        content_level=content_level,
        max_posts=max_posts,
        max_full_post_chars=max_full_post_chars,
        report_provider=report_provider,
        report_model=report_model,
        period=period or "7d",
    )
    try:
        yield from _generate_report_events(
            metas,
            report_mode=report_mode,
            content_level=content_level,
            max_full_post_chars=max_full_post_chars,
            report_provider=report_provider,
            report_model=report_model,
            period_dates=period_dates,
            sig=sig,
            should_cancel=should_cancel,
        )
    except PipelineCancelledError:
        raise
    except Exception as e:
        logger.exception("Report generation failed")
        yield ReportComplete(_report_error_message(e), None)


def generate_activity_report(
    report_mode: str = REPORT_MODE_PER_CATEGORY,
    content_level: str = CONTENT_LEVEL_SUMMARY,
    max_posts: int | None = None,
    max_full_post_chars: int = REPORT_MAX_FULL_POST_CHARS_DEFAULT,
    report_provider: str | None = None,
    report_model: str | None = None,
    period: str = "7d",
    activities_csv_path: Path | None = None,
    progress_callback: Callable[[str, float], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> str:
    """Blocking report generation. Prefer generate_report_events in the Gradio UI."""
    result = ""
    for event in generate_report_events(
        report_mode=report_mode,
        content_level=content_level,
        max_posts=max_posts,
        max_full_post_chars=max_full_post_chars,
        report_provider=report_provider,
        report_model=report_model,
        period=period,
        activities_csv_path=activities_csv_path,
        should_cancel=should_cancel,
    ):
        if isinstance(event, ReportProgress):
            if progress_callback:
                progress_callback(event.label, event.frac)
        else:
            result = event.text
    return result


def _report_error_message(e: Exception) -> str:
    """Turn an exception into a short, UI-safe message (no HTML)."""
    if isinstance(e, PipelineCancelledError):
        return "_Run stopped._"
    msg = str(e)
    if _is_llm_timeout_error(e):
        return (
            "❌ The LLM provider timed out (proxy limit, often ~120s per request). "
            "Your pipeline data is cached — rerun with **Skip fetch** to retry only the report. "
            "Try **Per category** mode, a shorter period, or a lower **Max posts** limit."
        )
    if any(tag in msg for tag in ("<!DOCTYPE", "<html", "<span", "<div")):
        return "❌ The LLM provider returned an error page. Try again later or check your API/network."
    return f"❌ Error: {msg[:200]}"
