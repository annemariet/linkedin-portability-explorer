"""Tests for HTML → markdown body extraction."""

from __future__ import annotations

import unittest

from bs4 import BeautifulSoup

from linkedin_api.html_text import extract_html_body_text


class HtmlTextHeadingTests(unittest.TestCase):
    def test_decorative_hash_in_h2_does_not_double_hash(self) -> None:
        soup = BeautifulSoup(
            "<html><body><h2>#How it works</h2><p>Body.</p></body></html>",
            "html.parser",
        )
        text = extract_html_body_text(soup)
        self.assertIn("## How it works", text)
        self.assertNotIn("## #How", text)

    def test_normal_h2_unchanged(self) -> None:
        soup = BeautifulSoup(
            "<html><body><h2>How it works</h2></body></html>",
            "html.parser",
        )
        text = extract_html_body_text(soup)
        self.assertIn("## How it works", text)


if __name__ == "__main__":
    unittest.main()
