"""Tests for WebSocket keepalive helpers and report markdown normalization."""

import time

from linkedin_api.gradio_app import (
    _normalize_report_markdown,
    _stream_with_keepalive,
)


def test_normalize_report_markdown_strips_fences():
    raw = "```markdown\n## Title\n\n- item\n```"
    assert _normalize_report_markdown(raw) == "## Title\n\n- item"


def test_normalize_report_markdown_empty_placeholder():
    assert "empty" in _normalize_report_markdown("").lower()


def test_normalize_report_markdown_converts_angle_bracket_urls():
    raw = "See <https://example.com/path> for details."
    assert (
        _normalize_report_markdown(raw)
        == "See [https://example.com/path](https://example.com/path) for details."
    )


def test_normalize_report_markdown_rejects_html_error_page():
    raw = "<!DOCTYPE html><html><body>Error</body></html>"
    assert "error page" in _normalize_report_markdown(raw).lower()


def test_stream_with_keepalive_emits_during_stall():
    def fast():
        yield "only"

    def keepalive():
        return "ping"

    out = list(_stream_with_keepalive(fast(), keepalive, interval=0.05))
    assert out == ["only"]

    def very_slow():
        time.sleep(0.08)
        yield "done"

    out = list(_stream_with_keepalive(very_slow(), keepalive, interval=0.02))
    assert out[-1] == "done"
    assert "ping" in out
