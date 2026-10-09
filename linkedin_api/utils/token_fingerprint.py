"""Detect access-token rotation without storing the secret."""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from linkedin_api.activity_csv import get_data_dir

logger = logging.getLogger(__name__)

_FINGERPRINT_FILE = ".linkedin_access_token_fingerprint"


def _fingerprint_path() -> Path:
    return get_data_dir() / _FINGERPRINT_FILE


def token_fingerprint(access_token: str) -> str:
    digest = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
    return digest


def detect_token_rotation(access_token: str) -> bool:
    """
    Return True when the token value changed since last recorded fingerprint.

    Persists only a SHA-256 hash under LINKEDIN_DATA_DIR (safe to commit path;
    file is gitignored via *.csv pattern — fingerprint is not csv but lives in
    data dir). Used to log rotation on Scalingo after env-set + redeploy.
    """
    path = _fingerprint_path()
    current = token_fingerprint(access_token)
    previous = path.read_text().strip() if path.exists() else ""
    if previous == current:
        return False
    path.write_text(current + "\n", encoding="utf-8")
    if previous:
        logger.info(
            "linkedin_access_token_rotated fingerprint changed (new token in env/keyring)"
        )
    return bool(previous)
