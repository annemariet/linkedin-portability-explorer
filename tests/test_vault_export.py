from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch

from kg_vault.writer import VaultWriteError, VaultWriter, VaultWriteResult

from linkedin_api.enriched_record import EnrichedRecord
from linkedin_api.fetch_linked_content import FetchResult
from linkedin_api.vault_catalog import (
    activity_source_id,
    article_source_id,
    build_activity_catalog_markdown,
    build_article_catalog_markdown,
    parse_source_id_from_markdown,
)
from linkedin_api.vault_export import export_activities_to_vault, export_period_to_vault


class _RecordingBackend:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.fail_source_ids: set[str] = set()

    def exists(self, rel_path: str) -> bool:
        return rel_path in self.files

    def read_text(self, rel_path: str) -> str | None:
        return self.files.get(rel_path)

    def write_text(
        self, rel_path: str, content: str, *, message: str
    ) -> VaultWriteResult:
        source_id = parse_source_id_from_markdown(content) or ""
        if source_id in self.fail_source_ids:
            raise VaultWriteError(f"simulated failure for {source_id}")
        created = rel_path not in self.files
        self.files[rel_path] = content
        return VaultWriteResult(rel_path=rel_path, created=created)


def _activity(activity_id: str = "7123456789") -> EnrichedRecord:
    return EnrichedRecord(
        post_urn="urn:li:activity:7398404729531285504",
        post_url="https://www.linkedin.com/feed/update/urn:li:activity:7398404729531285504",
        content="Post body about **AI** tooling.",
        urls=["https://example.com/article"],
        interaction_type="reaction",
        reaction_type="LIKE",
        comment_text="",
        post_id="7398404729531285504",
        activity_id=activity_id,
        timestamp=1_718_784_000_000,
        created_at="2024-06-19T00:00:00+00:00",
    )


class VaultCatalogLinkedInTests(unittest.TestCase):
    def test_activity_source_id(self) -> None:
        self.assertEqual(
            activity_source_id("7123456789"),
            "platform:linkedin:activity:7123456789",
        )

    def test_article_source_id_normalizes_url(self) -> None:
        with patch(
            "linkedin_api.vault_catalog.resolve_redirect",
            side_effect=lambda u: u,
        ):
            self.assertEqual(
                article_source_id("https://Example.com/x/?utm_source=y", resolve=False),
                "https://example.com/x",
            )

    def test_build_activity_markdown_has_required_frontmatter(self) -> None:
        rec = _activity()
        build = build_activity_catalog_markdown(
            rec,
            content=rec.content,
            meta={
                "summary": "A concise summary of the post.",
                "post_author": "Ada Lovelace",
                "topics": ["ai"],
            },
        )
        self.assertIn("producer: linkedin-api", build.markdown)
        self.assertIn("## Summary", build.markdown)
        self.assertIn("## Source", build.markdown)
        self.assertEqual(
            parse_source_id_from_markdown(build.markdown),
            "platform:linkedin:activity:7123456789",
        )

    def test_build_article_markdown_uses_canonical_url(self) -> None:
        result = FetchResult(
            url="https://bit.ly/abc",
            resolved_url="https://example.com/deep-dive",
            title="Deep Dive",
            content="Article body.",
            fetched_at="2026-06-19T12:00:00+00:00",
        )
        with patch(
            "linkedin_api.vault_catalog.resolve_redirect",
            side_effect=lambda u: u,
        ):
            build = build_article_catalog_markdown(
                result,
                activity_source_ids=["platform:linkedin:activity:1"],
            )
        assert build is not None
        self.assertEqual(
            parse_source_id_from_markdown(build.markdown),
            "https://example.com/deep-dive",
        )
        self.assertIn("related_source_ids:", build.markdown)


class VaultExportTests(unittest.TestCase):
    def test_writes_one_file_per_activity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = _RecordingBackend()
            writer = VaultWriter(backend=backend)
            report = export_activities_to_vault(
                [_activity("1"), _activity("2")],
                output_root=tmp,
                writer=writer,
            )
            self.assertEqual(report.attempted, 2)
            self.assertEqual(report.written, 2)
            self.assertEqual(len(backend.files), 2)

    def test_skips_duplicate_source_id_on_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = _RecordingBackend()
            writer = VaultWriter(backend=backend)
            activities = [_activity("99")]
            first = export_activities_to_vault(
                activities, output_root=tmp, writer=writer
            )
            second = export_activities_to_vault(
                activities, output_root=tmp, writer=writer
            )
            self.assertEqual(first.written, 1)
            self.assertEqual(second.skipped_existing, 1)
            self.assertEqual(second.written, 0)

    def test_failure_on_one_item_does_not_abort_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = _RecordingBackend()
            backend.fail_source_ids.add(activity_source_id("fail"))
            writer = VaultWriter(backend=backend)
            report = export_activities_to_vault(
                [_activity("ok"), _activity("fail")],
                output_root=tmp,
                writer=writer,
            )
            self.assertEqual(report.attempted, 2)
            self.assertEqual(report.written, 1)
            self.assertEqual(report.failed, 1)

    @patch("linkedin_api.vault_export.load_resource")
    @patch("linkedin_api.vault_export.load_metadata")
    @patch("linkedin_api.vault_export.load_content")
    def test_export_period_includes_linked_articles(
        self,
        mock_content,
        mock_meta,
        mock_resource,
    ) -> None:
        mock_content.return_value = "Post body"
        mock_meta.return_value = {
            "summary": "Summary.",
            "urls": ["https://example.com/article"],
        }
        mock_resource.return_value = FetchResult(
            url="https://example.com/article",
            resolved_url="https://example.com/article",
            title="Article",
            content="Article text.",
        )
        with tempfile.TemporaryDirectory() as tmp:
            backend = _RecordingBackend()
            writer = VaultWriter(backend=backend)
            report = export_period_to_vault(
                [_activity("42")],
                output_root=tmp,
                writer=writer,
            )
            self.assertEqual(report.attempted, 2)
            self.assertEqual(report.written, 2)


if __name__ == "__main__":
    unittest.main()
