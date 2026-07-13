#!/usr/bin/env python3
"""
Phase 3: Summarize posts with LLM.

Reads from content store (populated by enrich_activities). Batches posts,
sends to LLM, extracts summary, topics, technologies, people, category.
Output written to metadata sidecar (.meta.json).
"""

from __future__ import annotations

import argparse
import json
import re
import warnings

from linkedin_api.content_store import (
    list_posts_needing_summary,
    update_summary_metadata,
)
from linkedin_api.llm_config import create_llm

BATCH_SIZE = 5

_SYSTEM_PROMPT = """You extract structured metadata from LinkedIn posts. For each post provide:
- summary: 1-2 sentence summary
- topics: list of main topics/themes (e.g. ["AI", "careers"])
- technologies: tools, frameworks, languages mentioned (e.g. ["Python", "PyTorch"])
- people: named people or roles mentioned (e.g. ["Jane Doe", "CTO"])
- category: one of product_announcement, paper, experiment, job_news, opinion, tutorial, other.

Example categories you can pick from: product_announcement (new lib/product), paper (academic/research),
  experiment (trial/benchmark), job_news (hiring/career), opinion (hot take),
  tutorial (how-to), other.
Use empty arrays [] for topics/technologies/people when none apply.
Output valid JSON only. Format: {"posts": [{"urn": "...", "summary": "...",
  "topics": [], "technologies": [], "people": [], "category": "..."}]}"""

_USER_PROMPT_TEMPLATE = """For each post below: write a 1-2 sentence summary and fill in
topics, technologies, people, and category as relevant. Output JSON only.

---
{posts}
---
"""


def _truncate(content: str, max_chars: int = 2000) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "\n...[truncated]"


def _build_prompt_batch(posts: list[dict]) -> str:
    parts = []
    for i, p in enumerate(posts, 1):
        urn = p.get("urn", "")
        content = _truncate(p.get("content", ""))
        parts.append(f"[Post {i}]\nURN: {urn}\nContent:\n{content}\n")
    return "\n".join(parts)


def _parse_llm_response(text: str, urns: list[str]) -> list[dict]:
    """Extract JSON from LLM output. urns used to match back to posts."""
    text = text.strip()
    # Try to find JSON block
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return []
    try:
        data = json.loads(match.group())
        posts = data.get("posts", data) if isinstance(data, dict) else data
        if not isinstance(posts, list):
            return []
        result = []
        for i, p in enumerate(posts[: len(urns)]):
            if isinstance(p, dict):
                urn = p.get("urn") or (urns[i] if i < len(urns) else "")
                result.append(
                    {
                        "urn": urn,
                        "summary": str(p.get("summary", "")).strip(),
                        "topics": [str(x) for x in (p.get("topics") or []) if x],
                        "technologies": [
                            str(x) for x in (p.get("technologies") or []) if x
                        ],
                        "people": [str(x) for x in (p.get("people") or []) if x],
                        "category": str(p.get("category", "")).strip() or None,
                    }
                )
        return result
    except json.JSONDecodeError:
        return []


def _summarize_batch(posts: list[dict], llm) -> int:
    """Summarize one batch. Returns count updated."""
    user_prompt = _USER_PROMPT_TEMPLATE.format(posts=_build_prompt_batch(posts))
    urns = [p.get("urn") or "" for p in posts]
    post_id_by_urn = {p.get("urn") or "": p.get("post_id") or "" for p in posts}
    try:
        response = llm.invoke(user_prompt, system_instruction=_SYSTEM_PROMPT)
        content = response.content if hasattr(response, "content") else str(response)
        parsed = _parse_llm_response(content, urns)
        for p in parsed:
            urn = p["urn"]
            post_id = post_id_by_urn.get(urn, "")
            if post_id:
                update_summary_metadata(
                    post_id,
                    summary=p["summary"],
                    topics=p["topics"],
                    technologies=p["technologies"],
                    people=p["people"],
                    category=p.get("category"),
                    post_urn=urn,
                )
        return len(parsed)
    except Exception as e:
        print(f"  LLM error: {e}")
        return 0


def summarize_posts(
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    quiet: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> int:
    """Summarize posts. Returns count summarized."""
    posts = list_posts_needing_summary(limit=limit)
    if not posts:
        if not quiet:
            print("No posts needing summary.")
        return 0
    from tqdm import tqdm

    llm = create_llm(
        quiet=quiet,
        stage="summary",
        provider_override=llm_provider,
        model_override=llm_model,
    )
    total = 0
    batches = [posts[i : i + batch_size] for i in range(0, len(posts), batch_size)]
    it = tqdm(batches, desc="Summarize", unit="batch", disable=quiet)
    for batch in it:
        n = _summarize_batch(batch, llm)
        total += n
        it.set_postfix(done=total)
    if total == 0 and not quiet:
        warnings.warn(
            "No posts were summarized (LLM errors?). Check LLM_MODEL and API key."
        )
    return total


def summarize_posts_streaming(
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    quiet: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
):
    """
    Generator variant of summarize_posts.
    Yields (batches_done, total_batches) after each batch.
    Returns total posts summarized via StopIteration.value.
    """
    posts = list_posts_needing_summary(limit=limit)
    if not posts:
        return 0
    llm = create_llm(
        quiet=quiet,
        stage="summary",
        provider_override=llm_provider,
        model_override=llm_model,
    )
    batches = [posts[i : i + batch_size] for i in range(0, len(posts), batch_size)]
    total_batches = len(batches)
    total = 0
    for i, batch in enumerate(batches):
        n = _summarize_batch(batch, llm)
        total += n
        yield i + 1, total_batches
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize posts via LLM (Phase 3).")
    parser.add_argument("--limit", type=int, help="Max posts to process")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()
    n = summarize_posts(
        limit=args.limit,
        batch_size=args.batch_size,
        quiet=args.quiet,
    )
    if not args.quiet:
        if n == 0:
            print("Summarized 0 posts.")
        else:
            print(f"Summarized {n} posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
