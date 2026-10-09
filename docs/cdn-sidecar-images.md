# CDN images in content sidecars (ENRICHMENT_VERSION 4)

Companion to PR #72 / amai-lab #349. Copy into the GitHub PR description when editing by hand if needed.

## CDN URL expiry

### Shape we embed (real export markdown, ids redacted)

```text
https://media.licdn.com/dms/image/sync/v2/D4E27AQH{asset_id}/articleshare-shrink_480/B4EZmZ{blob_id}/0/{mtime}?e=1780668000&v=beta&t={signature}
```

- **`e=`** — Unix expiry (seconds). Example: `e=1780668000` → **2026-06-05 UTC**.
- **`v=beta`** — LinkedIn CDN version flag.
- **`t=`** — Access token; required with `e=` for fetch.

URLs from JSON-LD / `og:image` are stored verbatim in `meta.images` and in `![](…)` markdown.

### When a URL expires

Expect **HTTP 403** (or failed fetch) after expiry — broken image in Obsidian/vault previews unless bytes were copied elsewhere.

## Re-enrichment at version 4

| Situation | Behaviour |
|-----------|-----------|
| `enrichment_version` &lt; 4, full enrich | Overwrite `.md` / `.meta.json` with CDN embed + `meta.images`; no `content/images/` download. |
| Already v4, new activity | Merge `activities_ids` only — images unchanged. |
| Already v4, same activity | Skip. |
| Legacy `![](images/hash.jpg)` | Unchanged until full re-enrich; orphan files not deleted. |

## Proposal (vault / #349, not implemented)

1. Curator at vault export: temp GET while URL valid → quality gate → `catalog/third-party/images/`.
2. Re-enrich: refresh CDN URLs from HTML on each full fetch.
3. Optional S3 image cache only for vetted assets.
