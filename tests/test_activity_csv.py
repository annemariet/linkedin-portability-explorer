"""Tests for activity_csv module -- CSV round-trip serialization."""

from datetime import datetime
from pathlib import Path

import pytest

from linkedin_api.activity_csv import (
    ActivityRecord,
    ActivityType,
    CSV_COLUMNS,
    append_records_csv,
    filter_by_date,
    filter_by_type,
    get_data_dir,
    load_records_csv,
    make_activity_id,
    records_to_csv_string,
)


# -- Fixtures --------------------------------------------------------------


@pytest.fixture
def sample_records():
    r1_urn = "urn:li:share:111"
    r2_urn = "urn:li:share:222"
    r3_urn = "urn:li:comment:(ugcPost:333,444)"
    return [
        ActivityRecord(
            owner="urn:li:person:owner1",
            activity_type=ActivityType.POST.value,
            time="1700000000000",
            reaction_type="",
            author_urn="urn:li:person:author1",
            activity_urn=r1_urn,
            post_id="111",
            post_url="https://linkedin.com/posts/111",
            content="Hello world",
            parent_urn="",
            original_post_urn="",
            activity_id=make_activity_id("111", "post", "1700000000000", r1_urn),
            created_at="2023-11-14T22:13:20",
        ),
        ActivityRecord(
            owner="urn:li:person:owner1",
            activity_type=ActivityType.REACTION_TO_POST.value,
            time="1700000060000",
            reaction_type="LIKE",
            author_urn="urn:li:person:owner1",
            activity_urn=r2_urn,
            post_id="222",
            post_url="https://linkedin.com/posts/222",
            content="",
            parent_urn="",
            original_post_urn="",
            activity_id=make_activity_id(
                "222", "reaction_to_post", "1700000060000", r2_urn
            ),
            created_at="2023-11-14T22:14:20",
        ),
        ActivityRecord(
            owner="urn:li:person:owner1",
            activity_type=ActivityType.COMMENT.value,
            time="1700000120000",
            reaction_type="",
            author_urn="urn:li:person:owner1",
            activity_urn=r3_urn,
            post_id="333",
            post_url="https://linkedin.com/posts/333",
            content="Great post!",
            parent_urn="urn:li:ugcPost:333",
            original_post_urn="",
            activity_id=make_activity_id("333", "comment", "1700000120000", r3_urn),
            created_at="2023-11-14T22:15:20",
        ),
    ]


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "test_activities.csv"


# -- ActivityRecord --------------------------------------------------------


class TestActivityRecord:
    def test_to_row_returns_all_columns(self, sample_records):
        row = sample_records[0].to_row()
        assert set(row.keys()) == set(CSV_COLUMNS)

    def test_to_row_values(self, sample_records):
        row = sample_records[0].to_row()
        assert row["owner"] == "urn:li:person:owner1"
        assert row["activity_type"] == "post"
        assert row["content"] == "Hello world"
        assert row["reaction_type"] == ""

    def test_from_row_roundtrip(self, sample_records):
        original = sample_records[0]
        row = original.to_row()
        restored = ActivityRecord.from_row(row)
        assert restored == original

    def test_from_row_ignores_extra_columns(self):
        row = {"owner": "urn:li:person:x", "extra_field": "ignored"}
        rec = ActivityRecord.from_row(row)
        assert rec.owner == "urn:li:person:x"
        assert not hasattr(rec, "extra_field")

    def test_default_empty_strings(self):
        rec = ActivityRecord()
        row = rec.to_row()
        for col in CSV_COLUMNS:
            assert row[col] == ""

    def test_none_becomes_empty_string(self):
        rec = ActivityRecord(content=None)
        row = rec.to_row()
        assert row["content"] == ""


# -- CSV I/O ---------------------------------------------------------------


class TestAppendAndLoad:
    def test_append_creates_file(self, csv_path, sample_records):
        written = append_records_csv(sample_records, csv_path)
        assert written == 3
        assert csv_path.exists()

    def test_load_roundtrip(self, csv_path, sample_records):
        append_records_csv(sample_records, csv_path)
        loaded = load_records_csv(csv_path)
        assert len(loaded) == 3
        assert loaded[0].owner == "urn:li:person:owner1"
        assert loaded[0].activity_type == "post"
        assert loaded[1].reaction_type == "LIKE"
        assert loaded[2].content == "Great post!"

    def test_dedup_by_activity_id_across_runs(self, csv_path, sample_records):
        append_records_csv(sample_records, csv_path)
        written = append_records_csv(sample_records, csv_path)
        assert written == 0
        loaded = load_records_csv(csv_path)
        assert len(loaded) == 3

    def test_dedup_within_single_append_batch(self, csv_path, sample_records):
        records = [sample_records[0], sample_records[0], sample_records[1]]
        written = append_records_csv(records, csv_path)
        assert written == 2
        loaded = load_records_csv(csv_path)
        assert len(loaded) == 2

    def test_append_new_records_only(self, csv_path, sample_records):
        append_records_csv(sample_records[:2], csv_path)
        written = append_records_csv(sample_records, csv_path)
        assert written == 1  # only the third record is new
        loaded = load_records_csv(csv_path)
        assert len(loaded) == 3

    def test_load_nonexistent_file(self, tmp_path):
        loaded = load_records_csv(tmp_path / "does_not_exist.csv")
        assert loaded == []

    def test_load_empty_file(self, csv_path):
        csv_path.touch()
        loaded = load_records_csv(csv_path)
        assert loaded == []

    def test_records_without_activity_urn_skipped(self, csv_path):
        rec = ActivityRecord(owner="urn:li:person:x", activity_urn="")
        written = append_records_csv([rec], csv_path)
        assert written == 0

    def test_content_with_newlines(self, csv_path):
        rec = ActivityRecord(
            activity_urn="urn:li:share:999",
            content="Line 1\nLine 2\nLine 3",
        )
        append_records_csv([rec], csv_path)
        loaded = load_records_csv(csv_path)
        assert len(loaded) == 1
        assert loaded[0].content == "Line 1\nLine 2\nLine 3"

    def test_content_with_commas_and_quotes(self, csv_path):
        rec = ActivityRecord(
            activity_urn="urn:li:share:888",
            content='He said "hello, world" to everyone',
        )
        append_records_csv([rec], csv_path)
        loaded = load_records_csv(csv_path)
        assert loaded[0].content == 'He said "hello, world" to everyone'


