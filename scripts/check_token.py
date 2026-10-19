#!/usr/bin/env python3
"""Thin wrapper around linkedin-check-token for local `uv run python scripts/...`."""

from linkedin_api.utils.token_check import main

if __name__ == "__main__":
    main()
