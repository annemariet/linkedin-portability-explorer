from __future__ import annotations

import unittest

from linkedin_api.summary_text import parse_summary_response


class SummaryTextTests(unittest.TestCase):
    def test_parse_french_summary(self) -> None:
        raw = (
            "AUTHOR: Marie Dupont\n"
            "TLDR: L'IA accélère la livraison produit chez Lottie.\n"
            "- **Modèle** de delivery réorganisé autour de l'expérimentation.\n"
            "- Les erreurs initiales ont forcé une **culture** plus honnête.\n"
            "TOPICS: IA, produit, leadership\n"
        )
        parsed = parse_summary_response(raw)
        self.assertEqual("Marie Dupont", parsed.author)
        self.assertIn("Lottie", parsed.tldr)
        self.assertEqual(2, len(parsed.bullets))
        self.assertIn("IA", parsed.topics[0])

    def test_parse_empty_returns_not_ok(self) -> None:
        parsed = parse_summary_response("")
        self.assertFalse(parsed.ok)

    def test_post_user_prompt_includes_length_hint(self) -> None:
        from linkedin_api.summary_text import build_post_user_prompt

        prompt = build_post_user_prompt(content="Hello world")
        self.assertIn("Post length: 11 characters", prompt)
        self.assertIn("800+", prompt)


if __name__ == "__main__":
    unittest.main()
