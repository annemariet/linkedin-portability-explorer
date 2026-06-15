"""Tests for WebSocket keepalive helpers and report markdown normalization."""

import time

import pytest

from linkedin_api.gradio_app import (
    KEEPALIVE_TICK,
    PipelineCancelledError,
    _check_run_cancelled,
    _is_llm_timeout_error,
    _normalize_report_markdown,
    _report_error_message,
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


def test_is_llm_timeout_error_detects_524():
    assert _is_llm_timeout_error(Exception("Error code: 524 - timeout"))


def test_report_error_message_524():
    msg = _report_error_message(Exception("Error code: 524"))
    assert "524" in msg or "timed out" in msg.lower()
    assert "Skip fetch" in msg


def test_check_run_cancelled_raises():
    with pytest.raises(PipelineCancelledError):
        _check_run_cancelled(lambda: True)
    _check_run_cancelled(lambda: False)
    _check_run_cancelled(None)


def test_report_error_message_cancelled():
    assert "stopped" in _report_error_message(PipelineCancelledError()).lower()

    def fast():
        yield "only"

    def keepalive():
        return KEEPALIVE_TICK

    out = list(_stream_with_keepalive(fast(), keepalive, interval=0.05))
    assert out == ["only"]

    def very_slow():
        time.sleep(0.08)
        yield "done"

    out = list(_stream_with_keepalive(very_slow(), keepalive, interval=0.02))
    assert out[-1] == "done"
    assert KEEPALIVE_TICK in out
