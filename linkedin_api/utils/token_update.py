"""CLI: OAuth update flow for LinkedIn access token, then health check."""

from __future__ import annotations

import argparse
import sys
from typing import Optional

from linkedin_api.utils.token_check import (
    build_token_health_report,
    exit_code_for_report,
    print_token_health_report,
)
from linkedin_api.utils.token_oauth import (
    build_authorization_url,
    exchange_authorization_code,
    load_oauth_client_config,
    parse_authorization_code,
)
from linkedin_api.utils.token_secrets import (
    run_token_apply_hook,
    store_access_token,
    store_refresh_token,
)


def _read_code_interactively() -> str:
    print(
        "Paste the full redirect URL or the authorization code "
        "(input is not echoed in logs):"
    )
    line = sys.stdin.readline()
    return parse_authorization_code(line)


def run_update(
    *,
    redirect_or_code: Optional[str] = None,
    skip_check: bool = False,
) -> int:
    try:
        config = load_oauth_client_config()
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    auth_url = build_authorization_url(config)
    print("Open this URL in a browser, authorize, then paste the redirect URL or code:")
    print(auth_url)

    try:
        raw = redirect_or_code if redirect_or_code else _read_code_interactively()
        code = parse_authorization_code(raw)
        tokens = exchange_authorization_code(config, code)
    except (ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}")
        return 1

    try:
        store_access_token(tokens.access_token)
        if tokens.refresh_token:
            store_refresh_token(tokens.refresh_token)
        run_token_apply_hook(tokens.access_token)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        return 1

    print("OK: access token stored in keyring.")
    if tokens.refresh_token:
        print("OK: refresh token stored in keyring.")
    if tokens.expires_in is not None:
        print(f"OK: provider reported expires_in={tokens.expires_in} seconds.")

    if skip_check:
        return 0

    report = build_token_health_report(probe_api=False)
    print_token_health_report(report)
    return exit_code_for_report(report, warn_exit_code=False)


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(
        description="Update LinkedIn access token via OAuth authorization code"
    )
    parser.add_argument(
        "--redirect-or-code",
        help="Redirect URL or authorization code (skip interactive paste)",
    )
    parser.add_argument(
        "--skip-check",
        action="store_true",
        help="Store token only; do not run linkedin-check-token afterwards",
    )
    args = parser.parse_args(argv)
    sys.exit(
        run_update(redirect_or_code=args.redirect_or_code, skip_check=args.skip_check)
    )


if __name__ == "__main__":
    main()
