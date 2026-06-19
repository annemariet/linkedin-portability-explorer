#!/usr/bin/env python3
"""
Run the full MVP pipeline: collect → enrich → summarize.

Processes new data and backfills history (posts in store not yet summarized).

Incremental: Running 7d then 30d avoids recomputing. Phase 1 reads the period slice
from activities.csv. Phase 2 enriches into the content store (.md + .meta.json).
Phase 3 LLM-summarizes posts that lack summary metadata.
"""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace

from linkedin_api.enrich_activities import (
    enrich_activities,
    enrich_activities_streaming,
)
from linkedin_api.fetch_linked_content import fetch_linked_content_streaming
from linkedin_api.activity_csv import get_default_csv_path
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.summarize_activity import collect_from_csv, ensure_csv_fetched
from linkedin_api.summarize_posts import summarize_posts, summarize_posts_streaming
from linkedin_api.vault_export import run_vault_export_if_enabled


def _collect_activities(args) -> tuple[list[EnrichedRecord], int]:
    """Collect from CSV (fetch + append when not skip-fetch). Returns (activities, count)."""
    from datetime import datetime, timezone

    from linkedin_api.summarize_activity import _parse_last

    last = args.last or "30d"
    start_dt = None
    end_dt = None
    if last:
        start_ms = _parse_last(last)
        if start_ms is None:
            raise ValueError(f"Invalid --last '{last}'; use e.g. 7d, 14d, 30d")
        start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
        end_dt = datetime.now(timezone.utc)

    ensure_csv_fetched(last, verbose=not args.quiet, skip_fetch=args.from_cache)

    records = collect_from_csv(
        start=start_dt, end=end_dt, csv_path=get_default_csv_path()
    )
    if not records and args.from_cache:
        raise SystemExit(
            'No data in activities.csv. Run extract_graph_data or use without "Skip fetch".'
        )

    if not args.quiet:
        print(f"Collected {len(records)} activities")

    return records, len(records)


def _enrich_activities(activities: list[EnrichedRecord], args) -> int:
    """Enrich activities into the content store. Returns count enriched."""
    _, count = enrich_activities(activities, limit=args.limit)
    if not args.quiet:
        print(f"Enriched {count} activities")
    return count


def _summarize_posts(args):
    """Summarize posts in store that lack a summary (via LLM)."""
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
    return n


def _enrich_activities_streaming(activities: list[EnrichedRecord], args):
    """
    Generator variant of _enrich_activities.
    Yields (done, total) per activity. Returns count via StopIteration.
    """
    gen = enrich_activities_streaming(activities, limit=args.limit)
    count = 0
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        _, count = e.value
    return count


def _fetch_linked_content_streaming(args, urns: set[str] | None = None):
    """
    Generator: fetch content from URLs linked in posts. Yields (done, total).
    Returns urls_fetched via StopIteration.

    ``urns`` restricts processing to posts in the current activity period.
    """
    gen = fetch_linked_content_streaming(limit=args.limit, skip_cached=True, urns=urns)
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        return e.value or 0


def _summarize_posts_streaming(args, summary_provider=None, summary_model=None):
    """
    Generator variant of _summarize_posts.
    Yields (batches_done, total_batches) per batch. Returns total via StopIteration.
    """
    gen = summarize_posts_streaming(
        limit=args.limit,
        batch_size=args.batch_size,
        quiet=args.quiet,
        llm_provider=summary_provider,
        llm_model=summary_model,
    )
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        return e.value or 0


def run_pipeline_ui(
    last: str = "7d",
    from_cache: bool = False,
    limit: int | None = None,
    batch_size: int = 5,
) -> tuple[bool, str]:
    """
    Run the MVP pipeline with given options; capture stdout and return (success, log).

    For use from Gradio or other UIs. Does not call sys.exit.
    """
    args = SimpleNamespace(
        last=last,
        from_cache=from_cache,
        limit=limit,
        batch_size=batch_size,
        quiet=False,
    )
    if not args.last and not args.from_cache:
        args.from_cache = True
        args.last = "30d"
    out = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = out
        activities, _ = _collect_activities(args)
        _enrich_activities(activities, args)
        urns = {rec.post_urn for rec in activities if rec.post_urn}
        for _ in _fetch_linked_content_streaming(args, urns=urns):
            pass  # exhaust generator
        _summarize_posts(args)
        return True, out.getvalue()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return code == 0, out.getvalue()
    except Exception as e:
        traceback.print_exc(file=out)
        print(f"Error: {e}", file=out)
        return False, out.getvalue()
    finally:
        sys.stdout = old_stdout


