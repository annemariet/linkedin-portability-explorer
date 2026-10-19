"""Env-first secret retrieval with structured failure logging."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import dotenv

try:
    import keyring
except ImportError:
    keyring = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_TOKEN_VAR_DEFAULT = "LINKEDIN_ACCESS_TOKEN"
_ACCOUNT_VAR_DEFAULT = "LINKEDIN_ACCOUNT"


class SecretRetrievalIssue(str, Enum):
    OK = "ok"
    MISSING = "missing"
    ENV_SET_BUT_EMPTY = "env_set_but_empty"
    KEYRING_ERROR = "keyring_error"


@dataclass(frozen=True)
class SecretRetrievalResult:
    value: Optional[str]
    source: Optional[str]
    issue: SecretRetrievalIssue
    env_var: str


def _log_retrieval_failure(result: SecretRetrievalResult) -> None:
    if result.issue == SecretRetrievalIssue.OK:
        return
    if result.issue == SecretRetrievalIssue.ENV_SET_BUT_EMPTY:
        logger.error(
            "secret_retrieval %s is set in the environment but empty",
            result.env_var,
        )
        return
    if result.issue == SecretRetrievalIssue.KEYRING_ERROR:
        logger.error(
            "secret_retrieval keyring lookup failed for %s",
            result.env_var,
        )
        return
    if result.issue == SecretRetrievalIssue.MISSING:
        logger.error(
            "secret_retrieval %s not found (env or keyring)",
            result.env_var,
        )


def retrieve_secret(
    token_var: str = _TOKEN_VAR_DEFAULT,
    account_var: str = _ACCOUNT_VAR_DEFAULT,
    *,
    log_failures: bool = True,
) -> SecretRetrievalResult:
    """
    Resolve a secret: non-empty env value wins, then keyring.

    Logs retrieval failures at ERROR under the ``secret_retrieval`` event name
    (never logs secret values).
    """
    dotenv.load_dotenv()

    env_raw = os.environ.get(token_var)
    if env_raw is not None:
        env_value = env_raw.strip()
        if env_value:
            return SecretRetrievalResult(
                value=env_value,
                source="env",
                issue=SecretRetrievalIssue.OK,
                env_var=token_var,
            )
        result = SecretRetrievalResult(
            value=None,
            source=None,
            issue=SecretRetrievalIssue.ENV_SET_BUT_EMPTY,
            env_var=token_var,
        )
        if log_failures:
            _log_retrieval_failure(result)
        return result

    account = os.getenv(account_var, "")
    if keyring is not None:
        try:
            secret = keyring.get_password(token_var, account)
            if secret and secret.strip():
                return SecretRetrievalResult(
                    value=secret.strip(),
                    source="keyring",
                    issue=SecretRetrievalIssue.OK,
                    env_var=token_var,
                )
        except Exception as exc:
            logger.warning(
                "keyring lookup failed for %s (account=%r): %s",
                token_var,
                account or None,
                exc,
            )
            result = SecretRetrievalResult(
                value=None,
                source=None,
                issue=SecretRetrievalIssue.KEYRING_ERROR,
                env_var=token_var,
            )
            if log_failures:
                _log_retrieval_failure(result)
            return result

    result = SecretRetrievalResult(
        value=None,
        source=None,
        issue=SecretRetrievalIssue.MISSING,
        env_var=token_var,
    )
    if log_failures:
        _log_retrieval_failure(result)
    return result
