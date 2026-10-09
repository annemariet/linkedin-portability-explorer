# Post images: local store + CDN metadata (ENRICHMENT_VERSION 4)

## Enrich behaviour

1. **Download first** — `download_image_to_store` writes `content/images/<sha>.jpg`.
2. **Markdown** — embed `![](images/<sha>.jpg)` only when a local file exists. **Never** embed `https://media.licdn.com/…` when local copy exists.
3. **Metadata** — `meta.images[]` objects:
   - `cdn_url` — original LinkedIn CDN URL (fallback reference)
   - `cdn_expires_at` — ISO-8601 UTC decoded from query param `e=`
   - `local_path` — e.g. `images/<sha>.jpg` when downloaded

## CDN signed URLs

Example shape (redacted):

```text
https://media.licdn.com/dms/image/sync/v2/D4E27AQH{…}/articleshare-shrink_480/B4EZm{…}/0/1776442767212?e=1780668000&v=beta&t={…}
```

- `e=1780668000` → **2026-06-05 UTC**
- After expiry (or HTTP **403**), download/export **skips** the image with a **warning**; enrich/export **do not fail**.
- Metadata may still list `cdn_url` for a future refresh; markdown is not updated with a dead hotlink.

## Re-enrichment (v4)

| Case | Behaviour |
|------|-----------|
| Sidecar already has `![](images/…)` and file exists | **Keep** embed and file; refresh `cdn_url` / `cdn_expires_at` in meta only |
| Version &lt; 4, full re-enrich | Re-run download + local embed; strip any CDN markdown embeds |
| CDN fetch fails (non-expired) | Metadata-only `cdn_url`; no markdown embed |
| CDN expired | Skip fetch; metadata-only; warning logged |

## Vault export

`linkedin_api.post_images.fetch_image_bytes_for_vault` reads `local_path` first, then optional CDN GET (same expiry/403 rules). Missing bytes are not fatal.
