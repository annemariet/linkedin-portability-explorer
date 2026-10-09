"""Env-first secret retrieval with Scalingo-oriented failure logging."""

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


def _on_scalingo() -> bool:
    return bool(os.getenv("SCALINGO_APP"))


def _log_scalingo_retrieval_failure(result: SecretRetrievalResult) -> None:
    if not _on_scalingo():
        return
    if result.issue == SecretRetrievalIssue.OK:
        return
    if result.issue == SecretRetrievalIssue.ENV_SET_BUT_EMPTY:
        logger.error(
            "scalingo_secret_retrieval %s is set in the environment but empty; "
            "fix the Scalingo env var value",
            result.env_var,
        )
        return
    if result.issue == SecretRetrievalIssue.MISSING:
        if result.env_var in os.environ:
            logger.error(
                "scalingo_secret_retrieval %s is present in the environment but "
                "no usable secret was resolved (check for whitespace-only value)",
                result.env_var,
            )
        return
    if result.issue == SecretRetrievalIssue.KEYRING_ERROR:
        if result.env_var in os.environ:
            logger.error(
                "scalingo_secret_retrieval keyring lookup failed for %s while "
                "the env var is set; ensure the env value is non-empty (env wins "
                "over keyring when set)",
                result.env_var,
            )


def retrieve_secret(
    token_var: str = _TOKEN_VAR_DEFAULT,
    account_var: str = _ACCOUNT_VAR_DEFAULT,
    *,
    log_scalingo_alerts: bool = True,
) -> SecretRetrievalResult:
    """
    Resolve a secret: non-empty env value wins, then keyring.

    On Scalingo, logs at ERROR when the env var name is configured but retrieval
    still fails (empty value, whitespace-only, or keyring error with env present).
    """
    dotenv.load_dotenv()

    env_raw = os.environ.get(token_var)
    if env_raw is not None:
        env_value = env_raw.strip()
        if env_value:
            result = SecretRetrievalResult(
                value=env_value,
                source="env",
                issue=SecretRetrievalIssue.OK,
                env_var=token_var,
            )
            return result
        result = SecretRetrievalResult(
            value=None,
            source=None,
            issue=SecretRetrievalIssue.ENV_SET_BUT_EMPTY,
            env_var=token_var,
        )
        if log_scalingo_alerts:
            _log_scalingo_retrieval_failure(result)
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
            if token_var in os.environ and log_scalingo_alerts:
                _log_scalingo_retrieval_failure(
                    SecretRetrievalResult(
                        value=None,
                        source=None,
                        issue=SecretRetrievalIssue.KEYRING_ERROR,
                        env_var=token_var,
                    )
                )

    result = SecretRetrievalResult(
        value=None,
        source=None,
        issue=SecretRetrievalIssue.MISSING,
        env_var=token_var,
    )
    if log_scalingo_alerts:
        _log_scalingo_retrieval_failure(result)
    return result
