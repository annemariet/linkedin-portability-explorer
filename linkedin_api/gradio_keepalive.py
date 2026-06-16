"""WebSocket keepalive helpers for long-running Gradio generators."""

from __future__ import annotations

import queue
import re
import threading
from collections.abc import Callable, Iterator
from typing import TypeVar

_T = TypeVar("_T")

KEEPALIVE_TICK = object()
WS_KEEPALIVE_SECONDS = 20.0

_ANGLE_BRACKET_URL_RE = re.compile(r"<(https?://[^>\s]+)>")


def _stream_with_keepalive(
    iterator: Iterator[_T],
    keepalive: Callable[[], _T],
    *,
    interval: float = WS_KEEPALIVE_SECONDS,
    should_stop: Callable[[], bool] | None = None,
) -> Iterator[_T]:
    """Yield from *iterator*; emit *keepalive()* if no item arrives within *interval*."""
    q: queue.Queue = queue.Queue()
    sentinel = object()

    def feed() -> None:
        try:
            for item in iterator:
                if should_stop and should_stop():
                    break
                q.put(item)
        except Exception as e:
            q.put(e)
        finally:
            q.put(sentinel)

    threading.Thread(target=feed, daemon=True).start()
    while True:
        if should_stop and should_stop():
            break
        try:
            item = q.get(timeout=interval)
        except queue.Empty:
            if should_stop and should_stop():
                break
            yield keepalive()
            continue
        if item is sentinel:
            break
        if isinstance(item, Exception):
            raise item
        yield item


def normalize_report_markdown(report: str) -> str:
    """Prepare LLM report text for Gradio Markdown."""
    text = (report or "").strip()
    if not text:
        return "_Report was empty. Try again or check Scalingo logs._"
    fence = re.match(r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    if not text:
        return "_Report was empty. Try again or check Scalingo logs._"
    lower = text.lower()
    if lower.startswith("<!doctype") or lower.startswith("<html"):
        return (
            "_Report looked like an HTML error page, not markdown. "
            "Check Scalingo logs or try again._"
        )
    return _ANGLE_BRACKET_URL_RE.sub(r"[\1](\1)", text)
