"""Tests for migrate_content_store_post_id_stems helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.migrate_content_store_post_id_stems import (
    build_stem_remap,
    migrate_resource_cited_by,
    remap_cited_by,
)


def test_build_stem_remap_maps_hash_stem_and_urn_hash(tmp_path: Path):
    content = tmp_path / "content"
    content.mkdir()
    post_id = "7482038400523575296"
    urn = f"urn:li:ugcPost:{post_id}"
    hash_stem = hashlib.sha256(urn.encode()).hexdigest()
    meta = {"post_id": post_id, "post_urn": urn}
    (content / f"{hash_stem}.meta.json").write_text(json.dumps(meta), encoding="utf-8")

    remap = build_stem_remap(content)

    assert remap[hash_stem] == post_id
    assert remap[hash_stem] == remap[hashlib.sha256(urn.encode()).hexdigest()]


def test_remap_cited_by_replaces_hash_with_post_id():
    post_id = "7482038400523575296"
    urn = f"urn:li:ugcPost:{post_id}"
    hash_stem = hashlib.sha256(urn.encode()).hexdigest()
    remap = {hash_stem: post_id}

    out = remap_cited_by([hash_stem, urn, post_id], remap)

    assert out == [post_id]


def test_migrate_resource_cited_by_dry_run_counts_without_writing(tmp_path: Path):
    resources = tmp_path / "resources"
    resources.mkdir()
    post_id = "123"
    urn = "urn:li:ugcPost:123"
    hash_stem = hashlib.sha256(urn.encode()).hexdigest()
    path = resources / "abc.json"
    path.write_text(
        json.dumps({"url": "https://example.com", "cited_by": [hash_stem]}),
        encoding="utf-8",
    )

    changed = migrate_resource_cited_by(resources, {hash_stem: post_id}, apply=False)

    assert changed == 1
    assert json.loads(path.read_text())["cited_by"] == [hash_stem]


def test_migrate_resource_cited_by_apply_writes_post_id(tmp_path: Path):
    resources = tmp_path / "resources"
    resources.mkdir()
    post_id = "123"
    urn = "urn:li:ugcPost:123"
    hash_stem = hashlib.sha256(urn.encode()).hexdigest()
    path = resources / "abc.json"
    path.write_text(
        json.dumps({"url": "https://example.com", "cited_by": [hash_stem]}),
        encoding="utf-8",
    )

    changed = migrate_resource_cited_by(resources, {hash_stem: post_id}, apply=True)

    assert changed == 1
    assert json.loads(path.read_text())["cited_by"] == [post_id]
