"""Public pipeline API for LinkedIn Portability data collection and enrichment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from linkedin_api.activity_csv import get_default_csv_path
from linkedin_api.period import parse_period
from linkedin_api.activity_extract import (
    extract_activity_records,
    get_all_post_activities,
)
from linkedin_api.enriched_record import EnrichedRecord

logger = logging.getLogger(__name__)


@dataclass
class PipelineOptions:
    last: str = "30d"
    from_cache: bool = False
    limit: int | None = None
    batch_size: int = 5
    quiet: bool = False
    force_resummarize: bool = False


def collect_period(
    options: PipelineOptions,
) -> tuple[list[EnrichedRecord], int]:
    """Collect activities for a period (fetch + CSV load). Returns (records, count)."""
    # Lazy import: eager import breaks ``python -m linkedin_api.summarize_activity``.
    from linkedin_api.summarize_activity import collect_from_csv, ensure_csv_fetched

    last = options.last or "30d"
    start_dt = end_dt = None
    start_ms = parse_period(last)
    if start_ms is None:
        raise ValueError(f"Invalid period '{last}'; use e.g. 7d, 14d, 30d")
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    end_dt = datetime.now(timezone.utc)

    ensure_csv_fetched(last, verbose=not options.quiet, skip_fetch=options.from_cache)
    records = collect_from_csv(
        start=start_dt, end=end_dt, csv_path=get_default_csv_path()
    )
    if not records and options.from_cache:
        raise RuntimeError(
            "No data in activities.csv. Run without --skip-fetch to fetch from API."
        )
    if not options.quiet:
        print(f"Collected {len(records)} activities")
    return records, len(records)


def enrich_records(
    activities: list[EnrichedRecord],
    *,
    limit: int | None = None,
    quiet: bool = False,
) -> int:
    """Enrich activities into the content store. Returns count enriched."""
    # Lazy import: eager import breaks ``python -m linkedin_api.enrich_activities``.
    from linkedin_api.enrich_activities import enrich_activities

    _, count = enrich_activities(activities, limit=limit)
    if not quiet:
        print(f"Enriched {count} activities")
    return count


def fetch_linked_urls(
    options: PipelineOptions,
    urns: set[str] | None = None,
) -> int:
    """Fetch linked URL content for posts in scope. Returns URLs fetched."""
    # Lazy import: eager import breaks ``python -m linkedin_api.fetch_linked_content``.
    from linkedin_api.fetch_linked_content import fetch_linked_content_streaming

    gen = fetch_linked_content_streaming(
        limit=options.limit, skip_cached=True, urns=urns
    )
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value or 0


def summarize_linked_resources(
    options: PipelineOptions,
    *,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    urns: set[str] | None = None,
) -> int:
    """Summarize fetched linked articles lacking LLM summary."""
    # Lazy import: eager import breaks ``python -m linkedin_api.summarize_resources``
    # (and transitively ``python -m linkedin_api.fetch_linked_content``).
    from linkedin_api.summarize_resources import summarize_resources

    n = summarize_resources(
        limit=options.limit,
        quiet=options.quiet,
        llm_provider=llm_provider,
        llm_model=llm_model,
        force_resummarize=options.force_resummarize,
        urns=urns,
    )
    if not options.quiet:
        if n == 0:
            print("Summarized 0 linked articles.")
        else:
            print(f"Summarized {n} linked articles.")
    return n


def summarize_records(
    options: PipelineOptions,
    *,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    urns: set[str] | None = None,
) -> int:
    """Summarize posts lacking summary metadata. Returns count summarized."""
    # Lazy import: eager import breaks ``python -m linkedin_api.summarize_posts``.
    from linkedin_api.summarize_posts import summarize_posts

    n = summarize_posts(
        limit=options.limit,
        batch_size=options.batch_size,
        quiet=options.quiet,
        llm_provider=llm_provider,
        llm_model=llm_model,
        force_resummarize=options.force_resummarize,
        urns=urns,
    )
    if not options.quiet:
        if n == 0:
            print("Summarized 0 posts.")
        else:
            print(f"Summarized {n} posts.")
    return n


def run_pipeline(
    options: PipelineOptions | None = None,
    *,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> tuple[list[EnrichedRecord], dict[str, int]]:
    """Run collect → enrich → fetch linked URLs → summarize. Returns activities + stats."""
    opts = options or PipelineOptions()
    if not opts.last and not opts.from_cache:
        opts.from_cache = True
        opts.last = "30d"

    activities, collected = collect_period(opts)
    enriched = enrich_records(activities, limit=opts.limit, quiet=opts.quiet)
    fetch_scope = {rec.post_id for rec in activities if rec.post_id}
    urls_fetched = fetch_linked_urls(opts, urns=fetch_scope or None)
    from linkedin_api.summarize_resources import summary_scope_for_activities

    summary_scope = summary_scope_for_activities(activities)
    if not opts.quiet and summary_scope:
        print(f"Summarize scope: {len(summary_scope)} post keys from period")
    summarized = summarize_records(
        opts,
        llm_provider=llm_provider,
        llm_model=llm_model,
        urns=summary_scope,
    )
    articles_summarized = summarize_linked_resources(
        opts,
        llm_provider=llm_provider,
        llm_model=llm_model,
        urns=summary_scope,
    )
    stats = {
        "collected": collected,
        "enriched": enriched,
        "urls_fetched": urls_fetched,
        "summarized": summarized,
        "articles_summarized": articles_summarized,
    }
    return activities, stats


__all__ = [
    "PipelineOptions",
    "collect_period",
    "enrich_records",
    "extract_activity_records",
    "fetch_linked_urls",
    "get_all_post_activities",
    "parse_period",
    "run_pipeline",
    "summarize_linked_resources",
    "summarize_records",
]
