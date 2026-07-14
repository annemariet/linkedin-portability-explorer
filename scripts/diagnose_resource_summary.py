#!/usr/bin/env python3
"""Explain why a linked article is or is not selected for LLM summarization."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from linkedin_api.activity_csv import get_data_dir  # noqa: E402
from linkedin_api.fetch_linked_content import (  # noqa: E402
    _fetch_result_from_resource_data,
    _resource_dir,
    _resource_json_paths,
    is_exportable_resource,
)
from linkedin_api.summarize_resources import (  # noqa: E402
    _MIN_ARTICLE_CHARS,
    _resource_summary_complete,
    list_resources_for_summary,
)


def _load_json(url: str) -> tuple[Path | None, dict | None]:
    for path in _resource_json_paths(url):
        if path.exists():
            return path, json.loads(path.read_text(encoding="utf-8"))
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="Linked article URL as stored in metadata")
    ap.add_argument(
        "--scope",
        nargs="*",
        help="Period scope keys (post_id and/or post_urn values)",
    )
    ap.add_argument("--force", action="store_true", help="Ignore existing summary")
    args = ap.parse_args()

    path, data = _load_json(args.url)
    if data is None:
        print(f"NOT FOUND: no resource JSON for {args.url!r}")
        print(f"  data dir: {get_data_dir() / 'resources'}")
        return 1

    print(f"json: {path}")
    cited_by = [str(x) for x in (data.get("cited_by") or []) if str(x).strip()]
    print(
        f"cited_by ({len(cited_by)}): {cited_by[:5]}{'…' if len(cited_by) > 5 else ''}"
    )

    scope = set(args.scope) if args.scope else None
    if scope is not None:
        overlap = set(cited_by).intersection(scope)
        print(f"scope keys: {len(scope)}")
        print(f"scope overlap with cited_by: {sorted(overlap) or 'NONE'}")
        if not overlap:
            print(
                "  FAIL: period scope — article not linked to any post in --scope "
                "(empty cited_by also fails)"
            )
    else:
        print("scope: (none — would scan all resources)")

    result = _fetch_result_from_resource_data(dict(data))
    if result is None:
        print("FAIL: could not parse FetchResult")
        return 1

    if not is_exportable_resource(result):
        print(f"FAIL: not exportable (error={result.error!r}, title={result.title!r})")
        return 1

    body = (result.content or "").strip()
    print(f"content length: {len(body)} (min {_MIN_ARTICLE_CHARS})")
    if len(body) < _MIN_ARTICLE_CHARS:
        print("  FAIL: body too short for article summarization")

    complete = _resource_summary_complete(result)
    print(
        f"summary complete: {complete} "
        f"(tldr={result.tldr!r}, bullets={len(result.summary_bullets or [])})"
    )
    if complete and not args.force:
        print("  FAIL: already has TLDR + bullets (use --force to resummarize)")

    selected = list_resources_for_summary(
        urns=set(args.scope) if args.scope else None,
        force=args.force,
    )
    urls = {(r.resolved_url or r.url) for r in selected}
    canon = args.url.strip()
    if any(canon.rstrip("/") in (u or "").rstrip("/") for u in urls):
        print("RESULT: WOULD SUMMARIZE")
        return 0

    print("RESULT: would NOT summarize")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
