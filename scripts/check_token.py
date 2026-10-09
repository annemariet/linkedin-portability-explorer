#!/usr/bin/env python3
"""Thin wrapper around linkedin-check-token for local `uv run python scripts/...`."""

from linkedin_api.token_check_cli import main

if __name__ == "__main__":
    main()
