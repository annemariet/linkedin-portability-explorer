"""LinkedIn OAuth token introspection (expires_at / created_at from LinkedIn)."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

import requests

logger = logging.getLogger(__name__)

_INTROSPECT_URL = "https://www.linkedin.com/oauth/v2/introspectToken"


@dataclass(frozen=True)
class TokenIntrospection:
    active: bool
    status: Optional[str]
    expires_on: Optional[date]
    issued_on: Optional[date]
    scope: Optional[str]


def _client_credentials() -> tuple[Optional[str], Optional[str]]:
    client_id = (os.getenv("LINKEDIN_CLIENT_ID") or "").strip() or None
    client_secret = (os.getenv("LINKEDIN_CLIENT_SECRET") or "").strip() or None
    return client_id, client_secret


def _epoch_to_date(epoch_seconds: int) -> date:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).date()


def introspect_access_token(
    access_token: str,
    *,
    timeout: float = 15.0,
) -> Optional[TokenIntrospection]:
    """
    Call LinkedIn introspectToken when app client id/secret are configured.

    See: https://learn.microsoft.com/en-us/linkedin/shared/authentication/token-introspection
    """
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
    if isinstance(status, str):
        status_val: Optional[str] = status
    else:
        status_val = None
    if isinstance(scope, str):
        scope_val: Optional[str] = scope
    else:
        scope_val = None

    return TokenIntrospection(
        active=active,
        status=status_val,
        expires_on=expires_on,
        issued_on=issued_on,
        scope=scope_val,
    )
