"""Token health report, API probe, Gradio startup logging, and CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

from linkedin_api.utils.auth import build_linkedin_session
from linkedin_api.utils.changelog import BASE_URL
from linkedin_api.utils.token_expiry import (
    TokenExpiryLevel,
    assess_token_expiry,
)
from linkedin_api.utils.token_secrets import (
    SecretRetrievalIssue,
    detect_token_rotation,
    retrieve_secret,
)

logger = logging.getLogger(__name__)

# CLI exit codes (documented in docs/token-health.md)
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_WARN = 2
EXIT_EXPIRY_UNKNOWN = 3


class TokenHealthLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenHealthReport:
    level: TokenHealthLevel
    retrieval_issue: SecretRetrievalIssue
    expiry_level: Optional[TokenExpiryLevel]
    api_valid: Optional[bool]
    messages: tuple[str, ...]


def _probe_token_api(
    access_token: str, timeout: float = 15.0
) -> tuple[bool, Optional[str]]:
    """Lightweight changelog request; True if token accepted."""
    session = build_linkedin_session(access_token)
    params: dict[str, str | int] = {
        "q": "criteria",
        "startTime": 0,
        "count": 1,
    }
    url = f"{BASE_URL}/memberChangeLogs"
    try:
        response = session.get(url, params=params, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"API probe failed: {exc}"

    if response.status_code == 401 and "EXPIRED_ACCESS_TOKEN" in response.text:
        return False, "LinkedIn API returned EXPIRED_ACCESS_TOKEN"
    if response.status_code == 401:
        return False, "LinkedIn API returned 401 Unauthorized"
    if response.status_code >= 400:
        return False, f"LinkedIn API returned HTTP {response.status_code}"
    return True, None


def build_token_health_report(
    *,
    probe_api: bool = False,
) -> TokenHealthReport:
    retrieval = retrieve_secret(log_failures=True)
    messages: list[str] = []

    if retrieval.issue != SecretRetrievalIssue.OK or not retrieval.value:
        if retrieval.issue == SecretRetrievalIssue.ENV_SET_BUT_EMPTY:
            messages.append(f"{retrieval.env_var} is set but empty.")
        elif retrieval.issue == SecretRetrievalIssue.KEYRING_ERROR:
            messages.append(
                f"Keyring lookup failed for {retrieval.env_var} "
                "(see logs: secret_retrieval)."
            )
        elif retrieval.issue == SecretRetrievalIssue.MISSING:
            messages.append(f"{retrieval.env_var} not found (env or keyring).")
        else:
            messages.append(
                f"Could not resolve {retrieval.env_var} "
                f"(issue={retrieval.issue.value})."
            )
        return TokenHealthReport(
            level=TokenHealthLevel.ERROR,
            retrieval_issue=retrieval.issue,
            expiry_level=None,
            api_valid=None,
            messages=tuple(messages),
        )

    detect_token_rotation(retrieval.value)
    expiry = assess_token_expiry(access_token=retrieval.value)

    api_valid: Optional[bool] = None
    level = TokenHealthLevel.OK

    if expiry.message:
        messages.append(expiry.message)

    if expiry.level == TokenExpiryLevel.EXPIRED:
        level = TokenHealthLevel.ERROR
    elif expiry.level == TokenExpiryLevel.WARN:
        level = TokenHealthLevel.WARN
    elif expiry.level == TokenExpiryLevel.UNKNOWN:
        level = TokenHealthLevel.UNKNOWN
        logger.warning("linkedin_token_health %s", expiry.message)

    if probe_api:
        ok, err = _probe_token_api(retrieval.value)
        api_valid = ok
        if not ok:
            messages.append(err or "Token rejected by LinkedIn API.")
            level = TokenHealthLevel.ERROR

    return TokenHealthReport(
        level=level,
        retrieval_issue=retrieval.issue,
        expiry_level=expiry.level,
        api_valid=api_valid,
        messages=tuple(messages),
    )


def log_startup_token_health(probe_api: bool = False) -> TokenHealthReport:
    """Log token retrieval, expiry, and optional API probe (Gradio / web boot)."""
    report = build_token_health_report(probe_api=probe_api)

    for msg in report.messages:
        if report.level == TokenHealthLevel.ERROR:
            logger.error("linkedin_token_health %s", msg)
        elif report.level in (TokenHealthLevel.WARN, TokenHealthLevel.UNKNOWN):
            logger.warning("linkedin_token_health %s", msg)
        else:
            logger.info("linkedin_token_health %s", msg)

    return report


def exit_code_for_report(report: TokenHealthReport, *, warn_exit_code: bool) -> int:
    if report.level == TokenHealthLevel.ERROR:
        return EXIT_ERROR
    if report.level == TokenHealthLevel.UNKNOWN:
        return EXIT_EXPIRY_UNKNOWN
    if warn_exit_code and report.level == TokenHealthLevel.WARN:
        return EXIT_WARN
    return EXIT_OK


def _print_report(report: TokenHealthReport) -> None:
    prefix = {
        TokenHealthLevel.ERROR: "ERROR:",
        TokenHealthLevel.WARN: "WARN:",
        TokenHealthLevel.UNKNOWN: "UNKNOWN:",
        TokenHealthLevel.OK: "OK:",
    }[report.level]
    for msg in report.messages:
        print(f"{prefix} {msg}")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="Check LinkedIn access token health")
    parser.add_argument(
        "--probe-api",
        action="store_true",
        help="Call LinkedIn changelog API to verify the token is accepted",
    )
    parser.add_argument(
        "--warn-exit-code",
        action="store_true",
        help="Exit 2 when expiry is in the warning window (for scheduler alerts)",
    )
    args = parser.parse_args(argv)

    report = build_token_health_report(probe_api=args.probe_api)
    _print_report(report)
    sys.exit(exit_code_for_report(report, warn_exit_code=args.warn_exit_code))


if __name__ == "__main__":
    main()
