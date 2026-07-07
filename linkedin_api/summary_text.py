"""Explorer-style summary prompts and parsing (aligned with newsletter-summarizer)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_LANGUAGE_RULE = (
    "Write AUTHOR, TLDR, TOPICS (if requested), and every bullet in the same language "
    "as the source text (e.g. French source → French output; English → English)."
)

POST_SYSTEM_PROMPT = (
    "You summarize LinkedIn posts for a busy engineer. Be specific; avoid generic filler.\n"
    f"{_LANGUAGE_RULE}\n"
    "Output format (exactly this structure, in order):\n"
    "1) First line: AUTHOR: <post author if known from context; otherwise Unknown>\n"
    "2) Second line: TLDR: <exactly one sentence, at most 25 words; do not start with "
    '"This post discusses">\n'
    "3) Then bullet points — count must match post length (see user message for character count):\n"
    "   - Very short post (under ~200 characters): 0 or 1 bullet is fine; TLDR alone may suffice.\n"
    "   - Medium post: 1–2 bullets.\n"
    "   - Long post (roughly 800+ characters): up to 4 bullets; never pad with filler.\n"
    "   Each bullet is one or two sentences in Markdown. Use **bold** for critical terms where helpful.\n"
    "4) Last line: TOPICS: <comma-separated themes, 1-5 items; always in English, "
    "even when the post is in another language>\n"
    "Do not invent facts."
)

ARTICLE_SYSTEM_PROMPT = (
    "You are a careful technical analyst. Read the full article text and extract key "
    "learnings: main claims, definitions, tradeoffs, and actionable implications.\n"
    f"{_LANGUAGE_RULE}\n"
    "Output format (exactly this structure, in order):\n"
    "1) First line: AUTHOR: <human-readable author or publication if clearly stated; "
    "otherwise Unknown>\n"
    "2) Second line: TLDR: <exactly one sentence, at most 25 words; synthesize the article; "
    'do not start with "This article discusses">\n'
    "3) Then 3 to 6 bullet points. Each bullet is one or two sentences in Markdown. "
    "Use **bold** for critical terms where helpful.\n"
    "Do not repeat the title as a bullet. Do not invent URLs or facts."
)

_AUTHOR_LINE_RE = re.compile(r"^AUTHOR:\s*(.+)$", re.IGNORECASE)
_TLDR_LINE_RE = re.compile(r"^TLDR:\s*(.+)$", re.IGNORECASE)
_TOPICS_LINE_RE = re.compile(r"^TOPICS:\s*(.+)$", re.IGNORECASE)
_BULLET_RE = re.compile(r"^[-*•]\s+")


@dataclass(frozen=True)
class ParsedSummary:
    author: str = ""
    tldr: str = ""
    bullets: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    summary_text: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.tldr.strip() or self.bullets)


def build_post_user_prompt(
    *,
    content: str,
    post_author: str = "",
    post_url: str = "",
) -> str:
    author_hint = (post_author or "").strip() or "(unknown)"
    url_line = f"Post URL: {post_url}\n" if post_url else ""
    body = content.strip()
    char_count = len(body)
    length_hint = (
        f"Post length: {char_count} characters. "
        "Scale bullets to length — a one-liner may need only TLDR (0–1 bullet); "
        "reserve 3–4 bullets for posts around 800+ characters."
    )
    return (
        "Summarize this LinkedIn post.\n"
        f"{url_line}"
        f"{length_hint}\n"
        f"Author hint: {author_hint}\n"
        "Post text:\n"
        f"{body}\n"
    )


def build_article_user_prompt(
    *,
    title: str,
    url: str,
    content: str,
    author_hint: str = "",
) -> str:
    hint = (author_hint or "").strip() or "(none from page metadata)"
    return (
        "Summarize this linked article for a busy engineer.\n"
        f"Title: {title}\n"
        f"Article URL: {url}\n"
        f"Author hint from metadata: {hint}\n"
        "Full article text (primary source):\n"
        f"{content.strip()}\n"
    )


def _normalize_tldr(text: str, *, max_words: int = 25) -> str:
    stripped = re.sub(r"\s+", " ", (text or "").strip())
    if not stripped:
        return ""
    sentence = re.split(r"(?<=[.!?])\s+", stripped, maxsplit=1)[0].strip()
    words = sentence.split()
    if len(words) <= max_words:
        return sentence
    return " ".join(words[:max_words]).rstrip(",;:") + "."


def _parse_topics_line(line: str) -> list[str]:
    raw = line.strip()
    if not raw:
        return []
    parts = [p.strip() for p in re.split(r"[,;]", raw) if p.strip()]
    return parts[:8]


def parse_summary_response(raw_output: str) -> ParsedSummary:
    """Parse AUTHOR / TLDR / TOPICS preamble and Markdown bullets."""
    lines = (raw_output or "").splitlines()
    author = ""
    tldr = ""
    topics: list[str] = []
    bullets: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line:
            idx += 1
            continue
        m_author = _AUTHOR_LINE_RE.match(line)
        if m_author:
            author = m_author.group(1).strip()
            idx += 1
            continue
        m_tldr = _TLDR_LINE_RE.match(line)
        if m_tldr:
            tldr = _normalize_tldr(m_tldr.group(1))
            idx += 1
            continue
        m_topics = _TOPICS_LINE_RE.match(line)
        if m_topics:
            topics = _parse_topics_line(m_topics.group(1))
            idx += 1
            continue
        break

    for raw in lines[idx:]:
        stripped = raw.strip()
        if not stripped:
            continue
        m_topics_line = _TOPICS_LINE_RE.match(stripped)
        if m_topics_line:
            topics = _parse_topics_line(m_topics_line.group(1))
            continue
        if _BULLET_RE.match(stripped):
            bullets.append(_BULLET_RE.sub("", stripped, count=1).strip())
        elif bullets:
            bullets[-1] = f"{bullets[-1]} {stripped}".strip()

    summary_text = tldr
    if bullets:
        summary_text = (
            tldr + ("\n" if tldr else "") + "\n".join(f"- {b}" for b in bullets)
        )
    return ParsedSummary(
        author=author,
        tldr=tldr,
        bullets=bullets,
        topics=topics,
        summary_text=summary_text.strip(),
    )


def guess_content_lang(text: str) -> str:
    """Rough FR/EN guess for catalog frontmatter (not a substitute for real detection)."""
    sample = f" {(text or '')[:2500].lower()} "
    fr_hits = sum(
        1
        for marker in (
            " le ",
            " la ",
            " les ",
            " des ",
            " une ",
            " dans ",
            " pour ",
            " avec ",
            " est ",
            " pas ",
        )
        if marker in sample
    )
    return "fr" if fr_hits >= 3 else "en"
