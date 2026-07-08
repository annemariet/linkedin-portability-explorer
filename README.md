# linkedin-portability

Python client for the **LinkedIn Member Data Portability API**: fetch your post-related activities, enrich a local content store, fetch linked URLs, and optionally summarize with an LLM.

## Features

- Fetch post-related activities (posts, comments, reactions, reposts) via the Portability API
- Append-only `activities.csv` cache under `~/.linkedin_api/data/`
- Content store (Markdown + metadata sidecars) for enrichment and summaries
- Fetch and cache linked article/page content
- Optional LLM summarization and Gradio UI for pipeline + activity reports

## Install

```bash
uv sync                    # core: fetch, CSV, content store, URL fetch
uv sync --extra llm        # + OpenAI/Mammouth, Ollama (Anthropic via OpenAI SDK)
uv sync --extra ui         # + Gradio app (includes llm extra)
```

**Package name:** PyPI distribution is **`linkedin-portability`** (import as `linkedin_api`).
Do not install PyPI **`linkedin-api-client`** — that is LinkedIn’s unrelated official SDK.

## Library API

```python
from linkedin_api import PipelineOptions, run_pipeline, parse_period
from linkedin_api import extract_activity_records, get_all_post_activities
from linkedin_api import load_content, load_metadata, resolve_redirect, strip_utm_params

opts = PipelineOptions(last="7d", from_cache=False)
activities, stats = run_pipeline(opts)
```

## CLI entry points

| Command | Description |
|---------|-------------|
| `linkedin-pipeline` | collect → enrich → fetch linked URLs → summarize |
| `linkedin-gradio` | Gradio UI (pipeline + activity report) |

## Data flow

```
LinkedIn API → activities.csv → pipeline (collect → enrich → fetch URLs → summarize)
                                      ↓
                              content_store/ + resources/
```

Downstream consumers (e.g. private vault export in amai-lab) should depend on the public `linkedin_api` API — not private `_` helpers.

## Setup (auth)

As a library dependency:

```bash
pip install "linkedin-portability @ git+https://github.com/annemariet/linkedin-portability-explorer.git"
```

### 2. Get LinkedIn Access Token

1. Go to [LinkedIn Developers](https://www.linkedin.com/developers/)
2. Create a new app
3. Get OAuth 2.0 access token with `r_dma_portability_self_serve` scope
4. Get token from: https://www.linkedin.com/developers/tools/oauth?clientId=78bwhum7gz6t9t

**Note:** Tokens expire every ~60 days. You'll need to regenerate them.

### 3. Configure Access Token

**Recommended: Store in Keychain (macOS)**

```bash
uv run python scripts/setup_token.py
```

This securely stores your token in macOS Keychain.

**Alternative: Environment Variable**

```bash
export LINKEDIN_ACCESS_TOKEN=your_access_token_here
```

### 4. Configure LLM (optional)

Providers: **mammouth** (default, OpenAI-compatible), **anthropic** (Claude via Anthropic's OpenAI-compatible API), **ollama** (local).

```bash
# Mammouth (default when LLM_PROVIDER=openai)
LLM_PROVIDER=openai
LLM_API_KEY=your-mammouth-api-key

# Anthropic direct (OpenAI SDK → https://api.anthropic.com/v1/)
LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=your-anthropic-api-key
LLM_MODEL=claude-sonnet-4-5

# Per-stage overrides (cheaper summary, stronger report)
LLM_SUMMARY_PROVIDER=ollama
LLM_SUMMARY_MODEL=llama3.2:3b
LLM_REPORT_PROVIDER=anthropic
LLM_REPORT_MODEL=claude-sonnet-4-5
```

## Usage

### CLI pipeline

```bash
uv run linkedin-pipeline --last 7d
uv run linkedin-pipeline --skip-fetch --last 30d
```

### Gradio UI

```bash
uv run linkedin-gradio
```

Set period (e.g. 7d), run the pipeline, and generate an activity report. Uses `activities.csv` and `content_store/` only — no graph database.

### Scripts (`scripts/`)

| Script | Purpose |
|--------|---------|
| `setup_token.py` | Store LinkedIn token in keyring |
| `check_token.py` | Validate token without exposing it |
| `verify_vertex_ai.py` | Smoke-test Vertex AI connectivity |
| `urn_to_url_example.py` | Example: URN to URL conversion |
| `validate_urn_urls.py` | Validate URN-to-URL conversions (HTTP) |

Run with `uv run python scripts/<name>.py`.

## Troubleshooting

### Token Expired

If you see `401 Unauthorized` or `EXPIRED_ACCESS_TOKEN`:

1. Get a new token from: https://www.linkedin.com/developers/tools/oauth?clientId=78bwhum7gz6t9t
2. Update it: `uv run python scripts/setup_token.py`

### Rate Limiting

LinkedIn API has rate limits. If you hit them:
- Wait a few minutes and retry
- The scripts handle pagination automatically

## API References

- [LinkedIn Member Data Portability API](https://learn.microsoft.com/en-us/linkedin/dma/member-data-portability/shared/member-changelog-api?view=li-dma-data-portability-2025-11&tabs=http)
- [LinkedIn DMA Portability API Terms](https://www.linkedin.com/legal/l/portability-api-terms)
- [Anthropic OpenAI SDK compatibility](https://platform.claude.com/docs/en/api/openai-sdk)
