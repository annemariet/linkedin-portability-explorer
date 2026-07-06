from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from linkedin_api.llm_config import LLMResponse
from linkedin_api.topic_tags import (
    _parse_translate_line,
    catalog_tags_for_topics,
    topics_to_catalog_tags,
    translate_topics_to_english,
)


class TopicTagsTests(unittest.TestCase):
    def test_parse_translate_line(self) -> None:
        self.assertEqual(
            _parse_translate_line("artificial intelligence, product leadership"),
            ["artificial intelligence", "product leadership"],
        )
        self.assertEqual(
            _parse_translate_line("TOPICS: machine learning, devops"),
            ["machine learning", "devops"],
        )

    def test_translate_topics_to_english(self) -> None:
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(
            content="artificial intelligence, social networks"
        )
        self.assertEqual(
            translate_topics_to_english(
                ["intelligence artificielle", "réseaux sociaux"],
                llm,
            ),
            ["artificial intelligence", "social networks"],
        )

    @patch("linkedin_api.topic_tags._resolve_summary_llm", return_value=None)
    def test_topics_to_catalog_tags_without_llm_uses_accent_fold(self, _mock) -> None:
        tags = topics_to_catalog_tags(["réseaux", "café"], llm=None, quiet=True)
        self.assertEqual(tags, ["reseaux", "cafe"])

    def test_topics_to_catalog_tags_with_llm(self) -> None:
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(content="open source, c plus plus")
        tags = topics_to_catalog_tags(["Open Source", "C++"], llm=llm)
        self.assertEqual(tags, ["open-source", "c-plus-plus"])

    def test_catalog_tags_for_topics_prefers_stored(self) -> None:
        tags = catalog_tags_for_topics(
            ["ignored"],
            catalog_tags=["artificial-intelligence", "leadership"],
            llm=None,
        )
        self.assertEqual(tags, ["artificial-intelligence", "leadership"])


if __name__ == "__main__":
    unittest.main()
