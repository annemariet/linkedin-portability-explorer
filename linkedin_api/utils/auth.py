from typing import Optional

import requests

from linkedin_api.utils.secret_retrieval import retrieve_secret


def get_secret(
    token_var: str,
    account_var: str = "LINKEDIN_ACCOUNT",
) -> Optional[str]:
    """
    Retrieve a secret: non-empty environment variable wins, then keyring.

    On Scalingo, empty or unusable env values are logged at ERROR when the
    variable name is present in the environment (see secret_retrieval).
    """
    return retrieve_secret(token_var, account_var).value


def get_access_token(
    token_var: str = "LINKEDIN_ACCESS_TOKEN",
    account_var: str = "LINKEDIN_ACCOUNT",
) -> Optional[str]:
    """Retrieve the LinkedIn access token (env-first, then keyring)."""
    return get_secret(token_var, account_var)


def build_linkedin_session(access_token: str, version: str = "202312"):
    """Return a requests.Session preloaded with LinkedIn API headers."""

    if not access_token:
        raise ValueError("Missing LinkedIn access token")

    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": version,
        }
    )
    return session
