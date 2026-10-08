"""LinkedIn access token expiry estimation and proactive warnings."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Optional

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Member Data Portability tokens are valid ~60 days (LinkedIn docs / operator practice).
DEFAULT_TOKEN_LIFETIME_DAYS = 60
DEFAULT_WARN_DAYS_BEFORE_EXPIRY = 14

_ISSUED_AT_ENV = "LINKEDIN_ACCESS_TOKEN_ISSUED_AT"
_EXPIRES_AT_ENV = "LINKEDIN_ACCESS_TOKEN_EXPIRES_AT"
_KEYRING_SERVICE = "LINKEDIN_ACCESS_TOKEN"
_KEYRING_ISSUED_ACCOUNT_SUFFIX = "#issued_at"


class TokenExpiryLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenExpiryStatus:
    level: TokenExpiryLevel
    days_remaining: Optional[int]
    expires_on: Optional[date]
    message: str


def _parse_date(value: str) -> Optional[date]:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        if "T" in text:
            return datetime.fromisoformat(text).date()
        return date.fromisoformat(text)
    except ValueError:
        return None


def _issued_at_keyring_account(linkedin_account: str) -> str:
    base = linkedin_account or "default"
    return f"{base}{_KEYRING_ISSUED_ACCOUNT_SUFFIX}"


def read_issued_at(
    linkedin_account: str = "",
) -> Optional[date]:
    env_val = os.getenv(_ISSUED_AT_ENV)
    if env_val:
        parsed = _parse_date(env_val)
        if parsed:
            return parsed

    if keyring is None:
        return None
    try:
        stored = keyring.get_password(
            _KEYRING_SERVICE, _issued_at_keyring_account(linkedin_account)
        )
        if stored:
            return _parse_date(stored)
    except Exception:
        return None
    return None


def read_expires_at(
    issued_at: Optional[date],
    *,
    lifetime_days: int = DEFAULT_TOKEN_LIFETIME_DAYS,
) -> Optional[date]:
    env_val = os.getenv(_EXPIRES_AT_ENV)
    if env_val:
        parsed = _parse_date(env_val)
        if parsed:
            return parsed
    if issued_at is None:
        return None
    return issued_at + timedelta(days=lifetime_days)


def store_issued_at_now(linkedin_account: str = "") -> None:
    today = datetime.now(timezone.utc).date().isoformat()
    if keyring is None:
        return
    keyring.set_password(
        _KEYRING_SERVICE,
        _issued_at_keyring_account(linkedin_account),
        today,
    )


def assess_token_expiry(
    *,
    warn_days: int = DEFAULT_WARN_DAYS_BEFORE_EXPIRY,
    lifetime_days: int = DEFAULT_TOKEN_LIFETIME_DAYS,
    linkedin_account: str = "",
    today: Optional[date] = None,
) -> TokenExpiryStatus:
    """Estimate expiry from issued/expires metadata; warn before deactivation."""
    ref = today or datetime.now(timezone.utc).date()
    issued = read_issued_at(linkedin_account)
    expires = read_expires_at(issued, lifetime_days=lifetime_days)

    if expires is None:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.UNKNOWN,
            days_remaining=None,
            expires_on=None,
            message=(
                "LinkedIn token expiry unknown — set "
                f"{_ISSUED_AT_ENV} (YYYY-MM-DD) when you rotate the token on "
                "Scalingo, or run scripts/setup_token.py locally to record the date."
            ),
        )

    days_remaining = (expires - ref).days
    if days_remaining < 0:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.EXPIRED,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token is past estimated expiry ({expires.isoformat()}). "
                "Renew at https://www.linkedin.com/developers/tools/oauth and update "
                f"{_ISSUED_AT_ENV} / LINKEDIN_ACCESS_TOKEN on Scalingo."
            ),
        )
    if days_remaining <= warn_days:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.WARN,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token estimated to expire on {expires.isoformat()} "
                f"({days_remaining} day(s) remaining). Renew before deactivation."
            ),
        )
    return TokenExpiryStatus(
        level=TokenExpiryLevel.OK,
        days_remaining=days_remaining,
        expires_on=expires,
        message=(
            f"LinkedIn token OK — estimated expiry {expires.isoformat()} "
            f"({days_remaining} day(s) remaining)."
        ),
    )


def log_token_expiry_status(status: TokenExpiryStatus) -> None:
    if status.level == TokenExpiryLevel.OK:
        logger.info("linkedin_token_expiry %s", status.message)
    elif status.level == TokenExpiryLevel.UNKNOWN:
        logger.warning("linkedin_token_expiry %s", status.message)
    elif status.level == TokenExpiryLevel.WARN:
        logger.warning("linkedin_token_expiry %s", status.message)
    else:
        logger.error("linkedin_token_expiry %s", status.message)
