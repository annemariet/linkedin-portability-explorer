"""Configurable keyring service name(s) for shared secrets."""

from __future__ import annotations

import os

_DEFAULT_SERVICE = "linkedin-portability-explorer"
_ENV_VAR = "LINKEDIN_KEYRING_SERVICE"


def keyring_service_candidates() -> tuple[str, ...]:
    """
    Service names to try, in order.

    ``LINKEDIN_KEYRING_SERVICE`` defaults to ``linkedin-portability-explorer``.
    Use a comma-separated list to try additional names during migration.
    """
    raw = os.getenv(_ENV_VAR, _DEFAULT_SERVICE)
    names = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(names) if names else (_DEFAULT_SERVICE,)
