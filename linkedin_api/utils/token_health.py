"""Startup and CLI checks for LinkedIn token availability and expiry."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import requests

from linkedin_api.utils.auth import get_access_token
from linkedin_api.utils.changelog import BASE_URL, TokenExpiredError
from linkedin_api.utils.secret_retrieval import (
    SecretRetrievalIssue,
    retrieve_secret,
)
from linkedin_api.utils.token_fingerprint import detect_token_rotation
from linkedin_api.utils.token_lifecycle import (
    TokenExpiryLevel,
    assess_token_expiry,
    log_token_expiry_status,
)

logger = logging.getLogger(__name__)


class TokenHealthLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


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
    url = f"{BASE_URL}/memberChangeLogs"
    params: dict[str, str | int] = {
        "q": "criteria",
        "startTime": 0,
        "count": 1,
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
        "LinkedIn-Version": "202312",
    }
    try:
        response = requests.get(url, params=params, headers=headers, timeout=timeout)
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
    linkedin_account: str = "",
) -> TokenHealthReport:
    retrieval = retrieve_secret(log_scalingo_alerts=True)
    messages: list[str] = []

    if retrieval.issue != SecretRetrievalIssue.OK or not retrieval.value:
        if retrieval.issue == SecretRetrievalIssue.ENV_SET_BUT_EMPTY:
            messages.append(
                f"{retrieval.env_var} is set but empty — fix Scalingo configuration."
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
    expiry = assess_token_expiry(
        access_token=retrieval.value,
        linkedin_account=linkedin_account,
    )
    messages.append(expiry.message)

    api_valid: Optional[bool] = None
    level = TokenHealthLevel.OK

    if expiry.level == TokenExpiryLevel.EXPIRED:
        level = TokenHealthLevel.ERROR
    elif expiry.level == TokenExpiryLevel.WARN:
        level = TokenHealthLevel.WARN
    elif expiry.level == TokenExpiryLevel.UNKNOWN:
        level = TokenHealthLevel.WARN

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
    account = os.getenv("LINKEDIN_ACCOUNT", "")
    report = build_token_health_report(probe_api=probe_api, linkedin_account=account)

    for msg in report.messages:
        if report.level == TokenHealthLevel.ERROR:
            logger.error("linkedin_token_health %s", msg)
        elif report.level == TokenHealthLevel.WARN:
            logger.warning("linkedin_token_health %s", msg)
        else:
            logger.info("linkedin_token_health %s", msg)

    if report.retrieval_issue == SecretRetrievalIssue.OK:
        token = get_access_token()
        expiry = assess_token_expiry(
            access_token=token,
            linkedin_account=account,
        )
        log_token_expiry_status(expiry)

    return report


def ensure_token_available_or_raise() -> str:
    """Return access token or raise TokenExpiredError / ValueError with context."""
    token = get_access_token()
    if not token:
        raise ValueError("LINKEDIN_ACCESS_TOKEN not configured")
    expiry = assess_token_expiry(
        access_token=token,
        linkedin_account=os.getenv("LINKEDIN_ACCOUNT", ""),
    )
    if expiry.level == TokenExpiryLevel.EXPIRED:
        raise TokenExpiredError(expiry.message)
    return token
