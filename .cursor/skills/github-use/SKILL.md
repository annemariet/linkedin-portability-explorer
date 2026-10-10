---
name: github-use
description: Pointer to canonical amai-lab Git/GitHub workflow for this repo.
---

# GitHub (canonical policy)

**Do not maintain a second dialect here.** For branches, commits, PR titles, and push rules, follow the canonical skill:

**[amai-lab `github-use`](https://github.com/annemariet/amai-lab/blob/main/skills/github-use/SKILL.md)** (dialect A: gitmoji + `(scope)` PR titles, feature branches only, never push or merge `main`).

## Repo-specific addendum

- **Pre-push checks for this project:** `uv run black --check .`, `uv run flake8 linkedin_api tests examples *.py`, `uv run mypy linkedin_api`, `uv run pytest` (see `AGENTS.md`).
- **LinkedIn data PR validation:** when a change touches fetch, enrich, content store, or pipeline behavior, use [validation.md](./validation.md) in the PR description.
