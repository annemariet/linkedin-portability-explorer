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

    def test_parse_category_and_tech(self) -> None:
        raw = (
            "AUTHOR: Jane Doe\n"
            "CATEGORY: tutorial\n"
            "TLDR: How to run stateful workloads on Kubernetes.\n"
            "- Covers **StatefulSets** and **Persistent Volumes**.\n"
            "TOPICS: kubernetes, storage\n"
            "TECH: Kubernetes, PostgreSQL\n"
        )
        parsed = parse_summary_response(raw)
        self.assertEqual("tutorial", parsed.category)
        self.assertEqual(["Kubernetes", "PostgreSQL"], parsed.technologies)

    def test_parse_unknown_category_normalizes_to_empty(self) -> None:
        raw = "AUTHOR: Unknown\nCATEGORY: not a real category\nTLDR: A post.\n"
        parsed = parse_summary_response(raw)
        self.assertEqual("", parsed.category)

    def test_parse_without_category_or_tech_defaults_empty(self) -> None:
        raw = "AUTHOR: Unknown\nTLDR: A post.\n- A bullet.\n"
        parsed = parse_summary_response(raw)
        self.assertEqual("", parsed.category)
        self.assertEqual([], parsed.technologies)
        self.assertEqual([], parsed.people)

    def test_parse_people_captures_unlinked_names(self) -> None:
        raw = (
            "AUTHOR: Jane Doe\n"
            "CATEGORY: opinion\n"
            "TLDR: Thanks to the team for shipping this.\n"
            "- Credits **John Smith** for the launch.\n"
            "TOPICS: product launch\n"
            "TECH: \n"
            "PEOPLE: John Smith\n"
        )
        parsed = parse_summary_response(raw)
        self.assertEqual(["John Smith"], parsed.people)

    def test_post_user_prompt_includes_length_hint(self) -> None:
        from linkedin_api.summary_text import build_post_user_prompt

        prompt = build_post_user_prompt(content="Hello world")
        self.assertIn("Post length: 11 characters", prompt)
        self.assertIn("800+", prompt)


if __name__ == "__main__":
    unittest.main()
