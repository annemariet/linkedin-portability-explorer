# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python client for LinkedIn's Portability API (DMA-compliant) that fetches your own activity (posts, reactions, comments), enriches a local content store, fetches linked URLs, and optionally summarizes activity with an LLM. Data is obtained via the Portability API, not by scraping.

## Core Workflow

1. **Fetch LinkedIn Data** → Extract changelog activities via LinkedIn Portability API → `activities.csv`
2. **Enrich Content Store** → Markdown + metadata sidecars under `~/.linkedin_api/data/content_store/`
3. **Fetch Linked URLs** → Cache external article/page content
4. **Summarize & Report** → LLM categorization/summaries and period-based activity reports (CLI or Gradio)

## Development Commands

### Setup
```bash
uv sync                    # core
uv sync --extra llm        # + OpenAI, Ollama
uv sync --extra ui         # + Gradio (includes llm)
```

### Authentication Setup
```bash
uv run python scripts/setup_token.py
uv run python scripts/check_token.py
```

### Running Scripts
```bash
# Period-based pipeline (collect → enrich → fetch URLs → summarize)
uv run linkedin-pipeline --last 7d
uv run linkedin-pipeline --skip-fetch --last 30d

# Summarize/fetch CSV only
uv run python -m linkedin_api.summarize_activity --from-cache --last 7d

# Launch Gradio web interface
uv run linkedin-gradio
```

### Testing
```bash
uv run pytest
uv run black --check .
uv run flake8 linkedin_api tests scripts *.py
uv run mypy linkedin_api
```

## Architecture

### Key Modules

#### `linkedin_api/llm_config.py`
Configurable LLM factory (no neo4j-graphrag):
- `create_llm()`: Returns `OpenAICompatLLM`, `OllamaLLMClient` based on `LLM_PROVIDER`
- **mammouth** / **openai**: OpenAI SDK → Mammouth or custom `LLM_BASE_URL`
- **anthropic**: OpenAI SDK → `https://api.anthropic.com/v1/` with `ANTHROPIC_API_KEY`
- **ollama**: Local Ollama chat API

Per-stage overrides: `LLM_SUMMARY_PROVIDER/MODEL`, `LLM_REPORT_PROVIDER/MODEL`.

#### `linkedin_api/pipeline.py` / `run_pipeline.py`
Public pipeline API: collect → enrich → fetch linked content → summarize.

#### `linkedin_api/pipeline_report.py`
Period-based activity report generation with disk cache.

#### `linkedin_api/gradio_pipeline_ui.py`
Gradio UI for pipeline + report (imports from pipeline modules).

#### `linkedin_api/content_store.py`
Markdown content + `.meta.json` sidecars for posts.

#### `linkedin_api/activity_csv.py`
Append-only `activities.csv` cache.

### Data Flow

```
LinkedIn API
    ↓ (changelog fetch)
activities.csv
    ↓ (pipeline)
content_store/ + resources/
    ↓ (LLM summarize + report)
Activity reports (Markdown)
```

## Environment Variables

```bash
LINKEDIN_ACCESS_TOKEN=           # or keyring via setup_token.py
LLM_PROVIDER=openai              # openai | ollama | anthropic
LLM_MODEL=gpt-5-nano             # default for openai/mammouth
LLM_API_KEY=                     # Mammouth / OpenAI-compatible
ANTHROPIC_API_KEY=               # for anthropic provider
ANTHROPIC_BASE_URL=              # default https://api.anthropic.com/v1/
LLM_SUMMARY_PROVIDER=            # override for summarization
LLM_REPORT_PROVIDER=             # override for report generation
OLLAMA_BASE_URL=http://localhost:11434
LINKEDIN_DATA_DIR=~/.linkedin_api/data
PORT=7860                        # Gradio (Scalingo)
```

## Deployment

- `Procfile`: `web: python -m linkedin_api.gradio_app`
- `runtime.txt`: Python 3.12
- See `SCALINGO_DEPLOYMENT.md`

## Important Notes

- Python 3.12+ required
- Black (88 cols), flake8, mypy (blocking in CI)
- Commits: conventional commits with gitmoji
- Tokens expire ~60 days; recreate at LinkedIn Developers OAuth tools
