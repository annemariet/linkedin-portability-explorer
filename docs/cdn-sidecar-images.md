# Post images: CDN embeds + metadata (no enrich-time download)

## Enrich behaviour

1. **No download** — enrich does not fetch image bytes or write new files under `content/images/`.
2. **New / updated posts** — sidecar markdown embeds the **signed LinkedIn CDN URL** as-is: `![](https://media.licdn.com/dms/image/…?e=…&t=…)`. `meta.images[]` holds one record per image, de-duplicated by **identity**:
   - LinkedIn CDN: `scheme + host + path` (signed `e` / `v` / `t` query params are not part of identity)
   - Other hosts (if ever recorded): identity includes the query string
   - `cdn_url` — latest signed URL from extraction or prior meta (fallback paths)
   - `cdn_expires_at` — ISO-8601 UTC from query param `e=` when present
3. **Existing local images** (from older main-sidecar downloads) — never deleted or overwritten. If the same identity already has a trusted `images/…` file on disk (including v3 hash filenames), the sidecar keeps the **local** embed instead of the CDN link, and `local_path` stays in metadata. Only one embed per identity (no CDN + local double).
4. **Placeholders** — LinkedIn generic assets (`static.licdn.com`, `/scds/common/u/…`, `ghost_person`, `company-logo_*`, `profile-displayphoto*`) are skipped.
5. **Traversal** — untrusted `images/…` paths in fresh HTML are stripped; only trusted files under `content/images/` from the **existing** sidecar are reused. Non–LinkedIn `https://` image embeds in extracted markdown are left unchanged.

**Identity limits:** different `shrink_*` variants or hosts are separate images. Non-LinkedIn URLs that differ only by query are distinct identities.

## Signed URLs and expiry

CDN links use `e=` (and `t=`) query parameters. They **expire**; after expiry the embed may break in Obsidian or the vault. **`ENRICHMENT_VERSION` stays at 3**, so routine enrich does **not** refresh expired embeds on already-enriched posts. Refresh happens only via a **forced re-enrich**, or the planned **image-checker** pass (amai-lab-r0jf.4). **Local durable copies** are that checker’s job — not this pipeline.

## Vault export (amai-lab)

Export copies only trusted local `images/…` files (`resolve_trusted_local_rel`). It does not download CDN URLs at export time; CDN embeds remain as links in exported markdown.
