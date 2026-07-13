#!/usr/bin/env python3
"""Summarize LinkedIn posts with LLM (Explorer-style prompts)."""

from __future__ import annotations

import warnings
from typing import Any

from linkedin_api.content_store import (
    list_posts_for_summary,
    load_metadata,
    update_summary_metadata,
)
from linkedin_api.llm_config import create_llm, get_summary_model_id
from linkedin_api.summary_text import (
    POST_SYSTEM_PROMPT,
    build_post_user_prompt,
    parse_summary_response,
    truncate,
)
from linkedin_api.topic_tags import topics_to_catalog_tags

BATCH_SIZE = 5
_MAX_POST_CHARS = 2000


def _summarize_one(post: dict[str, Any], llm, *, model_id: str) -> bool:
    urn = str(post.get("urn") or "")
    if not urn:
        return False
    meta = load_metadata(urn) or {}
    user_prompt = build_post_user_prompt(
        content=truncate(str(post.get("content") or ""), _MAX_POST_CHARS),
        post_author=str(meta.get("post_author") or ""),
        post_url=str(meta.get("post_url") or ""),
    )
    try:
        response = llm.invoke(user_prompt, system_instruction=POST_SYSTEM_PROMPT)
        content = response.content if hasattr(response, "content") else str(response)
        parsed = parse_summary_response(content)
        if not parsed.ok:
            return False
        catalog_tags = topics_to_catalog_tags(parsed.topics, llm=llm, quiet=True)
        update_summary_metadata(
            urn,
            summary=parsed.summary_text,
            topics=parsed.topics,
            technologies=parsed.technologies,
            people=parsed.people,
            category=parsed.category or None,
            tldr=parsed.tldr,
            summary_bullets=parsed.bullets,
            summary_model=model_id,
            tags=catalog_tags,
        )
        return True
    except Exception as exc:
        print(f"  LLM error ({urn}): {exc}")
        return False


def summarize_posts(
    *,
    limit: int | None = None,
    batch_size: int = BATCH_SIZE,
    quiet: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    force_resummarize: bool = False,
    urns: set[str] | None = None,
) -> int:
    """Summarize posts. Returns count summarized."""
    posts = list_posts_for_summary(limit=limit, force=force_resummarize, urns=urns)
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
        json_mode=False,
    )
    model_id = get_summary_model_id(llm_provider, llm_model)
    total = 0
    it = tqdm(posts, desc="Summarize posts", unit="post", disable=quiet)
    for post in it:
        if _summarize_one(post, llm, model_id=model_id):
            total += 1
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
    force_resummarize: bool = False,
    urns: set[str] | None = None,
):
    """Generator variant; yields (done, total) after each post."""
    del batch_size
    posts = list_posts_for_summary(limit=limit, force=force_resummarize, urns=urns)
    if not posts:
        return 0
    llm = create_llm(
        quiet=quiet,
        stage="summary",
        provider_override=llm_provider,
        model_override=llm_model,
        json_mode=False,
    )
    model_id = get_summary_model_id(llm_provider, llm_model)
    total = len(posts)
    done = 0
    summarized = 0
    for post in posts:
        if _summarize_one(post, llm, model_id=model_id):
            summarized += 1
        done += 1
        yield done, total
    return summarized


def clear_post_summaries(*, limit: int | None = None) -> int:
    """Remove LLM summary fields so the next run will resummarize."""
    posts = list_posts_for_summary(limit=limit, force=True)
    cleared = 0
    for post in posts:
        urn = str(post.get("urn") or "")
        if not urn:
            continue
        update_summary_metadata(
            urn,
            summary="",
            topics=[],
            technologies=[],
            people=[],
            category="",
            tldr="",
            summary_bullets=[],
            summary_model="",
        )
        cleared += 1
    return cleared


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Summarize posts via LLM (Phase 3).")
    parser.add_argument("--limit", type=int, help="Max posts to process")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument(
        "--force-resummarize",
        action="store_true",
        help="Re-run LLM even when TLDR and summary are already complete",
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    args = parser.parse_args()
    n = summarize_posts(
        limit=args.limit,
        batch_size=args.batch_size,
        quiet=args.quiet,
        force_resummarize=args.force_resummarize,
    )
    if not args.quiet:
        print(f"Summarized {n} posts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
