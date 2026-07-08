# Deploying linkedin-portability to Scalingo

## Overview

Gradio web UI for the LinkedIn portability pipeline and activity reports.

## Prerequisites

- Scalingo account
- LinkedIn Portability API access token
- LLM API key (Mammouth, Anthropic, or Ollama on a worker — Ollama is not practical on Scalingo)

## Files for Deployment

- `Procfile` — `web: python -m linkedin_api.gradio_app`
- `pyproject.toml` / `uv.lock` — dependencies (install with `uv sync --extra ui`)
- `runtime.txt` — Python 3.12

## Deployment Steps

### 1. Create Scalingo App

```bash
scalingo create linkedin-portability
```

### 2. Configure Environment Variables

```bash
# LinkedIn
scalingo --app linkedin-portability env-set LINKEDIN_ACCESS_TOKEN="..."

# LLM — Mammouth (default)
scalingo --app linkedin-portability env-set LLM_PROVIDER=openai
scalingo --app linkedin-portability env-set LLM_API_KEY="..."

# Or Anthropic direct (OpenAI-compatible API)
scalingo --app linkedin-portability env-set LLM_PROVIDER=anthropic
scalingo --app linkedin-portability env-set ANTHROPIC_API_KEY="..."
scalingo --app linkedin-portability env-set LLM_REPORT_MODEL=claude-sonnet-4-5

# Per-stage models
scalingo --app linkedin-portability env-set LLM_SUMMARY_PROVIDER=openai
scalingo --app linkedin-portability env-set LLM_SUMMARY_MODEL=gpt-5-nano
scalingo --app linkedin-portability env-set LLM_REPORT_PROVIDER=anthropic
scalingo --app linkedin-portability env-set LLM_REPORT_MODEL=claude-sonnet-4-5
```

### 3. Deploy

Push to the Scalingo git remote or connect GitHub auto-deploy.

### 4. Open

```bash
scalingo --app linkedin-portability open
```

## Troubleshooting

```bash
scalingo --app linkedin-portability logs --lines 100
```

Common issues:
- `EXPIRED_ACCESS_TOKEN` — refresh LinkedIn token
- LLM timeout — reduce report scope (fewer posts, shorter period) or use a faster model for summary stage
- `ThinkingBlock` errors — redeploy latest `main` (Anthropic now routes through OpenAI-compatible API, not neo4j-graphrag)

## Notes

- Persistent data (`activities.csv`, content store) requires a Scalingo disk addon or external storage; ephemeral filesystem resets on restart.
- No Neo4j or GraphRAG stack — pipeline + report only.
