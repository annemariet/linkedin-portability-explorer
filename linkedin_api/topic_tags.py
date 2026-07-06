"""Translate LLM topics to English Obsidian catalog tags."""

from __future__ import annotations

import logging
import re

from kg_vault.catalog import normalize_obsidian_tags

from linkedin_api.llm_config import LLMClient

logger = logging.getLogger(__name__)

TOPIC_TRANSLATE_SYSTEM_PROMPT = (
    "You convert theme labels into English catalog tags for a personal knowledge vault.\n"
    "Input: comma-separated themes in any language.\n"
    "Output: exactly one line — comma-separated English tags (1-5 items).\n"
    "Each tag: 1-3 common English words, lowercase, no # symbol.\n"
    "Preserve meaning; do not invent themes absent from the input."
)

_TOPIC_PREFIX_RE = re.compile(r"^TOPICS:\s*", re.IGNORECASE)
_translation_cache: dict[tuple[str, ...], tuple[str, ...]] = {}


def _parse_translate_line(raw: str) -> list[str]:
    for line in (raw or "").splitlines():
        text = line.strip()
        if not text:
            continue
        text = _TOPIC_PREFIX_RE.sub("", text).strip()
        parts = [p.strip().lstrip("#") for p in re.split(r"[,;]", text) if p.strip()]
        if parts:
            return parts[:8]
    return []


def translate_topics_to_english(topics: list[str], llm: LLMClient) -> list[str]:
    """One LLM call: themes in any language → English tag labels."""
    topics_key = tuple(str(t).strip() for t in topics if str(t).strip())
    if not topics_key:
        return []
    cached = _translation_cache.get(topics_key)
    if cached is not None:
        return list(cached)

    user_prompt = "Themes:\n" + ", ".join(topics_key)
    response = llm.invoke(user_prompt, system_instruction=TOPIC_TRANSLATE_SYSTEM_PROMPT)
    content = response.content if hasattr(response, "content") else str(response)
    english = _parse_translate_line(content)
    if not english:
        logger.warning(
            "topic_tag_translate_empty input=%r output=%r",
            topics_key,
            content[:200],
        )
        english = list(topics_key)
    _translation_cache[topics_key] = tuple(english)
    return english


def _resolve_summary_llm(*, quiet: bool = True) -> LLMClient | None:
    try:
        from linkedin_api.llm_config import create_llm

        return create_llm(quiet=quiet, stage="summary", json_mode=False)
    except Exception as exc:
        logger.debug("topic_tags: no summary LLM (%s)", exc)
        return None


def topics_to_catalog_tags(
    topics: list[str],
    *,
    llm: LLMClient | None = None,
    limit: int = 5,
    quiet: bool = True,
) -> list[str]:
    """Translate themes to English, then slugify for Obsidian ``tags:`` frontmatter."""
    cleaned = [str(t).strip() for t in topics if str(t).strip()]
    if not cleaned:
        return []
    client = llm or _resolve_summary_llm(quiet=quiet)
    english = cleaned
    if client is not None:
        try:
            english = translate_topics_to_english(cleaned, client)
        except Exception as exc:
            logger.warning("topic_tag_translate_failed: %s", exc)
    return normalize_obsidian_tags(english, limit=limit)


def catalog_tags_for_topics(
    topics: list[str],
    *,
    catalog_tags: list[str] | None = None,
    llm: LLMClient | None = None,
    limit: int = 5,
    quiet: bool = True,
) -> list[str]:
    """Prefer precomputed ``catalog_tags``; otherwise translate *topics* to English slugs."""
    stored = [str(t).strip() for t in (catalog_tags or []) if str(t).strip()]
    if stored:
        normalized = normalize_obsidian_tags(stored, limit=limit)
        if normalized:
            return normalized
    return topics_to_catalog_tags(topics, llm=llm, limit=limit, quiet=quiet)
