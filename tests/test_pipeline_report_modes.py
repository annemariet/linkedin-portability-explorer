"""Report mode routing: single pass vs per category."""

from unittest.mock import MagicMock, patch

from linkedin_api.pipeline_report import (
    REPORT_MODE_PER_CATEGORY,
    REPORT_MODE_SINGLE_PASS,
    ReportComplete,
    ReportProgress,
    _generate_report_events,
)


def _sample_metas(n: int = 3) -> list[dict]:
    return [
        {
            "urn": f"urn:li:activity:{i}",
            "post_url": f"https://linkedin.com/feed/update/{i}",
            "category": "tutorial",
            "summary": f"Summary {i}",
            "summarized_at": f"ts{i}",
        }
        for i in range(n)
    ]


class TestReportModeRouting:
    @patch("linkedin_api.pipeline_report.create_llm")
    def test_single_pass_one_llm_call(self, mock_create_llm):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="## Digest\n\n- One bullet")
        mock_create_llm.return_value = llm

        events = list(
            _generate_report_events(
                _sample_metas(),
                report_mode=REPORT_MODE_SINGLE_PASS,
                content_level="minimal",
                max_full_post_chars=1500,
                report_provider=None,
                report_model=None,
                period_dates="2025-01-01 to 2025-01-07",
                sig=None,
                should_cancel=None,
            )
        )

        assert llm.invoke.call_count == 1
        progress = [e for e in events if isinstance(e, ReportProgress)]
        assert len(progress) == 1
        assert "single pass" in progress[0].label.lower()
        complete = events[-1]
        assert isinstance(complete, ReportComplete)
        assert "## Digest" in complete.text

    @patch("linkedin_api.pipeline_report.create_llm")
    def test_per_category_invokes_per_batch(self, mock_create_llm):
        llm = MagicMock()
        llm.invoke.return_value = MagicMock(content="Batch summary.")
        mock_create_llm.return_value = llm

        events = list(
            _generate_report_events(
                _sample_metas(),
                report_mode=REPORT_MODE_PER_CATEGORY,
                content_level="minimal",
                max_full_post_chars=1500,
                report_provider=None,
                report_model=None,
                period_dates="2025-01-01 to 2025-01-07",
                sig=None,
                should_cancel=None,
            )
        )

        assert llm.invoke.call_count == 1
        labels = [e.label for e in events if isinstance(e, ReportProgress)]
        assert any("Tutorials" in label for label in labels)
        assert not any("single pass" in label.lower() for label in labels)
        assert isinstance(events[-1], ReportComplete)
