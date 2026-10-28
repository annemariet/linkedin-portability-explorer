"""LinkedIn token introspection and expiry assessment."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
from typing import Optional

import requests

from linkedin_api.utils.token_secrets import (
    format_missing_app_credentials_message,
    missing_app_credential_names,
    read_app_credential,
)

logger = logging.getLogger(__name__)

_INTROSPECT_URL = "https://www.linkedin.com/oauth/v2/introspectToken"
DEFAULT_WARN_DAYS_BEFORE_EXPIRY = 14

_INTROSPECTION_UNAVAILABLE_MESSAGE = (
    "Token expiry unknown — LinkedIn introspection did not return expiry data."
)


def _unknown_expiry_message() -> str:
    missing = missing_app_credential_names()
    if missing:
        return format_missing_app_credentials_message(missing)
    return _INTROSPECTION_UNAVAILABLE_MESSAGE


def _introspection_date_clause(intro: TokenIntrospection, *, expired: bool) -> str:
    parts: list[str] = []
    if intro.issued_on is not None:
        parts.append(f"created {intro.issued_on.isoformat()}")
    if intro.expires_on is not None:
        if expired:
            parts.append(f"expired {intro.expires_on.isoformat()}")
        else:
            parts.append(f"expires {intro.expires_on.isoformat()}")
    return ", ".join(parts)


class TokenExpiryLevel(str, Enum):
    OK = "ok"
    WARN = "warn"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TokenIntrospection:
    active: bool
    status: Optional[str]
    expires_on: Optional[date]
    issued_on: Optional[date]
    scope: Optional[str]


@dataclass(frozen=True)
class TokenExpiryStatus:
    level: TokenExpiryLevel
    days_remaining: Optional[int]
    expires_on: Optional[date]
    message: str


def _client_credentials() -> tuple[Optional[str], Optional[str]]:
    client_id = read_app_credential("LINKEDIN_CLIENT_ID")
    client_secret = read_app_credential("LINKEDIN_CLIENT_SECRET")
    return client_id, client_secret


def _epoch_to_date(epoch_seconds: int) -> date:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date()


def introspect_access_token(
    access_token: str,
    *,
    timeout: float = 15.0,
) -> Optional[TokenIntrospection]:
    """Call LinkedIn introspectToken when app client id/secret are configured."""
    client_id, client_secret = _client_credentials()
    if not client_id or not client_secret:
        return None

    try:
        response = requests.post(
            _INTROSPECT_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "token": access_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        logger.warning("linkedin_token_introspection request failed: %s", exc)
        return None

    if response.status_code >= 400:
        logger.warning(
            "linkedin_token_introspection HTTP %s (body not logged)",
            response.status_code,
        )
        return None

    try:
        payload = response.json()
    except ValueError:
        logger.warning("linkedin_token_introspection invalid JSON response")
        return None

    expires_on: Optional[date] = None
    issued_on: Optional[date] = None
    expires_at = payload.get("expires_at")
    created_at = payload.get("created_at")
    if isinstance(expires_at, int):
        expires_on = _epoch_to_date(expires_at)
    if isinstance(created_at, int):
        issued_on = _epoch_to_date(created_at)

    active = bool(payload.get("active"))
    status = payload.get("status")
    scope = payload.get("scope")
    status_val = status if isinstance(status, str) else None
    scope_val = scope if isinstance(scope, str) else None

    return TokenIntrospection(
        active=active,
        status=status_val,
        expires_on=expires_on,
        issued_on=issued_on,
        scope=scope_val,
    )


def assess_token_expiry(
    *,
    access_token: Optional[str] = None,
    warn_days: int = DEFAULT_WARN_DAYS_BEFORE_EXPIRY,
    today: Optional[date] = None,
    introspection: Optional[TokenIntrospection] = None,
) -> TokenExpiryStatus:
    """Expiry warnings when LinkedIn introspection returns expires_at."""
    ref = today or datetime.now(timezone.utc).date()

    intro = introspection
    if intro is None and access_token:
        intro = introspect_access_token(access_token)

    if intro is None:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.UNKNOWN,
            days_remaining=None,
            expires_on=None,
            message=_unknown_expiry_message(),
        )

    if intro.status in ("expired", "revoked") or not intro.active:
        ref_expiry = intro.expires_on or ref
        days_remaining = (ref_expiry - ref).days
        dates = _introspection_date_clause(intro, expired=True)
        status_part = (
            f"LinkedIn reports token status={intro.status or 'inactive'} "
            f"(introspection"
        )
        if dates:
            status_part += f", {dates}"
        status_part += "). Renew at https://www.linkedin.com/developers/tools/oauth"
        return TokenExpiryStatus(
            level=TokenExpiryLevel.EXPIRED,
            days_remaining=days_remaining,
            expires_on=intro.expires_on,
            message=status_part,
        )

    expires = intro.expires_on
    if expires is None:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.UNKNOWN,
            days_remaining=None,
            expires_on=None,
            message=_INTROSPECTION_UNAVAILABLE_MESSAGE,
        )

    days_remaining = (expires - ref).days
    dates_active = _introspection_date_clause(intro, expired=False)
    date_suffix = f" ({dates_active})" if dates_active else ""

    if days_remaining < 0:
        dates_exp = _introspection_date_clause(intro, expired=True)
        detail = dates_exp or f"expired {expires.isoformat()}"
        return TokenExpiryStatus(
            level=TokenExpiryLevel.EXPIRED,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token expired ({detail}). "
                "Renew at https://www.linkedin.com/developers/tools/oauth"
            ),
        )
    if days_remaining <= warn_days:
        return TokenExpiryStatus(
            level=TokenExpiryLevel.WARN,
            days_remaining=days_remaining,
            expires_on=expires,
            message=(
                f"LinkedIn access token expires soon{date_suffix} "
                f"({days_remaining} day(s) remaining). Renew before deactivation."
            ),
        )
    return TokenExpiryStatus(
        level=TokenExpiryLevel.OK,
        days_remaining=days_remaining,
        expires_on=expires,
        message=(
            f"LinkedIn token OK{date_suffix} " f"({days_remaining} day(s) remaining)."
        ),
    )
