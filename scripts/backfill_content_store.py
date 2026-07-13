#!/usr/bin/env python3
"""
Backfill content-store ``.meta.json`` from CSV and optionally re-fetch LinkedIn HTML.

1. **CSV only (default):** Merge ``post_id``, ``post_urn``, union ``activities_ids``.

2. **``--fetch-html``:** GET each ``post_url`` and run ``linkedin_api.post_extraction``
   (same pipeline as enrich): DOM-classified ``urls`` / ``mentions`` / ``tags``,
   ``images``, trafilatura markdown → ``.md``, JSON-LD author fields.

3. **``--fetch-author`` (deprecated):** Only fill missing ``post_author`` / URL from HTML
   (lighter than full ``--fetch-html``).

Examples::

    uv run python scripts/backfill_content_store.py
    uv run python scripts/backfill_content_store.py --fetch-html --sleep 1.0 --limit 20
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import cast

from tqdm import tqdm

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from linkedin_api.activity_csv import get_data_dir, load_records_csv  # noqa: E402
from linkedin_api.content_store import (  # noqa: E402
    load_metadata,
    merge_post_identity,
    update_metadata_fields,
)
from linkedin_api.enriched_record import EnrichedRecord  # noqa: E402
from linkedin_api.http_client import fetch_linkedin_post_html  # noqa: E402
from linkedin_api.post_extraction import (  # noqa: E402
    ENRICHMENT_VERSION,
    extract_post_from_html,
    save_extraction_to_store,
)
from linkedin_api.utils.post_html import parse_post_meta_from_soup  # noqa: E402
from linkedin_api.utils.urns import extract_urn_id  # noqa: E402
from linkedin_api.utils.urls import (
    extract_urls_from_text,
    is_comment_feed_url,
)  # noqa: E402

logger = logging.getLogger("backfill_content_store")


def _load_registry(content_dir: Path) -> dict[str, str]:
    p = content_dir / "_urn_registry.json"
    if not p.exists():
        return {}
    return cast(dict[str, str], json.loads(p.read_text(encoding="utf-8")))


def _aggregate_csv_by_post_id(
    csv_path: Path,
) -> tuple[dict[str, list[str]], dict[str, list[str]], int]:
    """post_id -> activity_ids; post_id -> urls from CSV row text."""
    records = load_records_csv(csv_path)
    n_rows = len(records)
    by_id: dict[str, list[str]] = {}
    urls_by_id: dict[str, list[str]] = {}
    for rec in records:
        er = EnrichedRecord.from_activity_record(rec)
        pid = (er.post_id or "").strip()
        if not pid:
            continue
        aid = (rec.activity_id or "").strip()
        if aid:
            by_id.setdefault(pid, [])
            if aid not in by_id[pid]:
                by_id[pid].append(aid)
        for u in er.urls:
            u = (u or "").strip()
            if not u:
                continue
            urls_by_id.setdefault(pid, [])
            if u not in urls_by_id[pid]:
                urls_by_id[pid].append(u)
    return by_id, urls_by_id, n_rows


def _post_id_from_urn(urn: str) -> str:
    return (extract_urn_id(urn) or "").strip()


def _author_only_jobs(
    stems: list[str],
    registry: dict[str, str],
    content_dir: Path,
    limit: int,
) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for stem in stems:
        urn = (registry.get(stem) or "").strip()
        if not urn:
            continue
        if not (content_dir / f"{stem}.meta.json").exists():
            continue
        post_id = stem if stem.isdigit() else _post_id_from_urn(urn)
        meta = load_metadata(post_id, post_urn=urn)
        if meta is None:
            continue
        if (str(meta.get("post_author") or "")).strip():
            continue
        post_url = (meta.get("post_url") or "").strip()
        if not post_url or is_comment_feed_url(post_url):
            continue
        out.append((stem, urn, post_url))
        if limit and len(out) >= limit:
            break
    return out


def _html_fetch_jobs(
    stems: list[str],
    registry: dict[str, str],
    content_dir: Path,
    limit: int,
) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for stem in stems:
        urn = (registry.get(stem) or "").strip()
        if not urn:
            continue
        if not (content_dir / f"{stem}.meta.json").exists():
            continue
        post_id = stem if stem.isdigit() else _post_id_from_urn(urn)
        meta = load_metadata(post_id, post_urn=urn)
        if meta is None:
            continue
        post_url = (meta.get("post_url") or "").strip()
        if not post_url or is_comment_feed_url(post_url):
            continue
        try:
            if int(meta.get("enrichment_version") or 0) == ENRICHMENT_VERSION:
                continue  # already up to date, skip
        except (TypeError, ValueError):
            pass
        out.append((stem, urn, post_url))
        if limit and len(out) >= limit:
            break
    return out


def _fetch_author_only(post_url: str, timeout: float) -> dict[str, str]:
    fetched = fetch_linkedin_post_html(post_url, timeout=timeout)
    if not fetched:
        return {}
    html, final_url = fetched
    from bs4 import BeautifulSoup

    return parse_post_meta_from_soup(BeautifulSoup(html, "html.parser"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--csv", type=Path, default=None, help="activities.csv")
    ap.add_argument("--data-dir", type=Path, default=None, help="Data root")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--fetch-html",
        action="store_true",
        help="Re-fetch post HTML and refresh .md + classified metadata (trafilatura + DOM)",
    )
    ap.add_argument(
        "--fetch-author",
        action="store_true",
        help="Deprecated: only backfill post_author from HTML when empty",
    )
    ap.add_argument("--sleep", type=float, default=1.0)
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--limit", type=int, default=0, help="Max HTTP jobs (0 = no cap)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-q", "--quiet", action="store_true")
    ap.add_argument("--no-progress", action="store_true")
    args = ap.parse_args()

    if args.quiet and args.verbose:
        print("Use only one of --quiet or --verbose", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(message)s",
        stream=sys.stderr,
    )
    use_tqdm = not (args.quiet or args.no_progress)

    data_dir = args.data_dir or get_data_dir()
    csv_path = args.csv or (data_dir / "activities.csv")
    if not csv_path.exists():
        logger.error("CSV not found: %s", csv_path)
        return 1

    content_dir = data_dir / "content"
    if not content_dir.is_dir():
        logger.error("Content dir not found: %s", content_dir)
        return 1

    registry = _load_registry(content_dir)
    if not registry:
        logger.error("No _urn_registry.json under %s", content_dir)
        return 1

    by_id, urls_by_id, n_csv_rows = _aggregate_csv_by_post_id(csv_path)
    stems = sorted(registry.keys())
    eligible = [
        s
        for s in stems
        if (registry.get(s) or "").strip() and (content_dir / f"{s}.meta.json").exists()
    ]
    if not args.quiet:
        print(
            f"CSV identity merge: {len(eligible)} metadata file(s), "
            f"{n_csv_rows} CSV row(s)",
            file=sys.stderr,
        )

    csv_merged = 0
    for stem in tqdm(
        eligible,
        desc="CSV merge",
        unit="file",
        disable=not use_tqdm,
        file=sys.stderr,
    ):
        urn = (registry.get(stem) or "").strip()
        post_id = stem if stem.isdigit() else _post_id_from_urn(urn)
        if not post_id:
            continue
        extra_ids = by_id.get(post_id, [])
        if args.dry_run:
            continue
        out = merge_post_identity(
            post_id,
            post_urn=urn,
            extra_activity_ids=extra_ids,
        )
        if out is not None:
            csv_merged += 1

    if not args.quiet and not args.dry_run:
        print(
            f"CSV merge done: updated {csv_merged} metadata file(s).", file=sys.stderr
        )

    lim = args.limit or 0

    if args.fetch_html:
        if args.dry_run:
            jobs = _html_fetch_jobs(stems, registry, content_dir, lim)
            if not args.quiet:
                print(
                    f"[dry-run] would run {len(jobs)} HTML fetch(es).", file=sys.stderr
                )
            return 0
        jobs = _html_fetch_jobs(stems, registry, content_dir, lim)
        if not args.quiet:
            print(f"HTML fetch: {len(jobs)} job(s)", file=sys.stderr)
        for _stem, urn, post_url in tqdm(
            jobs, desc="fetch-html", unit="GET", disable=not use_tqdm, file=sys.stderr
        ):
            post_id = _stem if _stem.isdigit() else _post_id_from_urn(urn)
            meta = load_metadata(post_id, post_urn=urn)
            if meta is None:
                continue
            fetched = fetch_linkedin_post_html(post_url, timeout=args.timeout)
            if fetched is None:
                # Reason already logged by fetch_linkedin_post_html.
                if args.sleep > 0:
                    time.sleep(args.sleep)
                continue
            html, final_url = fetched
            ext = extract_post_from_html(html, final_url)
            if ext is None:
                logger.warning("No extractable body: %s", post_url[:80])
                if args.sleep > 0:
                    time.sleep(args.sleep)
                continue
            api_urls = urls_by_id.get(post_id, [])
            post_created = (ext.html_meta.get("post_created_at") or "").strip() or None
            if not post_created:
                try:
                    from linkedin_api.utils.linkedin_snowflake import (
                        post_created_at_from_urn,
                    )

                    post_created = post_created_at_from_urn(urn) or None
                except Exception:
                    post_created = None
            save_extraction_to_store(
                post_id=post_id,
                post_urn=urn,
                post_url=post_url,
                ext=ext,
                urls_from_api=api_urls,
                activity_time_iso=(meta.get("activity_time_iso") or "").strip() or "",
                post_created=post_created or "",
                activities_ids=list(
                    dict.fromkeys(
                        (meta.get("activities_ids") or []) + by_id.get(post_id, [])
                    )
                ),
            )
            if args.sleep > 0:
                time.sleep(args.sleep)
        if not args.quiet:
            print("HTML backfill done.", file=sys.stderr)
        return 0

    if args.fetch_author:
        if args.dry_run:
            jobs = _author_only_jobs(stems, registry, content_dir, lim)
            if not args.quiet:
                print(
                    f"[dry-run] would run {len(jobs)} author GET(s).", file=sys.stderr
                )
            return 0
        jobs = _author_only_jobs(stems, registry, content_dir, lim)
        for _stem, urn, post_url in tqdm(
            jobs, desc="Author fetch", unit="GET", disable=not use_tqdm, file=sys.stderr
        ):
            post_id = _stem if _stem.isdigit() else _post_id_from_urn(urn)
            meta = load_metadata(post_id, post_urn=urn)
            if meta is None:
                continue
            html_meta = _fetch_author_only(post_url, args.timeout)
            if not html_meta.get("post_author") and not html_meta.get(
                "post_author_url"
            ):
                if args.sleep > 0:
                    time.sleep(args.sleep)
                continue
            kwargs: dict = {}
            if html_meta.get("post_author"):
                kwargs["post_author"] = html_meta["post_author"]
            if html_meta.get("post_author_url"):
                kwargs["post_author_url"] = html_meta["post_author_url"]
            if (
                html_meta.get("post_created_at")
                and not (str(meta.get("post_created_at") or "")).strip()
            ):
                kwargs["post_created_at"] = html_meta["post_created_at"]
            if kwargs:
                update_metadata_fields(post_id, post_urn=urn, **kwargs)
            if args.sleep > 0:
                time.sleep(args.sleep)
        return 0

    if not args.quiet:
        print(
            "Done. Use --fetch-html to refresh body + metadata, or --fetch-author for author only.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
