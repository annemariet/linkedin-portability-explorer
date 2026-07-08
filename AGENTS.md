# AGENTS.md

## Cursor Cloud specific instructions

### Overview

Python client for LinkedIn's Portability API — fetches activity, enriches a local content store, and generates LLM activity reports via a Gradio UI. See `CLAUDE.md` for full architecture and command reference.

### Cursor skills for this project

- **`github-use`** (`.cursor/skills/github-use/SKILL.md`): Git and GitHub workflow – feature branches and PRs only, small focused gitmoji-style commits, and always run format, lint, and tests before committing (never force-push).
- **`linear-use`** (`.cursor/skills/linear-use/SKILL.md`): Linear workflow – include ticket IDs in branches and PRs, keep ticket status in sync (in progress → in review → done), and start each ticket with a short implementation plan posted to Linear.
- **`gradio-pages`** (`.cursor/skills/gradio-pages/SKILL.md`): Gradio UI design – never block the first paint, keep one expensive operation per explicit action, use `gr.State` for UI state, and add logging so long-running steps stay observable.

### Running services

- **Gradio app**: `uv run python -m linkedin_api.gradio_app` (port 7860). The UI starts without an LLM; full pipeline requires an API key or Ollama.
- **LLM providers**: Mammouth (OpenAI-compatible, default), Anthropic (Claude via Anthropic's OpenAI-compatible API at `https://api.anthropic.com/v1/`), or Ollama (local).
- **Ollama**: Pre-installed with `llama3.2:3b` and `nomic-embed-text` models. In non-systemd environments (like this VM), start with `ollama serve &` before using LLM features.
  - If missing in a fresh VM, install runtime: `curl -fsSL https://ollama.com/install.sh | sh` (if installer asks for `zstd`: `sudo apt-get update && sudo apt-get install -y zstd`).
  - Ensure default models are available: `ollama pull llama3.2:3b` and `ollama pull nomic-embed-text`.

### Development commands

All commands use `uv run` as the project manages dependencies with `uv`. See `CLAUDE.md` for the full list.

| Task | Command |
|------|---------|
| Install deps | `uv sync --all-groups` |
| Tests | `uv run pytest` |
| Format check | `uv run black --check .` |
| Lint | `uv run flake8 linkedin_api tests examples *.py` |
| Type check | `uv run mypy linkedin_api` |
| Gradio app | `uv run python -m linkedin_api.gradio_app` |

### Git & PR workflow

- **Commits**: commit after each individual change, using gitmojis comments (see https://gitmoji.dev/).
- **Push destination**: After committing, push to the current branch (e.g. `cursor/model-selection-by-stage-0735`). If you need a specific branch for review, it will be stated in the task.
- **PR title format**: Use `[TICKET-XX] Title` (e.g. `[LUC-60] Single pass posts report`) when the work is tied to a Linear ticket.
- **PR comments**: Always address review comments on the PR. Fetch them with `gh api repos/annemariet/linkedin-portability-explorer/pulls/<number>/comments` if needed.

### Gotchas

- **mypy must pass clean** (`uv run mypy linkedin_api`). All three linters (`black`, `flake8`, `mypy`) must pass before committing.
- **`uv` must be on PATH**: install with `curl -LsSf https://astral.sh/uv/install.sh | sh` and ensure `$HOME/.local/bin` is on PATH.
- **Commits**: Use conventional commits with gitmoji (see `CLAUDE.md`).
- **Before pushing**: Always run the checks above for the change you just made; only push once everything is green, and ensure you are on the correct feature branch (including the ticket id in the branch name when working on a Linear ticket).
- **Python 3.12+** is required (`requires-python = ">=3.12"` in `pyproject.toml`).
- If pipeline/report fails with `Cannot connect to Ollama` or `model ... not found`, verify `ollama list` shows `llama3.2:3b` and `nomic-embed-text`.
- **zstd** is pre-installed as a system dependency (used by Ollama for model compression).
- **Ollama serve must be started manually** in this VM since there is no systemd. Run `ollama serve &` and wait a few seconds before any LLM operations. The app's `_ensure_ollama_running()` in `llm_config.py` will also attempt auto-start.
- **Pipeline local tests**: Use `--last 1d` to reduce API fetch volume; `--limit 2` keeps enrichment/summarization cheap. With `--skip-fetch`, ensure changelog cache timestamps fall within the selected period.
- **Anthropic provider** uses the OpenAI SDK against `https://api.anthropic.com/v1/` — no `anthropic` Python package. This avoids `ThinkingBlock` parsing issues from the native Messages API.
