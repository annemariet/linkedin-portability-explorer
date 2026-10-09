# Post images: CDN embeds + metadata (no enrich-time download)

## Enrich behaviour

1. **No download** — enrich does not fetch image bytes or write new files under `content/images/`.
2. **New / updated posts** — sidecar markdown embeds the **signed LinkedIn CDN URL** as-is: `![](https://media.licdn.com/…?e=…&t=…)`. `meta.images[]` holds one record per image, de-duplicated by **identity** (`scheme + host + path`, query stripped):
   - `cdn_url` — latest signed URL from extraction
   - `cdn_expires_at` — ISO-8601 UTC from query param `e=` when present
   - `identity` — stable path identity (merge key on re-sign)
3. **Existing local images** (from older main-sidecar downloads) — never deleted or overwritten. If the same identity already has a trusted `images/…` file on disk, the sidecar keeps the **local** embed instead of the CDN link, and `local_path` stays in metadata.
4. **Placeholders** — LinkedIn generic assets (`static.licdn.com`, ghost/logo paths) are skipped (not embedded, not in meta).
5. **Traversal** — untrusted `images/…` paths in fresh HTML are stripped; only trusted files under `content/images/` from the **existing** sidecar are reused.

## Signed URLs and expiry

CDN links use `e=` (and `t=`) query parameters. They **expire**; after expiry the embed may break in Obsidian or the vault until a later enrich refreshes the URL in markdown and meta. **Local durable copies** (diagrams worth keeping offline) are a separate future **image-checker** pass — not this pipeline.

## Vault export (amai-lab)

Export copies only trusted local `images/…` files (`resolve_trusted_local_rel`). It does not download CDN URLs at export time.
