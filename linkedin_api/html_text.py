"""HTML → plain text with inline ``code`` preserved as Markdown backticks."""

from __future__ import annotations

import re
from typing import Any, Iterable

from bs4 import BeautifulSoup, NavigableString, Tag

from linkedin_api.utils.urls import fix_mojibake

_BLOCK_TAGS = frozenset(
    {
        "p",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "pre",
        "blockquote",
        "td",
        "th",
        "figcaption",
    }
)
_CHROME_TAGS = ("script", "style", "nav", "header", "footer", "aside")
_COLLAPSE_WS = re.compile(r"[ \t]+")


def _render_inline(element: Any) -> str:
    if isinstance(element, NavigableString):
        return str(element)
    if not isinstance(element, Tag):
        return ""
    name = element.name or ""
    if name in ("script", "style"):
        return ""
    if name == "code":
        return f"`{element.get_text()}`"
    if name == "br":
        return "\n"
    if name == "pre":
        return element.get_text()
    return "".join(_render_inline(child) for child in element.children)


def _normalize_block(text: str, *, preserve_newlines: bool = False) -> str:
    cleaned = fix_mojibake(text.strip())
    if not preserve_newlines:
        cleaned = cleaned.replace("\n", " ")
    cleaned = _COLLAPSE_WS.sub(" ", cleaned)
    return cleaned.replace(" \n", "\n").strip()


def _heading_prefix(tag_name: str, text: str) -> str:
    if tag_name == "h1":
        return f"# {text}"
    if tag_name == "h2":
        return f"## {text}"
    if tag_name == "h3":
        return f"### {text}"
    if tag_name in {"h4", "h5", "h6"}:
        return f"#### {text}"
    return text


def extract_html_body_text(soup: BeautifulSoup) -> str:
    """Extract readable body text; inline ``code`` → Markdown backticks."""
    for tag in soup(list(_CHROME_TAGS)):
        tag.decompose()
    body = soup.find("body") or soup
    blocks: list[str] = []
    seen: set[int] = set()
    for element in body.find_all(_BLOCK_TAGS):
        element_id = id(element)
        if element_id in seen:
            continue
        seen.add(element_id)
        inline = _render_inline(element)
        preserve_nl = element.name == "pre"
        rendered = _normalize_block(inline, preserve_newlines=preserve_nl)
        if not rendered:
            continue
        if element.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            rendered = _heading_prefix(element.name, rendered)
        elif element.name == "li":
            rendered = f"- {rendered}"
        blocks.append(rendered)
    if not blocks:
        fallback = _normalize_block(body.get_text(separator="\n"))
        return fallback
    return "\n\n".join(blocks)


def x_entity_map(entity_map: Any) -> dict[int, dict]:
    """Normalize fxTwitter Draft.js entityMap (list or dict) to int → entity."""
    out: dict[int, dict] = {}
    items: Iterable[tuple[Any, Any]]
    if isinstance(entity_map, dict):
        items = entity_map.items()
    elif isinstance(entity_map, list):
        items = (
            (item.get("key"), item.get("value"))
            for item in entity_map
            if isinstance(item, dict)
        )
    else:
        return out
    for raw_key, value in items:
        if value is None:
            continue
        try:
            out[int(raw_key)] = value
        except (TypeError, ValueError):
            continue
    return out


def _apply_inline_styles(text: str, ranges: list[dict]) -> str:
    if not ranges or not text:
        return text
    bold_spans = [
        (r.get("offset", 0), r.get("offset", 0) + r.get("length", 0))
        for r in ranges
        if r.get("style") == "Bold"
    ]
    if not bold_spans:
        return text
    parts: list[str] = []
    cursor = 0
    for start, end in sorted(bold_spans):
        start = max(0, min(start, len(text)))
        end = max(start, min(end, len(text)))
        if cursor < start:
            parts.append(text[cursor:start])
        if start < end:
            parts.append(f"**{text[start:end]}**")
        cursor = end
    if cursor < len(text):
        parts.append(text[cursor:])
    return "".join(parts)


def x_article_blocks_to_text(content: dict | None) -> str:
    """Flatten fxTwitter / vxTwitter X Article Draft.js blocks to plain text."""
    if not content:
        return ""
    blocks = content.get("blocks") or []
    entities = x_entity_map(content.get("entityMap"))
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type") or "unstyled"
        text = (block.get("text") or "").strip()
        if block_type == "atomic":
            for entity_range in block.get("entityRanges") or []:
                entity = entities.get(entity_range.get("key"))
                if not entity:
                    continue
                data = entity.get("data") or {}
                entity_type = entity.get("type") or ""
                if entity_type == "MARKDOWN" and data.get("markdown"):
                    parts.append(str(data["markdown"]).strip())
                elif entity_type == "MEDIA" and data.get("caption"):
                    parts.append(f"*{data['caption']}*")
                elif entity_type == "DIVIDER":
                    parts.append("---")
            continue
        if not text:
            continue
        styled = _apply_inline_styles(text, block.get("inlineStyleRanges") or [])
        if block_type == "header-one":
            parts.append(f"## {styled}")
        elif block_type == "header-two":
            parts.append(f"### {styled}")
        elif block_type == "unordered-list-item":
            parts.append(f"- {styled}")
        elif block_type == "ordered-list-item":
            parts.append(f"1. {styled}")
        else:
            parts.append(styled)
    return "\n\n".join(part for part in parts if part.strip())
