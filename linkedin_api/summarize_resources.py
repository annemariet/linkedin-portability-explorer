#!/usr/bin/env python3
"""Summarize fetched linked articles (resource store) with LLM."""

from __future__ import annotations

import json
import warnings
from datetime import UTC, datetime

from linkedin_api.fetch_linked_content import (
    FetchResult,
    _fetch_result_from_resource_data,
    _resource_dir,
    is_exportable_resource,
    save_resource,
)
from linkedin_api.llm_config import create_llm, get_summary_model_id
from linkedin_api.summary_text import (
    ARTICLE_SYSTEM_PROMPT,
    build_article_user_prompt,
    parse_summary_response,
    truncate,
)

_MIN_ARTICLE_CHARS = 200
_MAX_ARTICLE_CHARS = 12000


def _resource_summary_complete(result: FetchResult) -> bool:
    """True when both TLDR and summary bullets exist."""
    if not (result.tldr or "").strip():
        return False
    return bool(result.summary_bullets) and any(
        str(b).strip() for b in result.summary_bullets
    )


def list_resources_for_summary(
    *,
    limit: int | None = None,
    force: bool = False,
    urns: set[str] | None = None,
) -> list[FetchResult]:
    scope = set(urns) if urns is not None else None
    out: list[FetchResult] = []
    for json_path in sorted(_resource_dir().glob("*.json")):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if scope is not None:
            cited = set(str(x) for x in (data.get("cited_by") or []) if str(x).strip())
            if not cited.intersection(scope):
                continue
        result = _fetch_result_from_resource_data(data)
        if result is None or not is_exportable_resource(result):
            continue
        body = (result.content or "").strip()
        if len(body) < _MIN_ARTICLE_CHARS:
            continue
        if not force and _resource_summary_complete(result):
            continue
        out.append(result)
        if limit and len(out) >= limit:
            break
    return out


def _summarize_resource(result: FetchResult, llm, *, model_id: str) -> bool:
    url = (result.resolved_url or result.url or "").strip()
    if not url:
        return False
    title = (result.title or url).strip()
    user_prompt = build_article_user_prompt(
        title=title,
        url=url,
        content=truncate((result.content or "").strip(), _MAX_ARTICLE_CHARS),
    )
    try:
        response = llm.invoke(user_prompt, system_instruction=ARTICLE_SYSTEM_PROMPT)
        raw = response.content if hasattr(response, "content") else str(response)
        parsed = parse_summary_response(raw)
        if not parsed.ok:
            return False
        updated = FetchResult(
            url=result.url,
            resolved_url=result.resolved_url,
            title=result.title,
            content=result.content,
            url_type=result.url_type,
            domain=result.domain,
            error=result.error,
            fetched_at=result.fetched_at,
            tldr=parsed.tldr,
            summary_author=parsed.author,
            summary_bullets=parsed.bullets,
            summary_model=model_id,
            summarized_at=datetime.now(UTC).isoformat(),
        )
        save_resource(url, updated)
        return True
    except Exception as exc:
        print(f"  LLM error ({url}): {exc}")
        return False


def summarize_resources(
    *,
    limit: int | None = None,
    quiet: bool = False,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    force_resummarize: bool = False,
    urns: set[str] | None = None,
) -> int:
    resources = list_resources_for_summary(
        limit=limit, force=force_resummarize, urns=urns
    )
    if not resources:
        if not quiet:
            print("No linked articles needing summary.")
        return 0
    from tqdm import tqdm

    llm = create_llm(
        quiet=quiet,
        stage="summary",
        provider_override=llm_provider,
        model_override=llm_model,
        json_mode=False,
    )
    model_id = get_summary_model_id(llm_provider, llm_model)
    total = 0
    it = tqdm(resources, desc="Summarize articles", unit="article", disable=quiet)
    for result in it:
        if _summarize_resource(result, llm, model_id=model_id):
            total += 1
        it.set_postfix(done=total)
    if total == 0 and not quiet:
        warnings.warn(
            "No articles were summarized (LLM errors?). Check LLM_MODEL and API key."
        )
    return total
