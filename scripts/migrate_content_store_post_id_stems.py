#!/usr/bin/env python3
"""
Merge legacy URN-hash content store files into ``{post_id}.*`` stems.

Groups ``*.meta.json`` by ``post_id`` metadata field (or numeric stem).
Keeps the richest ``.md`` body, unions ``activities_ids``, and removes
superseded hash-named files after ``--apply``.

Also rewrites ``cited_by`` in ``resources/*.json`` so linked-article
records point at the new ``{post_id}`` content stems instead of legacy
``sha256(post_urn)`` hashes (or raw URNs).

Examples::

    uv run python scripts/migrate_content_store_post_id_stems.py --dry-run
    uv run python scripts/migrate_content_store_post_id_stems.py --apply
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from linkedin_api.activity_csv import get_data_dir  # noqa: E402
from linkedin_api.content_store import _content_dir  # noqa: E402


def _group_by_post_id(content_dir: Path) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = defaultdict(list)
    for meta_path in sorted(content_dir.glob("*.meta.json")):
        stem = meta_path.name.removesuffix(".meta.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = (str(meta.get("post_id") or "")).strip() or (
            stem if stem.isdigit() else ""
        )
        if not pid:
            continue
        if stem not in groups[pid]:
            groups[pid].append(stem)
    return groups


def build_stem_remap(content_dir: Path) -> dict[str, str]:
    """Map legacy content stems (hash filenames, sha256 URNs) to ``post_id``."""
    remap: dict[str, str] = {}
    for meta_path in sorted(content_dir.glob("*.meta.json")):
        stem = meta_path.name.removesuffix(".meta.json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pid = (str(meta.get("post_id") or "")).strip() or (
            stem if stem.isdigit() else ""
        )
        if not pid:
            continue
        if stem != pid:
            remap[stem] = pid
        post_urn = (str(meta.get("post_urn") or "")).strip()
        if post_urn:
            remap[hashlib.sha256(post_urn.encode()).hexdigest()] = pid
    return remap


def remap_cited_by_entry(entry: str, remap: dict[str, str]) -> str:
    """Return the post_id stem for a single ``cited_by`` entry."""
    raw = str(entry).strip()
    if not raw:
        return raw
    if raw in remap:
        return remap[raw]
    if raw.startswith("urn:"):
        hashed = hashlib.sha256(raw.encode()).hexdigest()
        return remap.get(hashed, hashed)
    return raw


def remap_cited_by(cited_by: list[object], remap: dict[str, str]) -> list[str]:
    """Rewrite ``cited_by`` list entries to ``post_id`` stems; dedupe."""
    out: list[str] = []
    for entry in cited_by:
        mapped = remap_cited_by_entry(str(entry), remap)
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def _best_md_stem(content_dir: Path, stems: list[str]) -> str:
    best = stems[0]
    best_len = 0
    for stem in stems:
        path = content_dir / f"{stem}.md"
        if not path.exists():
            continue
        n = len(path.read_text(encoding="utf-8"))
        if n > best_len:
            best_len = n
            best = stem
    return best


def migrate_resource_cited_by(
    resource_dir: Path,
    remap: dict[str, str],
    *,
    apply: bool,
) -> int:
    """Rewrite ``cited_by`` in resource JSON files. Returns files changed."""
    if not remap or not resource_dir.is_dir():
        return 0

    changed = 0
    for json_path in sorted(resource_dir.glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        raw = data.get("cited_by")
        if not isinstance(raw, list) or not raw:
            continue
        updated = remap_cited_by(raw, remap)
        if updated == [str(x) for x in raw if str(x).strip()]:
            continue
        changed += 1
        if apply:
            data["cited_by"] = updated
            json_path.write_text(
                json.dumps(data, indent=0, ensure_ascii=False), encoding="utf-8"
            )
    return changed


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data-dir", type=Path, default=None)
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned merges without writing (default when --apply is omitted)",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write merged files and delete superseded stems",
    )
    args = ap.parse_args()
    if args.apply and args.dry_run:
        ap.error("use either --dry-run or --apply, not both")

    data_dir = args.data_dir or get_data_dir()
    content_dir = data_dir / "content" if args.data_dir else _content_dir()
    resource_dir = data_dir / "resources"

    if not content_dir.is_dir():
        print(f"No content dir: {content_dir}", file=sys.stderr)
        return 1

    stem_remap = build_stem_remap(content_dir)
    groups = _group_by_post_id(content_dir)
    merged = 0
    for pid, stems in sorted(groups.items()):
        if len(stems) <= 1 and stems[0] == pid:
            continue
        target = pid
        source_md = _best_md_stem(content_dir, stems)
        print(f"post_id={pid}: {len(stems)} stem(s) -> {target} (md from {source_md})")
        if not args.apply:
            merged += 1
            continue

        metas = []
        for stem in stems:
            mp = content_dir / f"{stem}.meta.json"
            if mp.exists():
                metas.append(json.loads(mp.read_text(encoding="utf-8")))

        combined: dict = {}
        for meta in metas:
            for k, v in meta.items():
                if k == "activities_ids":
                    prev = combined.get("activities_ids") or []
                    inc = v if isinstance(v, list) else []
                    combined["activities_ids"] = list(
                        dict.fromkeys([*(prev if isinstance(prev, list) else []), *inc])
                    )
                elif k not in combined or not combined[k]:
                    combined[k] = v
        combined["post_id"] = pid

        md_src = content_dir / f"{source_md}.md"
        if md_src.exists():
            shutil.copy2(md_src, content_dir / f"{target}.md")

        for suffix in (".meta.json", ".comments.json"):
            for stem in stems:
                p = content_dir / f"{stem}{suffix}"
                if stem != target and p.exists():
                    p.unlink()

        (content_dir / f"{target}.meta.json").write_text(
            json.dumps(combined, indent=0), encoding="utf-8"
        )

        comments = None
        for stem in stems:
            cp = content_dir / f"{stem}.comments.json"
            if cp.exists():
                comments = cp.read_text(encoding="utf-8")
                if stem != target:
                    cp.unlink()
        if comments and not (content_dir / f"{target}.comments.json").exists():
            (content_dir / f"{target}.comments.json").write_text(
                comments, encoding="utf-8"
            )

        for stem in stems:
            if stem == target:
                continue
            for ext in (".md", ".meta.json", ".comments.json"):
                p = content_dir / f"{stem}{ext}"
                if p.exists():
                    p.unlink()
        merged += 1

    resources_changed = migrate_resource_cited_by(
        resource_dir, stem_remap, apply=args.apply
    )

    print(
        f"{'Would merge' if not args.apply else 'Merged'} {merged} post_id group(s).",
        file=sys.stderr,
    )
    print(
        f"{'Would update' if not args.apply else 'Updated'} "
        f"{resources_changed} resource file(s) cited_by.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
