"""LinkedIn access token expiry from OAuth introspection only."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

from linkedin_api.utils.token_introspection import (
    TokenIntrospection,
    introspect_access_token,
)

logger = logging.getLogger(__name__)

DEFAULT_WARN_DAYS_BEFORE_EXPIRY = 14


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


def assess_token_expiry(
    *,
    access_token: Optional[str] = None,
    warn_days: int = DEFAULT_WARN_DAYS_BEFORE_EXPIRY,
    today: Optional[date] = None,
    introspection: Optional[TokenIntrospection] = None,
) -> TokenExpiryStatus:
    """Expiry warnings only when LinkedIn introspection returns expires_at."""
    ref = today or datetime.now(timezone.utc).date()

    intro = introspection
    if intro is None and access_token:
        intro = introspect_access_token(access_token)

    if intro is None:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.UNKNOWN,
            days_remaining=None,
            expires_on=None,
            message="",
        )

    if intro.status in ("expired", "revoked") or not intro.active:
        ref_expiry = intro.expires_on or ref
        days_remaining = (ref_expiry - ref).days
        return TokenExpiryStatus(
            level=TokenExpiryLevel.EXPIRED,
            days_remaining=days_remaining,
            expires_on=intro.expires_on,
            message=(
                f"LinkedIn reports token status={intro.status or 'inactive'} "
                f"(introspection). Renew at "
                "https://www.linkedin.com/developers/tools/oauth"
            ),
        )

    expires = intro.expires_on
    if expires is None:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.UNKNOWN,
            days_remaining=None,
            expires_on=None,
            message="",
        )

    days_remaining = (expires - ref).days
    if days_remaining < 0:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.EXPIRED,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token expired on {expires.isoformat()}. "
                "Renew at https://www.linkedin.com/developers/tools/oauth"
            ),
        )
    if days_remaining <= warn_days:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.WARN,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token expires on {expires.isoformat()} "
                f"({days_remaining} day(s) remaining). Renew before deactivation."
            ),
        )
    return TokenExpiryStatus(
        level=TokenExpiryLevel.OK,
        days_remaining=days_remaining,
        expires_on=expires,
        message=(
            f"LinkedIn token OK — expires {expires.isoformat()} "
            f"({days_remaining} day(s) remaining)."
        ),
    )


def log_token_expiry_status(status: TokenExpiryStatus) -> None:
    if status.level == TokenExpiryLevel.UNKNOWN:
        return
    if status.level == TokenExpiryLevel.OK:
        logger.info("linkedin_token_expiry %s", status.message)
    elif status.level == TokenExpiryLevel.WARN:
        logger.warning("linkedin_token_expiry %s", status.message)
    else:
        logger.error("linkedin_token_expiry %s", status.message)
