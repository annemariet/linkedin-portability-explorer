"""Period parsing for pipeline and collection."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_period(value: str) -> int | None:
    """Convert '7d', '14d', '30d', '1w', '1m' to start_time in epoch milliseconds."""
    if not value or len(value) < 2:
        return None
    try:
        n = int(value[:-1])
    except ValueError:
        return None
    unit = value[-1].lower()
    if unit == "d":
        delta = timedelta(days=n)
    elif unit == "w":
        delta = timedelta(weeks=n)
    elif unit == "m":
        delta = timedelta(days=n * 30)
    else:
        return None
    cutoff = datetime.now(timezone.utc) - delta
    return int(cutoff.timestamp() * 1000)
