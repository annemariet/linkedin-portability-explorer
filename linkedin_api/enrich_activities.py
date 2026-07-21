#!/usr/bin/env python3
"""
Enrich activities with post content and structured metadata.

- **skip** — ``.meta.json`` has ``enrichment_version`` = current, ``.md`` exists, and
  this row's ``activity_id`` is already in ``activities_ids``.
- **merge** — same version and body on disk, but a **new** CSV line (union
  ``activities_ids``, fill empty ``post_url`` / ``activity_time_iso``).
- **full** — no metadata, missing ``.md``, wrong ``enrichment_version``, or first
  time for this post — GET HTML via :mod:`linkedin_api.post_extraction`.

Set ``ENRICH_TELEMETRY=1`` to print a path-count summary to stderr. INFO logs
include the same counts.

Comments / Playwright are not implemented (see ticket backlog).
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from linkedin_api.activity_csv import get_default_csv_path
from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.content_store import (
    _ms_to_iso,
    has_content,
    load_metadata,
    merge_enrichment_activity,
    resolve_urls_for_metadata,
    save_content,
    save_metadata,
)
from linkedin_api.http_client import fetch_linkedin_post_html
from linkedin_api.post_extraction import (
    ENRICHMENT_VERSION,
    append_missing_resource_urls,
    extract_post_from_html,
    merge_classification_with_api,
    save_extraction_to_store,
)
from linkedin_api.summarize_activity import collect_from_csv
from linkedin_api.utils.linkedin_snowflake import post_created_at_from_urn
from linkedin_api.utils.urls import (
    extract_urls_from_text,
    is_comment_feed_url,
)

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentTelemetry:
    """Counts per path. Every row in a run increments exactly one of these,
    so they should always sum to the number of rows processed — a mismatch
    means some path is silently dropping rows uncounted."""

    skip_already_complete: int = 0
    skip_missing_urn_or_url: int = 0
    merge_activity_only: int = 0
    merge_noop: int = 0
    full_html_success: int = 0
    fallback_extract_fail_post_body: int = 0
    fallback_extract_fail_urls_only: int = 0
    fallback_extract_fail_no_content: int = 0
    fallback_http_fail_post_body: int = 0
    fallback_http_fail_urls_only: int = 0
    fallback_http_fail_no_content: int = 0

    def total(self) -> int:
        return sum(vars(self).values())

    def log_summary(self) -> None:
        msg = (
            "enrich telemetry:\n"
            f"  skip_already_complete={self.skip_already_complete}\n"
            f"  skip_missing_urn_or_url={self.skip_missing_urn_or_url}\n"
            f"  merge_activity_only={self.merge_activity_only}\n"
            f"  merge_noop={self.merge_noop}\n"
            f"  full_html_success={self.full_html_success}\n"
            f"  fallback_extract_fail_post_body={self.fallback_extract_fail_post_body}\n"
            f"  fallback_extract_fail_urls_only={self.fallback_extract_fail_urls_only}\n"
            f"  fallback_extract_fail_no_content={self.fallback_extract_fail_no_content}\n"
            f"  fallback_http_fail_post_body={self.fallback_http_fail_post_body}\n"
            f"  fallback_http_fail_urls_only={self.fallback_http_fail_urls_only}\n"
            f"  fallback_http_fail_no_content={self.fallback_http_fail_no_content}\n"
            f"  total={self.total()}"
        )
        logger.info(msg)
        if os.environ.get("ENRICH_TELEMETRY", "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            print(msg, flush=True)


def _meta_version(meta: dict | None) -> int:
    if not meta:
        return 0
    try:
        return int(meta.get("enrichment_version") or 0)
    except (TypeError, ValueError):
        return 0


def _activity_id_in_meta(meta: dict, activity_id: str) -> bool:
    aid = (activity_id or "").strip()
    if not aid:
        return True
    raw = meta.get("activities_ids") or []
    if not isinstance(raw, list):
        raw = [str(raw)] if raw else []
    ids = {str(x) for x in raw if x}
    return aid in ids


def _row_needs_work(rec: EnrichedRecord) -> tuple[str, dict | None]:
    """
    ``skip`` | ``merge`` | ``full`` — see module docstring.
    """
    post_id = rec.post_id
    post_urn = rec.post_urn
    meta = load_metadata(post_id, post_urn=post_urn)
    if meta is None:
        return "full", None

    if not has_content(post_id, post_urn=post_urn):
        return "full", meta

    if _meta_version(meta) != ENRICHMENT_VERSION:
        return "full", meta

    if _activity_id_in_meta(meta, rec.activity_id or ""):
        return "skip", meta

    return "merge", meta


def _save_from_api_fallback(
    rec: EnrichedRecord,
    post_id: str,
    post_urn: str,
    url: str,
    post_created: str | None,
    *,
    telemetry: EnrichmentTelemetry,
    reason: str,
) -> bool:
    """CSV/API-only when HTML unusable. Returns True if wrote metadata."""
    urls_from_api = rec.urls
    api_body = (rec.content or "").strip()
    api_urls = list(dict.fromkeys(urls_from_api))

    if rec.interaction_type == "post" and len(api_body) >= 50:
        u, m, t = merge_classification_with_api([], [], [], api_urls)
        u, m, t = merge_classification_with_api(
            u, m, t, extract_urls_from_text(api_body)
        )
        meta_urls = resolve_urls_for_metadata(u)
        body = append_missing_resource_urls(api_body, meta_urls)
        rec.urls = meta_urls
        save_content(post_id, body, post_urn=post_urn)
        save_metadata(
            post_id,
            urls=meta_urls,
            mentions=m,
            tags=t,
            post_url=url,
            post_author="",
            post_author_url="",
            activity_time_iso=_ms_to_iso(
                int(rec.timestamp) if rec.timestamp is not None else None
            ),
            post_created_at=post_created or "",
            post_urn=post_urn,
            activities_ids=[rec.activity_id] if rec.activity_id else [],
            enrichment_version=ENRICHMENT_VERSION,
        )
        if reason == "extract_fail":
            telemetry.fallback_extract_fail_post_body += 1
        else:
            telemetry.fallback_http_fail_post_body += 1
        return True

    if api_urls:
        u, m, t = merge_classification_with_api([], [], [], api_urls)
        meta_urls = resolve_urls_for_metadata(u)
        rec.urls = meta_urls
        save_metadata(
            post_id,
            urls=meta_urls,
            mentions=m,
            tags=t,
            post_url=url,
            post_author="",
            post_author_url="",
            activity_time_iso=_ms_to_iso(
                int(rec.timestamp) if rec.timestamp is not None else None
            ),
            post_created_at=post_created or "",
            post_urn=post_urn,
            activities_ids=[rec.activity_id] if rec.activity_id else [],
            enrichment_version=ENRICHMENT_VERSION,
        )
        if reason == "extract_fail":
            telemetry.fallback_extract_fail_urls_only += 1
        else:
            telemetry.fallback_http_fail_urls_only += 1
        return True

    if reason == "extract_fail":
        telemetry.fallback_extract_fail_no_content += 1
    else:
        telemetry.fallback_http_fail_no_content += 1
    return False


def _apply_html_extraction(
    rec: EnrichedRecord,
    post_id: str,
    post_urn: str,
    url: str,
    html: str,
    final_url: str,
    post_created: str | None,
    telemetry: EnrichmentTelemetry,
) -> bool:
    ext = extract_post_from_html(html, final_url)
    if ext:
        if ext.html_meta.get("post_created_at") and not post_created:
            post_created = ext.html_meta["post_created_at"]
        body, meta_urls = save_extraction_to_store(
            post_id=post_id,
            post_urn=post_urn,
            post_url=url,
            ext=ext,
            urls_from_api=rec.urls,
            activity_time_iso=_ms_to_iso(
                int(rec.timestamp) if rec.timestamp is not None else None
            ),
            post_created=post_created or "",
            activities_ids=[rec.activity_id] if rec.activity_id else [],
        )
        rec.urls = meta_urls
        if not rec.content:
            rec.content = body
        telemetry.full_html_success += 1
        return True

    return _save_from_api_fallback(
        rec,
        post_id,
        post_urn,
        url,
        post_created,
        telemetry=telemetry,
        reason="extract_fail",
    )


def _run_enrichment(to_enrich: list[EnrichedRecord]):
    total = len(to_enrich)
    enriched_count = 0
    tel = EnrichmentTelemetry()

    for i, rec in enumerate(to_enrich):
        post_id = rec.post_id
        post_urn = rec.post_urn
        url = rec.post_url
        logger.info("Enriching %d/%d: %s", i + 1, total, url or post_id or "?")
        if not (post_id and url):
            tel.skip_missing_urn_or_url += 1
            yield i + 1, total
            continue

        mode, existing_meta = _row_needs_work(rec)
        if mode == "skip":
            tel.skip_already_complete += 1
            yield i + 1, total
            continue

        ts_ms = int(rec.timestamp) if rec.timestamp is not None else None
        post_created = (rec.post_created_at or "").strip() or None
        if not post_created:
            post_created = post_created_at_from_urn(post_urn)

        if mode == "merge":
            out = merge_enrichment_activity(
                post_id,
                post_urn=post_urn,
                activity_id=rec.activity_id or "",
                post_url=url,
                activity_time_iso=_ms_to_iso(ts_ms),
            )
            if out is not None:
                tel.merge_activity_only += 1
                enriched_count += 1
            else:
                tel.merge_noop += 1
            yield i + 1, total
            continue

        # --- full enrichment ---
        fetched = fetch_linkedin_post_html(url)
        if fetched:
            html, final_url = fetched
            if _apply_html_extraction(
                rec, post_id, post_urn, url, html, final_url, post_created, tel
            ):
                enriched_count += 1
        elif _save_from_api_fallback(
            rec, post_id, post_urn, url, post_created, telemetry=tel, reason="http_fail"
        ):
            enriched_count += 1

        yield i + 1, total

    return enriched_count, tel


def _activities_to_enrich(
    activities: list[EnrichedRecord],
    *,
    limit: int | None,
) -> list[EnrichedRecord]:
    rows = [
        a
        for a in activities
        if a.post_id
        and a.post_url
        and not is_comment_feed_url(a.post_url)
        and _row_needs_work(a)[0] != "skip"
    ]
    if limit:
        return rows[:limit]
    return rows


def enrich_activities(
    activities: list[EnrichedRecord],
    *,
    limit: int | None = None,
) -> tuple[list[EnrichedRecord], int]:
    to_enrich = _activities_to_enrich(activities, limit=limit)
    if not to_enrich:
        return activities, 0

    gen = _run_enrichment(to_enrich)
    try:
        while True:
            next(gen)
    except StopIteration as e:
        count, telemetry = e.value
        telemetry.log_summary()
        return activities, count


def enrich_activities_streaming(
    activities: list[EnrichedRecord],
    *,
    limit: int | None = None,
):
    to_enrich = _activities_to_enrich(activities, limit=limit)
    if not to_enrich:
        return activities, 0

    gen = _run_enrichment(to_enrich)
    try:
        while True:
            yield next(gen)
    except StopIteration as e:
        count, telemetry = e.value
        telemetry.log_summary()
        return activities, count


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        description="Enrich activities with post content (HTTP and store only).",
    )
    parser.add_argument(
        "input",
        type=Path,
        nargs="?",
        help="activities.csv path (default: master CSV)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Max number of posts to enrich (for testing)",
    )
    args = parser.parse_args()
    in_path = args.input
    if not in_path:
        in_path = get_default_csv_path()
    if not in_path.exists():
        parser.error(f"Input not found: {in_path}")
    if in_path.suffix.lower() != ".csv":
        parser.error(f"Expected a .csv file, got {in_path}")
    activities = collect_from_csv(csv_path=in_path)
    _, count = enrich_activities(activities, limit=args.limit)
    print(f"Enriched {count} activities (content store updated).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