class TestRecordsToCsvString:
    def test_string_contains_header(self, sample_records):
        csv_str = records_to_csv_string(sample_records)
        header_line = csv_str.split("\n")[0]
        for col in CSV_COLUMNS:
            assert col in header_line

    def test_string_has_correct_row_count(self, sample_records):
        csv_str = records_to_csv_string(sample_records)
        lines = [line for line in csv_str.strip().split("\n") if line]
        assert len(lines) == 4  # header + 3 data rows


# -- Filtering -------------------------------------------------------------


class TestFilterByDate:
    def test_filter_start(self, sample_records):
        start = datetime(2023, 11, 14, 22, 14, 0)
        result = filter_by_date(sample_records, start=start)
        assert len(result) == 2
        assert all(r.created_at >= start.isoformat() for r in result)

    def test_filter_end(self, sample_records):
        end = datetime(2023, 11, 14, 22, 14, 20)
        result = filter_by_date(sample_records, end=end)
        assert len(result) == 2

    def test_filter_both(self, sample_records):
        start = datetime(2023, 11, 14, 22, 14, 0)
        end = datetime(2023, 11, 14, 22, 14, 30)
        result = filter_by_date(sample_records, start=start, end=end)
        assert len(result) == 1
        assert result[0].activity_type == "reaction_to_post"

    def test_filter_no_bounds(self, sample_records):
        result = filter_by_date(sample_records)
        assert len(result) == 3

    def test_skips_empty_created_at(self):
        rec = ActivityRecord(activity_urn="urn:x", created_at="")
        result = filter_by_date([rec])
        assert len(result) == 0

    def test_prefers_epoch_time_over_naive_local_created_at(self):
        """Naive created_at ahead of UTC must not drop a row with correct time ms."""
        # 2026-07-24 14:41:56 UTC — created_at wrongly stored as local (+2h) naive
        rec = ActivityRecord(
            activity_urn="urn:li:activity:7486048147157262338",
            post_id="7486048147157262338",
            time="1784904116421",
            created_at="2026-07-24T16:41:56.421000",
        )
        end = datetime.fromisoformat("2026-07-24T15:00:00+00:00")
        start = datetime.fromisoformat("2026-07-23T15:00:00+00:00")
        result = filter_by_date([rec], start=start, end=end)
        assert [r.post_id for r in result] == ["7486048147157262338"]

    def test_mixed_naive_and_aware_datetimes(self):
        records = [
            ActivityRecord(activity_urn="urn:a", created_at="2023-11-14T22:13:20"),
            ActivityRecord(
                activity_urn="urn:b", created_at="2023-11-14T22:14:20+00:00"
            ),
        ]
        start = datetime.fromisoformat("2023-11-14T22:13:30+00:00")
        result = filter_by_date(records, start=start)
        assert [r.activity_urn for r in result] == ["urn:b"]


class TestFilterByType:
    def test_filter_by_enum(self, sample_records):
        result = filter_by_type(sample_records, ActivityType.POST)
        assert len(result) == 1
        assert result[0].activity_urn == "urn:li:share:111"

    def test_filter_by_string(self, sample_records):
        result = filter_by_type(sample_records, "comment")
        assert len(result) == 1
        assert result[0].activity_urn == "urn:li:comment:(ugcPost:333,444)"


# -- Data directory --------------------------------------------------------


class TestGetDataDir:
    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("LINKEDIN_DATA_DIR", raising=False)
        data_dir = get_data_dir()
        assert data_dir == Path.home() / ".linkedin_api" / "data"

    def test_env_override(self, monkeypatch, tmp_path):
        custom = tmp_path / "custom_data"
        monkeypatch.setenv("LINKEDIN_DATA_DIR", str(custom))
        data_dir = get_data_dir()
        assert data_dir == custom
        assert data_dir.exists()

    def test_creates_directory(self, monkeypatch, tmp_path):
        custom = tmp_path / "nested" / "dir"
        monkeypatch.setenv("LINKEDIN_DATA_DIR", str(custom))
        data_dir = get_data_dir()
        assert data_dir.exists()
