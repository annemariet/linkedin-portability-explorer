"""LinkedIn OAuth authorization-code flow (read app credentials; never log secrets)."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from linkedin_api.utils.token_secrets import read_app_credential

_AUTHORIZE_URL = "https://www.linkedin.com/oauth/v2/authorization"
_TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
_DEFAULT_SCOPE = "r_dma_portability_self_serve"
_DEFAULT_REDIRECT = "http://127.0.0.1:8765/linkedin/oauth/callback"


@dataclass(frozen=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scope: str


@dataclass(frozen=True)
class TokenExchangeResult:
    access_token: str
    refresh_token: Optional[str]
    expires_in: Optional[int]


def load_oauth_client_config() -> OAuthClientConfig:
    import os

    client_id = read_app_credential("LINKEDIN_CLIENT_ID")
    client_secret = read_app_credential("LINKEDIN_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise ValueError(
            "LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET must be set (env or keyring)"
        )
    redirect_uri = (
        os.getenv("LINKEDIN_OAUTH_REDIRECT_URI", _DEFAULT_REDIRECT).strip()
        or _DEFAULT_REDIRECT
    )
    scope = (os.getenv("LINKEDIN_OAUTH_SCOPE") or _DEFAULT_SCOPE).strip()
    return OAuthClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scope,
    )


def build_authorization_url(
    config: OAuthClientConfig, *, state: Optional[str] = None
) -> str:
    csrf = state or secrets.token_urlsafe(16)
    query = urlencode(
        {
            "response_type": "code",
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "scope": config.scope,
            "state": csrf,
        }
    )
    return f"{_AUTHORIZE_URL}?{query}"


def parse_authorization_code(redirect_or_code: str) -> str:
    text = redirect_or_code.strip()
    if not text:
        raise ValueError("empty authorization response")
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        params = parse_qs(parsed.query)
        codes = params.get("code") or []
        if not codes or not codes[0].strip():
            raise ValueError("authorization response missing code query parameter")
        return codes[0].strip()
    return text


def exchange_authorization_code(
    config: OAuthClientConfig,
    code: str,
    *,
    timeout: float = 30.0,
) -> TokenExchangeResult:
    try:
        response = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": config.redirect_uri,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError("token exchange request failed") from exc

    if response.status_code >= 400:
        raise RuntimeError(f"token exchange failed with HTTP {response.status_code}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("token exchange returned invalid JSON") from exc

    access = payload.get("access_token")
    if not isinstance(access, str) or not access.strip():
        raise RuntimeError("token exchange response missing access_token")

    refresh = payload.get("refresh_token")
    refresh_token = (
        refresh.strip() if isinstance(refresh, str) and refresh.strip() else None
    )
    expires_in_raw = payload.get("expires_in")
    expires_in = expires_in_raw if isinstance(expires_in_raw, int) else None

    return TokenExchangeResult(
        access_token=access.strip(),
        refresh_token=refresh_token,
        expires_in=expires_in,
    )
