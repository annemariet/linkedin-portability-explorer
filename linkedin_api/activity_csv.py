"""
Activity record model and CSV serialization.

The master CSV at ``get_data_dir() / "activities.csv"`` is the canonical
append-only local cache of all Portability API data.  It is shared across
pipelines (Neo4j graph builder, MVP summarizer, etc.).

CSV columns
-----------
owner            API user's URN
activity_type    post | comment | repost | instant_repost | reaction_to_post
                 | reaction_to_comment
time             Epoch milliseconds
reaction_type    LIKE | PRAISE | ... (empty for non-reactions)
author_urn       Person who performed the action
activity_urn     Post / Comment URN
post_id          Original post ID (extract_urn_id of target post; empty if unknown)
post_url         LinkedIn URL
content          Post / comment text (from API)
parent_urn       Parent post / comment URN (for comments, reposts)
original_post_urn  Original post URN (for reposts)
activity_id      Unique key per line: hash(post_id, activity_type, time, activity_urn)
created_at       ISO timestamp
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Sequence


# -- ActivityType enum -----------------------------------------------------


class ActivityType(str, Enum):
    """Recognised activity types in the CSV."""

    POST = "post"
    COMMENT = "comment"
    REPOST = "repost"
    INSTANT_REPOST = "instant_repost"
    REACTION_TO_POST = "reaction_to_post"
    REACTION_TO_COMMENT = "reaction_to_comment"


# -- ActivityRecord dataclass ----------------------------------------------

CSV_COLUMNS = [
    "owner",
    "activity_type",
    "time",
    "reaction_type",
    "author_urn",
    "activity_urn",
    "post_id",
    "post_url",
    "content",
    "parent_urn",
    "original_post_urn",
    "activity_id",
    "created_at",
]


def make_activity_id(
    post_id: str,
    activity_type: str,
    time: str,
    activity_urn: str,
) -> str:
    """Generate a unique activity_id from post_id, type, time, and activity_urn."""
    payload = f"{post_id}|{activity_type}|{time}|{activity_urn}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class ActivityRecord:
    """One row in the master activities CSV."""

    owner: str = ""
    activity_type: str = ""
    time: str = ""  # epoch ms as string (CSV-safe)
    reaction_type: str = ""
    author_urn: str = ""
    activity_urn: str = ""
    post_id: str = ""
    post_url: str = ""
    content: str = ""
    parent_urn: str = ""
    original_post_urn: str = ""
    activity_id: str = ""
    created_at: str = ""

    def to_row(self) -> dict[str, str]:
        """Return an ordered dict suitable for ``csv.DictWriter``."""
        d = asdict(self)
        return {col: str(d.get(col, "") or "") for col in CSV_COLUMNS}

    @classmethod
    def from_row(cls, row: dict[str, str]) -> "ActivityRecord":
        """Create an ActivityRecord from a CSV row dict."""
        valid_fields = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in row.items() if k in valid_fields}
        return cls(**filtered)


# -- Canonical data directory ----------------------------------------------


def get_data_dir() -> Path:
    """Return the canonical data directory for LinkedIn API data.

    Uses ``LINKEDIN_DATA_DIR`` env var if set, otherwise
    ``~/.linkedin_api/data/``.  This ensures a single shared location
    across worktrees.
    """
    env = os.getenv("LINKEDIN_DATA_DIR")
    if env:
        data_dir = Path(env)
    else:
        data_dir = Path.home() / ".linkedin_api" / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir


def get_default_csv_path() -> Path:
    """Return the default path for the master activities CSV."""
    return get_data_dir() / "activities.csv"


# -- CSV I/O ---------------------------------------------------------------


def _write_header(path: Path) -> None:
    """Write the CSV header row if the file does not exist or is empty."""
    if path.exists() and path.stat().st_size > 0:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()


def _row_identity(row: dict[str, str]) -> str:
    """Return a stable dedup key for one CSV row."""
    activity_id = row.get("activity_id", "")
    if activity_id:
        return f"id:{activity_id}"
    return (
        "fallback:"
        f"{row.get('activity_urn', '')}|{row.get('activity_type', '')}|"
        f"{row.get('author_urn', '')}|{row.get('reaction_type', '')}|{row.get('time', '')}"
    )


def _record_identity(rec: ActivityRecord) -> str:
    """Return a stable dedup key for one in-memory record."""
    if rec.activity_id:
        return f"id:{rec.activity_id}"
    return (
        "fallback:"
        f"{rec.activity_urn}|{rec.activity_type}|{rec.author_urn}|"
        f"{rec.reaction_type}|{rec.time}"
    )


def _load_existing_keys(path: Path) -> set[str]:
    """Return the set of dedup keys already in *path*."""
    if not path.exists() or path.stat().st_size == 0:
        return set()
    keys: set[str] = set()
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = _row_identity(row)
            if key:
                keys.add(key)
    return keys


def append_records_csv(
    records: Sequence[ActivityRecord],
    path: Path | None = None,
) -> int:
    """Append *records* to the CSV at *path*, deduplicating by stable identity.

    Returns the number of new records actually written.
    """
    if path is None:
        path = get_default_csv_path()

    _write_header(path)
    seen_keys = _load_existing_keys(path)
    new_records: list[ActivityRecord] = []
    for rec in records:
        if not rec.activity_urn:
            continue
        key = _record_identity(rec)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        new_records.append(rec)
    if not new_records:
        return 0

    with open(path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        for rec in new_records:
            writer.writerow(rec.to_row())

    return len(new_records)


def load_records_csv(path: Path | None = None) -> list[ActivityRecord]:
    """Read all records from the CSV at *path*.

    Returns an empty list when the file does not exist.
    """
    if path is None:
        path = get_default_csv_path()

    if not path.exists() or path.stat().st_size == 0:
        return []

    records: list[ActivityRecord] = []
    with open(path, "r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(ActivityRecord.from_row(row))
    return records


def records_to_csv_string(records: Sequence[ActivityRecord]) -> str:
    """Serialize *records* to a CSV string (useful for tests)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    for rec in records:
        writer.writerow(rec.to_row())
    return buf.getvalue()


