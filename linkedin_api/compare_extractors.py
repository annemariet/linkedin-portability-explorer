"""
Side-by-side comparison of the ``httpx`` and ``tavily`` linked-content
extractors (see ``fetch_linked_content.py`` / ``LINKEDIN_EXTRACTOR``) on a
handful of URLs, so a human can judge which output reads better before
switching the pipeline default.

Bypasses the resource store entirely — this never reads or writes cached
fetch results, it just runs both strategies fresh and reports the outcome.

CLI
---
  uv run python -m linkedin_api.compare_extractors <url> [<url> ...]
  uv run python -m linkedin_api.compare_extractors <url> --out-dir /tmp/cmp
"""

from __future__ import annotations

import argparse
from pathlib import Path

from linkedin_api.activity_csv import get_data_dir
from linkedin_api.fetch_linked_content import (
    _BODY_BACKENDS,
    _METADATA_ONLY_URL_TYPES,
    FetchResult,
)
from linkedin_api.utils.urls import categorize_url, resolve_redirect


def _run_backend(name: str, url: str) -> FetchResult:
    """Run a single named backend strategy against *url*, never raising."""
    strategy = _BODY_BACKENDS[name]
    try:
        title, content, images = strategy(url)
        return FetchResult(
            url=url, resolved_url=url, title=title, content=content, images=images
        )
    except Exception as exc:
        return FetchResult(url=url, resolved_url=url, error=str(exc))


def compare_url(url: str, *, out_dir: Path | None = None) -> dict[str, FetchResult]:
    """Run every backend in ``_BODY_BACKENDS`` against *url* and return results
    keyed by backend name. Writes each backend's content to ``{out_dir}/{stem}-{backend}.md``
    when ``out_dir`` is given, for manual diffing."""
    resolved = resolve_redirect(url)
    info = categorize_url(resolved)
    url_type = info.get("type") or "article"

    results: dict[str, FetchResult] = {}
    if url_type in _METADATA_ONLY_URL_TYPES:
        note = f"skipped: '{url_type}' is metadata-only, same for every backend"
        for name in _BODY_BACKENDS:
            results[name] = FetchResult(url=url, resolved_url=resolved, error=note)
        return results

    for name in _BODY_BACKENDS:
        results[name] = _run_backend(name, resolved)

    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = "".join(c if c.isalnum() else "-" for c in resolved)[:80]
        for name, result in results.items():
            images_section = (
                "\n\n## Images\n" + "\n".join(f"- {u}" for u in result.images)
                if result.images
                else ""
            )
            (out_dir / f"{stem}-{name}.md").write_text(
                f"# {result.title}\n\n{result.content}{images_section}",
                encoding="utf-8",
            )

    return results


def _print_summary(url: str, results: dict[str, FetchResult]) -> None:
    print(f"\n{url}")
    for name, result in results.items():
        if result.error:
            print(f"  {name:8s} ERROR  {result.error}")
        else:
            print(
                f"  {name:8s} OK     {len(result.content):6d} chars  "
                f"{len(result.images)} image(s)  title={result.title[:60]!r}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compare httpx vs tavily extraction on the same URL(s).",
    )
    parser.add_argument("urls", nargs="+", help="URL(s) to compare.")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Write each backend's extracted content here as .md files "
        "(default: <data dir>/extractor_comparisons).",
    )
    args = parser.parse_args()

    out_dir = args.out_dir or (get_data_dir() / "extractor_comparisons")

    for url in args.urls:
        results = compare_url(url, out_dir=out_dir)
        _print_summary(url, results)

    print(f"\nFull output written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
