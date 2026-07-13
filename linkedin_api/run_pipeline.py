#!/usr/bin/env python3
"""CLI entry point: collect → enrich → fetch linked URLs → summarize."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable
from io import StringIO
from types import SimpleNamespace

from linkedin_api.enrich_activities import enrich_activities_streaming
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.fetch_linked_content import fetch_linked_content_streaming
from linkedin_api.pipeline import (
    PipelineOptions,
    collect_period,
    enrich_records,
    run_pipeline,
    summarize_records,
)
from linkedin_api.summarize_posts import summarize_posts_streaming


def _args_to_options(args) -> PipelineOptions:
    return PipelineOptions(
        last=args.last or "30d",
        from_cache=args.from_cache,
        limit=args.limit,
        batch_size=args.batch_size,
        quiet=args.quiet,
        force_resummarize=getattr(args, "force_resummarize", False),
    )


def _collect_activities(args) -> tuple[list[EnrichedRecord], int]:
    return collect_period(_args_to_options(args))


def _enrich_activities(activities: list[EnrichedRecord], args) -> int:
    return enrich_records(activities, limit=args.limit, quiet=args.quiet)


def _summarize_posts(args) -> int:
    return summarize_records(_args_to_options(args))


def _enrich_activities_streaming(activities: list[EnrichedRecord], args):
    gen = enrich_activities_streaming(activities, limit=args.limit)
    count = 0
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        _, count = e.value
    return count


def _fetch_linked_content_streaming(args, urns: set[str] | None = None):
    gen = fetch_linked_content_streaming(limit=args.limit, skip_cached=True, urns=urns)
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        return e.value or 0


def _summarize_posts_streaming(
    args,
    summary_provider=None,
    summary_model=None,
    *,
    urns: set[str] | None = None,
):
    gen = summarize_posts_streaming(
        limit=args.limit,
        batch_size=args.batch_size,
        quiet=args.quiet,
        llm_provider=summary_provider,
        llm_model=summary_model,
        urns=urns,
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
    """Run pipeline; capture stdout and return (success, log)."""
    opts = PipelineOptions(
        last=last,
        from_cache=from_cache,
        limit=limit,
        batch_size=batch_size,
        quiet=False,
    )
    if not opts.last and not opts.from_cache:
        opts.from_cache = True
        opts.last = "30d"
    out = StringIO()
    old_stdout = sys.stdout
    try:
        sys.stdout = out
        run_pipeline(opts)
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
    """Generator that runs the pipeline and yields user-friendly progress for the UI."""

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

        n3 = 0
        lines.append("Summarizing…")
        gen = _summarize_posts_streaming(
            args,
            summary_provider=summary_provider,
            summary_model=summary_model,
            urns=urns or None,
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
        description="Run pipeline: collect → enrich → fetch linked URLs → summarize."
    )
    parser.add_argument("--last", metavar="Nd", help="Period: 7d, 14d, 30d")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        dest="from_cache",
        help="Use only cached data from activities.csv (no API fetch)",
    )
    parser.add_argument("--limit", type=int, help="Limit posts per phase")
    parser.add_argument(
        "--batch-size", type=int, default=5, help="Summarize batch size"
    )
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument(
        "--force-resummarize",
        action="store_true",
        help="Re-run LLM even when TLDR and summary are already complete",
    )
    args = parser.parse_args()

    opts = _args_to_options(args)
    if not opts.last and not opts.from_cache:
        opts.from_cache = True
        opts.last = "30d"
        if not opts.quiet:
            print("Using --skip-fetch --last 30d (default)")

    try:
        run_pipeline(opts)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:
        print(f"Error: {e}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
