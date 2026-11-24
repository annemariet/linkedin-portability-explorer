"""Shared keyring service names (host-agnostic, configurable via env)."""

from __future__ import annotations

import os

_DEFAULT_SERVICE = "linkedin-portability-explorer"
_SERVICE_ENV = "LINKEDIN_KEYRING_SERVICE"
_LEGACY_ENV = "LINKEDIN_KEYRING_LEGACY_SERVICES"


def primary_keyring_service() -> str:
    """Primary keyring service name for this project's secrets."""
    raw = os.getenv(_SERVICE_ENV, "").strip()
    return raw or _DEFAULT_SERVICE


def keyring_service_candidates() -> tuple[str, ...]:
    """
    Service names to try, in order: primary then optional legacy aliases.

    Set ``LINKEDIN_KEYRING_LEGACY_SERVICES`` to a comma-separated list when
    migrating secrets from an older keyring layout (see docs/keyring-migration.md).
    """
    primary = primary_keyring_service()
    legacy_raw = os.getenv(_LEGACY_ENV, "")
    legacy = [part.strip() for part in legacy_raw.split(",") if part.strip()]
    seen: set[str] = set()
    ordered: list[str] = []
    for name in (primary, *legacy):
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return tuple(ordered)