# -- Filtering helpers -----------------------------------------------------


def filter_by_date(
    records: list[ActivityRecord],
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[ActivityRecord]:
    """Filter records by date range (inclusive). Prefer ``time`` (epoch ms).

    ``created_at`` is a fallback for legacy rows. Preferring ``time`` avoids
    dropping recent activity when ``created_at`` was written as naive local time
    and then interpreted as UTC.
    """

    def _as_utc(dt: datetime) -> datetime:
        # Old rows may be timezone-naive. Treat naive values as UTC.
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def _record_dt(rec: ActivityRecord) -> datetime | None:
        raw_time = (rec.time or "").strip()
        if raw_time:
            try:
                return datetime.fromtimestamp(int(raw_time) / 1000, tz=UTC)
            except (TypeError, ValueError, OSError, OverflowError):
                pass
        if not rec.created_at:
            return None
        try:
            return _as_utc(datetime.fromisoformat(rec.created_at))
        except (ValueError, TypeError):
            return None

    start_cmp = _as_utc(start) if start else None
    end_cmp = _as_utc(end) if end else None
    result: list[ActivityRecord] = []
    for rec in records:
        dt = _record_dt(rec)
        if dt is None:
            continue
        if start_cmp and dt < start_cmp:
            continue
        if end_cmp and dt > end_cmp:
            continue
        result.append(rec)
    return result


def filter_by_type(
    records: list[ActivityRecord],
    activity_type: ActivityType | str,
) -> list[ActivityRecord]:
    """Filter records by activity type."""
    type_str = (
        activity_type.value
        if isinstance(activity_type, ActivityType)
        else str(activity_type)
    )
    return [r for r in records if r.activity_type == type_str]
