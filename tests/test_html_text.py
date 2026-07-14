from __future__ import annotations

from bs4 import BeautifulSoup

from linkedin_api.html_text import extract_html_body_text, x_article_blocks_to_text


def test_extract_html_preserves_inline_code_in_paragraph():
    html = """
    <html><body><main>
    <p>Accented characters like <code>é</code> may be escaped in JSON as
    <code>\\u00e9</code>. So <code>é</code> cannot form.</p>
    </main></body></html>
    """
    text = extract_html_body_text(BeautifulSoup(html, "html.parser"))
    assert (
        text
        == "Accented characters like `é` may be escaped in JSON as `\\u00e9`. So `é` cannot form."
    )


def test_x_article_blocks_to_text_flattens_paragraphs():
    content = {
        "blocks": [
            {"type": "unstyled", "text": "Intro paragraph.", "inlineStyleRanges": []},
            {"type": "header-one", "text": "Section", "inlineStyleRanges": []},
            {
                "type": "atomic",
                "text": " ",
                "entityRanges": [{"key": 0, "length": 1, "offset": 0}],
            },
        ],
        "entityMap": [
            {
                "key": "0",
                "value": {
                    "type": "MARKDOWN",
                    "data": {"markdown": "```python\nprint('hi')\n```"},
                },
            }
        ],
    }
    text = x_article_blocks_to_text(content)
    assert "Intro paragraph." in text
    assert "## Section" in text
    assert "```python" in text
