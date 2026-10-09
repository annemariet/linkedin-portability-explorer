# Post images: local store + CDN metadata (ENRICHMENT_VERSION 4)

## Enrich behaviour

1. **Download first** — `download_image_to_store` writes `content/images/<sha(path)>.<ext>` (hash uses the CDN URL **path**, query stripped).
2. **Markdown** — embed `![](images/…)` only for files under `content/images/`. **Never** trust `images/…` paths from fresh HTML extraction (author-controlled); only paths from the **existing** sidecar `.md` are reused when download is skipped.
3. **Metadata** — one `meta.images[]` entry per CDN image path identity (all `cdn_urls` from extraction, not only the first):
   - `cdn_url` — latest signed LinkedIn CDN URL
   - `cdn_expires_at` — ISO-8601 UTC from query param `e=` (60s clock-skew margin before expiry)
   - `local_path` — e.g. `images/<sha>.jpg` when on disk

## CDN signed URLs

Example shape (redacted):

```text
https://media.licdn.com/dms/image/sync/v2/D4E27AQH{…}/articleshare-shrink_480/B4EZm{…}/0/1776442767212?e=1780668000&v=beta&t={…}
```

- `e=1780668000` → **2026-06-05 UTC**
- After expiry or non-200 HTTP, download **skips** with a **warning** (URL path only in logs); enrich **does not fail**.
- API/HTML fallback runs the same sidecar step; if a prior local embed would be lost, **do not** bump `enrichment_version` so the post is retried.

## Re-enrichment (v4)

| Case | Behaviour |
|------|-----------|
| Sidecar already has trusted `![](images/…)` and file exists | Keep embed; merge meta by CDN path identity |
| Version &lt; 4, full re-enrich | Re-run download for each CDN URL; strip CDN markdown embeds |
| CDN fetch fails (non-expired) | Metadata-only `cdn_url`; reuse trusted prior local if present |
| CDN expired | Skip fetch; attach trusted prior local when available |

## Identity limits (known)

- **Host:** identity is `scheme + host + path`, so the same asset on `media.licdn.com` vs `media-exp1.licdn.com` is two entries (other `*.licdn.com` hosts are still downloaded when extraction lists them).
- **Size variants:** two `shrink_*` paths for one visual are two identities and may download twice.

## v3 → v4 migration

v3 files used `sha256(full signed URL)`; v4 uses `sha256(path without query)`. Re-enrich reuses the v3 on-disk file when `meta.images` still has the old full URL (no second download, no orphan).

## Vault export (amai-lab)

Catalog export copies `![](images/…)` only when `resolve_trusted_local_rel` accepts the path (see `linkedin_vault.vault_export`).
