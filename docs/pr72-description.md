# PR #72 — Paste into GitHub description

## Summary

Post images are **durable in the content store** again: enrich downloads to `content/images/<hash>.jpg` and embeds **local** `![](images/…)` in sidecar markdown. LinkedIn CDN URLs live only in **`meta.images[]`** as `{ cdn_url, cdn_expires_at, local_path? }` — not in `.md` when a local copy exists.

`ENRICHMENT_VERSION` remains **4**.

## CDN expiry evidence

Signed URL shape (from export markdown; redacted):

```text
https://media.licdn.com/dms/image/sync/v2/D4E27AQH{…}/articleshare-shrink_480/B4EZm{…}/0/1776442767212?e=1780668000&v=beta&t={…}
```

- **`e=1780668000`** → **2026-06-05 UTC** (`parse_linkedin_cdn_expires_at_unix` / `cdn_expires_at_iso`).
- **`v=beta`**, **`t=`** — LinkedIn CDN signature parameters.

When `e` is in the past (or GET returns **403**), download and vault CDN fetch **log a warning and skip**; enrich/export **continue** (no dead CDN embed in markdown).

## ENRICHMENT_VERSION=4 behaviour

| Situation | Markdown | `content/images/` | `meta.images[]` |
|-----------|----------|-------------------|-----------------|
| Fresh enrich, CDN OK | `![](images/hash.jpg)` | file written | `cdn_url`, `cdn_expires_at`, `local_path` |
| Re-enrich, local embed + file already present | **unchanged** local embed | **kept** | `cdn_url` / expiry refreshed |
| CDN expired at enrich | no image line | no new file | `cdn_url` + `cdn_expires_at` only |
| CDN fetch failed (not expired) | no image line | — | metadata-only `cdn_url` |
| Legacy `![](images/…)` on disk | preserved on re-enrich | preserved | gains `cdn_url` when HTML provides one |

## Test plan

```bash
uv run black --check .
uv run flake8 linkedin_api tests examples scripts
uv run mypy linkedin_api
uv run pytest
```

Key tests: `tests/test_post_images.py`

- `test_save_extraction_downloads_local_and_records_cdn_metadata`
- `test_reenrich_preserves_local_embed_and_file`
- `test_expired_cdn_fixture_skips_fetch_without_network`
- `test_fetch_cdn_image_bytes_logs_403_and_returns_none`
- `test_export_succeeds_when_expired_cdn_image_skipped`

Co-authored-by: Linus <linus@bots.amai.local>
