from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from linkedin_api.llm_config import LLMResponse
from linkedin_api.topic_tags import (
    _looks_non_english,
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

    def test_looks_non_english(self) -> None:
        self.assertFalse(_looks_non_english(["open source", "devops"]))
        self.assertTrue(_looks_non_english(["réseaux sociaux"]))

    @patch("linkedin_api.topic_tags._resolve_summary_llm", return_value=None)
    def test_topics_to_catalog_tags_without_llm_uses_accent_fold(self, _mock) -> None:
        tags = topics_to_catalog_tags(["réseaux", "café"], llm=None, quiet=True)
        self.assertEqual(tags, ["reseaux", "cafe"])

    def test_topics_to_catalog_tags_skips_llm_when_already_english(self) -> None:
        """Design b: the extraction prompt already asks for English topics,
        so the common case should cost zero extra LLM calls here."""
        llm = MagicMock()
        tags = topics_to_catalog_tags(["Open Source", "DevOps"], llm=llm)
        llm.invoke.assert_not_called()
        self.assertEqual(tags, ["open-source", "devops"])

    def test_topics_to_catalog_tags_translates_when_non_english(self) -> None:
        """Heuristic fallback: non-ASCII topics still trigger one translate call."""
        llm = MagicMock()
        llm.invoke.return_value = LLMResponse(
            content="artificial intelligence, social networks"
        )
        tags = topics_to_catalog_tags(
            ["intelligence artificielle", "réseaux sociaux"], llm=llm
        )
        llm.invoke.assert_called_once()
        self.assertEqual(tags, ["artificial-intelligence", "social-networks"])

    def test_catalog_tags_for_topics_prefers_stored(self) -> None:
        tags = catalog_tags_for_topics(
            ["ignored"],
            catalog_tags=["artificial-intelligence", "leadership"],
            llm=None,
        )
        self.assertEqual(tags, ["artificial-intelligence", "leadership"])


if __name__ == "__main__":
    unittest.main()
