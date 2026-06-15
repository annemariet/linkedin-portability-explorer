"""Unit tests for pipeline progress rendering in gradio_pipeline_ui."""

from linkedin_api.gradio_pipeline_ui import (
    PIPELINE_HINT_TEXT,
    _render_pipeline_status,
    _status_from_pipeline_line,
)
from linkedin_api.pipeline_report import (
    REPORT_MODE_CHOICES,
    REPORT_MODE_PER_CATEGORY,
    REPORT_MODE_SINGLE_PASS,
)


def test_render_pipeline_status_shows_hint_when_idle():
    html = _render_pipeline_status()
    assert PIPELINE_HINT_TEXT in html


def test_render_pipeline_status_shows_label_and_progress_width():
    html = _render_pipeline_status("enriching [3/10]…", (1, 0.256))
    assert "enriching [3/10]…" in html
    assert "width: 26%;" in html


def test_render_pipeline_status_escapes_label_html():
    html = _render_pipeline_status('<script>alert("x")</script>', (0, 0.5))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_status_from_pipeline_line_parses_enriching_fraction():
    stage_progress, label = _status_from_pipeline_line("Enriching 3/10…")
    assert stage_progress == (1, 0.3)
    assert label == "enriching [3/10]…"


def test_status_from_pipeline_line_parses_fetching_linked_urls():
    stage_progress, label = _status_from_pipeline_line("Fetching linked URLs 2/5…")
    assert stage_progress == (2, 0.4)
    assert label == "fetching linked URLs [2/5]…"


def test_status_from_pipeline_line_parses_summarizing_fraction():
    stage_progress, label = _status_from_pipeline_line("Summarizing batch 2/4…")
    assert stage_progress == (3, 0.5)
    assert label == "summarizing [2/4]…"


def test_status_from_pipeline_line_handles_failures():
    assert _status_from_pipeline_line("❌ Failed: boom") == ((4, 1.0), "Failed.")


def test_report_mode_choices_have_label_value_pairs():
    """Gradio dropdown passes value (2nd elem); each choice is (label, value)."""
    values = [v for _, v in REPORT_MODE_CHOICES]
    assert REPORT_MODE_PER_CATEGORY in values
    assert REPORT_MODE_SINGLE_PASS in values