def run_pipeline_ui_streaming(
    last: str = "7d",
    from_cache: bool = False,
    limit: int | None = None,
    batch_size: int = 5,
    summary_provider: str | None = None,
    summary_model: str | None = None,
    should_cancel: Callable[[], bool] | None = None,
):
    """
    Generator that runs the MVP pipeline and yields user-friendly progress for the UI.

    Full technical output goes to the terminal (stdout). Yields only short progress
    lines so the UI stays readable.
    """

    def _cancelled() -> bool:
        return bool(should_cancel and should_cancel())

    args = SimpleNamespace(
        last=last,
        from_cache=from_cache,
        limit=limit,
        batch_size=batch_size,
        quiet=False,
    )
    if not args.last and not args.from_cache:
        args.from_cache = True
        args.last = "30d"
    lines: list[str] = []

    def _snapshot() -> str:
        return "\n".join(lines)

    def _add(msg: str) -> str:
        lines.append(msg)
        return _snapshot()

    try:
        yield _add("Starting pipeline…")
        if _cancelled():
            yield _add("⏹ Stopped.")
            return
        activities, n1 = _collect_activities(args)
        yield _add(f"Collected {n1} activities.")

        # Enrich with per-activity progress (placeholder updated in-place)
        n2 = 0
        lines.append("Enriching…")
        gen = _enrich_activities_streaming(activities, args)
        try:
            while True:
                if _cancelled():
                    lines[-1] = "Enriching stopped."
                    yield _snapshot()
                    return
                done, total = next(gen)
                lines[-1] = f"Enriching {done}/{total}…"
                yield _snapshot()
        except StopIteration as e:
            n2 = e.value
        if _cancelled():
            yield _add("⏹ Stopped.")
            return
        lines[-1] = f"Enriched {n2} activities."
        yield _snapshot()

        # Fetch linked URL content (posts with urls in metadata)
        urns = {rec.post_urn for rec in activities if rec.post_urn}
        n_urls = 0
        lines.append("Fetching linked URLs…")
        gen = _fetch_linked_content_streaming(args, urns=urns)
        try:
            while True:
                if _cancelled():
                    lines[-1] = "Fetching linked URLs stopped."
                    yield _snapshot()
                    return
                done, total = next(gen)
                lines[-1] = f"Fetching linked URLs {done}/{total}…"
                yield _snapshot()
        except StopIteration as e:
            n_urls = e.value or 0
        if _cancelled():
            yield _add("⏹ Stopped.")
            return
        lines[-1] = f"Fetched {n_urls} URL(s) from linked posts."
        yield _snapshot()

        # Summarize with per-batch progress (placeholder updated in-place)
        n3 = 0
        lines.append("Summarizing…")
        gen = _summarize_posts_streaming(
            args,
            summary_provider=summary_provider,
            summary_model=summary_model,
        )
        try:
            while True:
                if _cancelled():
                    lines[-1] = "Summarizing stopped."
                    yield _snapshot()
                    return
                batches_done, total_batches = next(gen)
                lines[-1] = f"Summarizing batch {batches_done}/{total_batches}…"
                yield _snapshot()
        except StopIteration as e:
            n3 = e.value or 0
        if _cancelled():
            yield _add("⏹ Stopped.")
            return
        lines[-1] = f"Summarized {n3} posts."
        yield _snapshot()

        yield _add("✅ Done.")
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        yield _add(f"❌ Failed (exit {code}).")
    except Exception as e:
        traceback.print_exc()
        yield _add(f"❌ Failed: {e}")


def main() -> int:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("linkedin_api.fetch_linked_content").setLevel(logging.INFO)

    parser = argparse.ArgumentParser(
        description="Run MVP pipeline: collect → enrich → summarize (including history)."
    )
    parser.add_argument("--last", metavar="Nd", help="Period: 7d, 14d, 30d")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        dest="from_cache",
        help="Use only cached data from activities.csv (no API fetch)",
    )
    parser.add_argument("--limit", type=int, help="Limit posts per phase")
    parser.add_argument("--batch-size", type=int, default=5, help="Phase 3 batch size")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument(
        "--output-root",
        metavar="PATH",
        help="Knowledge vault root for catalog writes (implies vault export when set)",
    )
    args = parser.parse_args()

    if not args.last and not args.from_cache:
        args.from_cache = True
        args.last = "30d"
        if not args.quiet:
            print("Using --skip-fetch --last 30d (default)")

    try:
        activities, _ = _collect_activities(args)
        _enrich_activities(activities, args)
        urns = {rec.post_urn for rec in activities if rec.post_urn}
        for done, total in _fetch_linked_content_streaming(args, urns=urns):
            if not args.quiet:
                print(f"\rFetching linked URLs {done}/{total}…", end="", flush=True)
        if not args.quiet:
            print()  # newline after progress
        _summarize_posts(args)
        run_vault_export_if_enabled(
            activities,
            output_root=getattr(args, "output_root", None),
            quiet=args.quiet,
        )
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
