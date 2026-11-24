# Keyring service names

Secrets can live in the OS keyring under a **service** name plus **account** (see `LINKEDIN_ACCOUNT` for LinkedIn/Tavily entries that use this repo’s convention).

## Default layout

| Secret | Service (default) | Account |
|--------|-------------------|---------|
| LinkedIn token | `LINKEDIN_ACCESS_TOKEN` | value of `LINKEDIN_ACCOUNT` |
| Tavily API key | `TAVILY_API_KEY` | value of `LINKEDIN_ACCOUNT` |
| Mammouth / OpenAI-compatible LLM key | `linkedin-portability-explorer` | `mammouth`, `mammouth_api_key`, or `openai` |
| Anthropic key | `linkedin-portability-explorer` | `Anthropic`, `anthropic`, or `anthropic_api_key` |
| Tavily (alternate account) | `linkedin-portability-explorer` | `tavily` |

Override the shared service name:

```bash
export LINKEDIN_KEYRING_SERVICE=my-team-linkedin
```

## Migrating from older keyring layouts

If keys were stored under **legacy service names**, re-save them under the default (or your `LINKEDIN_KEYRING_SERVICE`), **or** set legacy aliases until you migrate:

```bash
export LINKEDIN_KEYRING_LEGACY_SERVICES=old-service-a,old-service-b
```

The app tries the primary service first, then each legacy name. Remove `LINKEDIN_KEYRING_LEGACY_SERVICES` once keys are moved.

Example (macOS Keychain via `keyring` CLI):

```bash
# Copy Tavily key to the default service (adjust account names to match yours)
keyring set linkedin-portability-explorer tavily
```

Environment variables (`LINKEDIN_ACCESS_TOKEN`, `TAVILY_API_KEY`, `LLM_API_KEY`, etc.) always win over keyring when set and non-empty.
