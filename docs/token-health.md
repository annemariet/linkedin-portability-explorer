# LinkedIn access token health

Host-agnostic **check** and **update** tooling for any deployment of this app.

## Check (scheduler)

```bash
uv run linkedin-check-token --warn-exit-code
```

| Exit code | Meaning |
|-----------|---------|
| `0` | Token present; expiry known and not in the warning window |
| `1` | Hard failure (missing/empty secret, keyring error, expired token, or `--probe-api` rejection) |
| `2` | Expiry in the warning window (requires `--warn-exit-code`) |
| `3` | Token present but expiry **unknown** (app credentials missing for introspection) |

Optional API probe: `uv run linkedin-check-token --probe-api --warn-exit-code`

Wire your scheduler to alert on non-zero exits (cron, systemd timer, Kubernetes CronJob, etc.).

## Update (OAuth)

```bash
uv run linkedin-update-token
```

Or non-interactive:

```bash
uv run linkedin-update-token --redirect-or-code 'https://your.redirect/callback?code=...'
```

Flow:

1. Reads **app credentials only** (never writes them): `LINKEDIN_CLIENT_ID` and `LINKEDIN_CLIENT_SECRET` from the environment or OS keyring. Keyring lookups use service name = env var name and account = `LINKEDIN_ACCOUNT`; if nothing is stored under that account, an entry with an **empty account** (`-a ""` on macOS) is also tried.
2. Prints a browser-ready LinkedIn authorization URL (scope default `r_dma_portability_self_serve`; override with `LINKEDIN_OAUTH_SCOPE`). The app client id appears **only** in that URL on your terminal.
3. You authorize in the browser; paste the **full redirect URL** (must include `code` and `state`; bare codes are rejected).
4. Exchanges the code for an access token (and refresh token when LinkedIn returns one). **Tokens and the client secret are never logged.**
5. Stores the access token in the keyring under **`LINKEDIN_KEYRING_SERVICE`** when set, otherwise service name `LINKEDIN_ACCESS_TOKEN` (account `LINKEDIN_ACCOUNT`). Refresh tokens use service `LINKEDIN_REFRESH_TOKEN`.
6. Optionally runs **`LINKEDIN_TOKEN_APPLY_CMD`**: argv parsed with `shlex` (no shell); the new access token is sent on **stdin** only. Timeout: **`LINKEDIN_TOKEN_APPLY_TIMEOUT`** seconds (default 60).
7. Runs the same health check as `linkedin-check-token` so you see the new expiry when introspection is configured.

Configure the redirect URI to match your LinkedIn app settings:

```bash
export LINKEDIN_OAUTH_REDIRECT_URI='http://127.0.0.1:8765/linkedin/oauth/callback'
```

If `LINKEDIN_ACCESS_TOKEN` is still set in the environment, it **overrides** keyring on read — unset it or update the env var after renewal so the new keyring value is used.

Legacy manual paste: `uv run python scripts/setup_token.py` still works.

## App credentials in macOS Keychain

Set `LINKEDIN_ACCOUNT` in `.env` when you use a named keychain account; leave it unset to match entries stored with an empty account.

**macOS (client secret, prompts for value with `-w`):**

```bash
security add-generic-password -U -s LINKEDIN_CLIENT_SECRET -a "${LINKEDIN_ACCOUNT:-}" -w
```

When `LINKEDIN_ACCOUNT` is unset, use `-a ""` explicitly:

```bash
security add-generic-password -U -s LINKEDIN_CLIENT_SECRET -a "" -w
```

**Cross-platform (Python keyring):**

```bash
python -m keyring set LINKEDIN_CLIENT_SECRET "${LINKEDIN_ACCOUNT:-}"
```

Use the same pattern with service `LINKEDIN_CLIENT_ID` for the app client id (not the member access token).

## Expiry warnings (introspection)

With **`LINKEDIN_CLIENT_ID`** and **`LINKEDIN_CLIENT_SECRET`**, the checker calls LinkedIn [token introspection](https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection). Without them, expiry is unknown (exit `3`); CLI output lists what is missing and repeats the keychain commands above.

## Gradio / web boot

`linkedin-gradio` logs **`linkedin_token_health`** once on startup (`LINKEDIN_TOKEN_PROBE_ON_STARTUP=1` for an API probe). Rotation logs **`linkedin_access_token_rotated`** when the fingerprint under `LINKEDIN_DATA_DIR` changes (gitignored).

## Logs (no secret values)

- `secret_retrieval` — env empty, missing, or keyring failure
- `linkedin_token_health` — startup / CLI health summary

App credentials and member tokens must not appear in logs.
