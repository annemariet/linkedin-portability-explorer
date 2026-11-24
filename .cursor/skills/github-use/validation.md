# PR validation (LinkedIn data)

When the change touches fetch, enrich, content store, or pipeline behavior, and a human or agent **can** run against real LinkedIn data (`LINKEDIN_ACCESS_TOKEN` valid in that environment), add a **Validation** section to the PR that is **systematic** enough to rerun and **safe** to paste publicly.

## Pin the run

- **Branch and commit:** branch name + `git rev-parse --short HEAD`; if comparing to `main`, note **`main` at** the baseline short SHA you used.
- **Isolated data dir:** set **`LINKEDIN_DATA_DIR`** to a **dedicated path** per run (e.g. `/tmp/prNN_main` vs `/tmp/prNN_branch`) so outputs do not overwrite local dev data and diffs are comparable.
- **Scope:** state the **time window** (e.g. `summarize_activity --last 1d`) and **slice** (e.g. last **N** rows of `activities.csv`, **`enrich_activities --limit N`**).

## Commands (copy-pasteable)

Include the **exact** sequence used, for example:

1. `uv sync --all-groups` (if needed)
2. `uv run python -m linkedin_api.summarize_activity --last 1d` (or the project’s fetch step)
3. Build a small CSV (e.g. `tail -n N "$LINKEDIN_DATA_DIR/activities.csv" > /tmp/…`)
4. `ENRICH_TELEMETRY=1 uv run python -m linkedin_api.enrich_activities … --limit N` (when testing enrichment)

If the token is **missing or expired**, say so in the PR (**do not** paste secrets). Partial validation (unit tests only) is OK if labeled as such.

## What to paste back

- **Exit codes / outcome:** fetch and enrich succeeded or failed (e.g. 401 / expired token).
- **Telemetry:** one line or summary of **`ENRICH_TELEMETRY`** counters when enrichment ran.
- **Diff or summary:** e.g. `diff -ruN` between two isolated `content/` trees, or bullets: which files changed, whether `.md` vs `.meta.json` differed, any known skips (login wall, empty window).

## Redaction

- **Never** paste `LINKEDIN_ACCESS_TOKEN` or other secrets.
- Truncate or generalize **sensitive URLs** in prose if needed; hashed content-store stems are fine.

## Optional

- Attach a **sanitized** log file (gist or PR comment) if stdout is long; keep the PR body to a **short** summary plus link.
