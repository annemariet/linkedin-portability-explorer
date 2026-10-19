---
name: github-use
description: Git and GitHub workflow — branches, commits, PRs, checks, and LinkedIn-data validation when relevant.
---

# GitHub

## Branches and commits

- **Feature branches only:** branch off `main` (or the maintainer’s agreed base). Never commit or push directly to `main`.
- **Never merge** into `main` unless the maintainer explicitly asks.
- **Never force-push** or **rebase shared branches** others may have checked out.
- **Commits:** small and focused; [gitmoji](https://gitmoji.dev/) + conventional style (see `CLAUDE.md`).

## Before pushing

Run this repo’s checks and fix failures (or call them out in the PR):

```bash
uv run black --check .
uv run flake8 linkedin_api tests examples *.py
uv run mypy linkedin_api
uv run pytest
```

## Pull requests

- **Title:** `<gitmoji> (<scope>) Short summary` in plain English (e.g. `📝 (skills) Make agent skills self-contained`). **No ticket IDs** in titles.
- **Body:** what changed, why, and how it was validated. Prefer **linked, labelled references** (e.g. “Fixes #42”, “Compared to commit abc1234 on main”) instead of bare issue numbers or SHAs with no context.
- **Review comments:** address feedback on the PR; use `gh` when helpful (e.g. `gh pr view`, `gh api repos/annemariet/linkedin-portability-explorer/pulls/<number>/comments`).
- **LinkedIn pipeline changes:** when fetch, enrich, content store, or pipeline behavior changes, add a Validation section per [validation.md](./validation.md).

## GitHub CLI

- `gh pr list`, `gh pr view`, `gh pr comment` — inspect PRs and respond to review when the task requires it.
