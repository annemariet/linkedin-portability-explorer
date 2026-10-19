# LinkedIn access token health

Host-agnostic **check** tooling for any deployment of this app (local, VM, PaaS, Kubernetes, etc.).

## Check (scheduler)

Run daily (or on your own cadence) from the app environment:

```bash
uv run linkedin-check-token --warn-exit-code
```

Exit codes:

| Code | Meaning |
|------|---------|
| `0` | Token present; expiry known and not in the warning window |
| `1` | Hard failure (missing/empty secret, keyring error, expired token, or `--probe-api` rejection) |
| `2` | Token in the **warning window** (default: 14 days before LinkedIn introspection `expires_at`; requires `--warn-exit-code`) |
| `3` | Token present but **expiry unknown** (missing `LINKEDIN_CLIENT_ID` / `LINKEDIN_CLIENT_SECRET` for introspection) |

Optional API probe (extra LinkedIn call):

```bash
uv run linkedin-check-token --probe-api --warn-exit-code
```

Wire your scheduler to alert on non-zero exits (cron, systemd timer, CI workflow, Kubernetes CronJob, etc.). Example cron line:

```cron
0 8 * * * cd /path/to/linkedin-portability-explorer && uv run linkedin-check-token --warn-exit-code
```

## Expiry warnings (introspection)

When **`LINKEDIN_CLIENT_ID`** and **`LINKEDIN_CLIENT_SECRET`** are set, the checker uses LinkedIn [token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection) for `expires_at`. Without app credentials, expiry status is **unknown** (exit `3`; presence checks still run).

## Gradio / web boot

`linkedin-gradio` logs **`linkedin_token_health`** once on startup (optional **`LINKEDIN_TOKEN_PROBE_ON_STARTUP=1`** for an API probe). Token rotation is logged as **`linkedin_access_token_rotated`** when the fingerprint file under `LINKEDIN_DATA_DIR` changes (that file is gitignored).

## Update (secret store)

This repo does **not** perform OAuth refresh automatically. Member tokens are re-issued via LinkedIn’s developer OAuth flow (~60-day lifetime).

**Local:** `uv run python scripts/setup_token.py` (interactive keyring).

**Deployed:** after you obtain a new token, update your secret store the way your platform expects (environment variable, vault, etc.) and restart or redeploy. A standardized apply hook may be added later; it is not implemented yet.

## Logs (no secret values)

- `secret_retrieval` — env empty, missing, or keyring failure
- `linkedin_token_health` — startup / CLI health summary
