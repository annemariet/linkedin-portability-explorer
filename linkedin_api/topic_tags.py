"""Translate LLM topics to English Obsidian catalog tags."""

from __future__ import annotations

import logging
import re
import unicodedata

from linkedin_api.llm_config import LLMClient

logger = logging.getLogger(__name__)

_NON_OBSIDIAN_TAG_CHARS = re.compile(r"[^a-z0-9/_-]+")
_TAG_COLLAPSE_RE = re.compile(r"-{2,}|/{2,}")


def _fold_accents(text: str) -> str:
    """Strip combining marks (é→e, ü→u) for ASCII-safe tag slugs."""
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in normalized if not unicodedata.combining(ch))


def _normalize_obsidian_tag(raw: str) -> str | None:
    """Return an Obsidian-valid tag slug, or None if nothing usable remains."""
    text = (raw or "").strip().lstrip("#")
    if not text:
        return None
    text = _fold_accents(text)
    text = text.lower().replace("'", "").replace("’", "")
    text = re.sub(r"[@.]+", "-", text)
    text = re.sub(r"[\s_()]+", "-", text)
    text = _NON_OBSIDIAN_TAG_CHARS.sub("", text)
    text = _TAG_COLLAPSE_RE.sub(lambda m: m.group(0)[0], text)
    text = text.strip("-/")
    if not text:
        return None
    body = text.replace("/", "").replace("-", "")
    if body.isdigit():
        text = f"tag-{text}"
    return text


def normalize_obsidian_tags(
    values: list[str] | None,
    *,
    limit: int | None = None,
) -> list[str]:
    """Normalize topic/tag strings for YAML ``tags:`` frontmatter (deduped, ordered)."""
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        text = str(value).strip()
        if not text:
            continue
        chunks = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
        for chunk in chunks:
            slug = _normalize_obsidian_tag(chunk)
            if not slug or slug in seen:
                continue
            seen.add(slug)
            ordered.append(slug)
            if limit is not None and len(ordered) >= limit:
                return ordered
    return ordered


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


def _looks_non_english(topics: list[str]) -> bool:
    """Cheap heuristic (no LLM call): any non-ASCII character suggests the
    extraction prompt's "TOPICS always in English" instruction wasn't
    followed. Some false positives (English loanwords like "café") are
    acceptable — they cost one extra translate call, not a wrong tag."""
    return any(not text.isascii() for text in topics)


def topics_to_catalog_tags(
    topics: list[str],
    *,
    llm: LLMClient | None = None,
    limit: int = 5,
    quiet: bool = True,
) -> list[str]:
    """Slugify extracted topics for Obsidian ``tags:`` frontmatter.

    The extraction prompt already asks for English topics, so the common
    case needs no LLM call here. Only when a topic still looks non-English
    do we spend a second call translating it — see
    ``translate_topics_to_english``.
    """
    cleaned = [str(t).strip() for t in topics if str(t).strip()]
    if not cleaned:
        return []
    english = cleaned
    if _looks_non_english(cleaned):
        client = llm or _resolve_summary_llm(quiet=quiet)
        if client is not None:
            logger.info("topic_tag_looks_non_english topics=%r", cleaned)
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
