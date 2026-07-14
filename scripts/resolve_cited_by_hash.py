#!/usr/bin/env python3
"""Map a resource ``cited_by`` hash back to content-store metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from linkedin_api.activity_csv import get_data_dir  # noqa: E402
from linkedin_api.content_keys import storage_key  # noqa: E402


def _iter_candidates(content_dir: Path):
    registry_path = content_dir / "_urn_registry.json"
    registry: dict[str, str] = {}
    if registry_path.exists():
        registry = json.loads(registry_path.read_text(encoding="utf-8"))

    for meta_path in sorted(content_dir.glob("*.meta.json")):
        stem = meta_path.name.removesuffix(".meta.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        post_id = (str(meta.get("post_id") or "")).strip() or (
            stem if stem.isdigit() else ""
        )
        post_urn = (str(meta.get("post_urn") or registry.get(stem) or "")).strip()
        yield stem, post_id, post_urn, meta


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cited_by_hash", help="64-char hex from resources/*.json cited_by")
    args = ap.parse_args()
    target = args.cited_by_hash.strip().lower()
    content_dir = get_data_dir() / "content"

    print(f"Looking for cited_by hash: {target}")
    print(f"content dir: {content_dir}\n")

    matches = []
    for stem, post_id, post_urn, meta in _iter_candidates(content_dir):
        keys: dict[str, str] = {}
        if post_id:
            keys["post_id"] = post_id
        if post_urn:
            keys["sha256(post_urn)"] = hashlib.sha256(post_urn.encode()).hexdigest()
            cite_stem, _ = storage_key(post_id, post_urn=post_urn)
            if cite_stem:
                keys["citation_stem"] = cite_stem
        for label, value in keys.items():
            if value.lower() == target:
                matches.append((stem, post_id, post_urn, label, meta.get("post_url")))

    if not matches:
        print("No content-store meta matched this hash.")
        print("Likely causes:")
        print("- post_urn in .meta.json is an unparseable comment/nested URN")
        print("- fetch wrote sha256(post_urn) before post_id-based cited_by fix")
        print("- hash refers to a deleted/superseded content stem")
        return 1

    for stem, post_id, post_urn, label, post_url in matches:
        print(f"stem={stem} post_id={post_id!r}")
        print(f"  matched via: {label}")
        print(f"  post_urn: {post_urn!r}")
        print(f"  post_url: {post_url!r}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
