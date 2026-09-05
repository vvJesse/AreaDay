from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from apply_orthography_review import validate_selection  # noqa: E402


class OrthographySelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.review_input = {
            "candidates": [
                {"observed_lemma": "transformer"},
                {"observed_lemma": "whic"},
                {"observed_lemma": "pretraine"},
            ]
        }

    def test_rejects_any_suspicious_lemma_without_a_decision(self) -> None:
        selection = {
            "schema_version": 1,
            "reviewer": "current-host-agent",
            "lemma_keeps": ["transformer"],
            "lemma_replacements": {"pretraine": "pretrain"},
            "lemma_drops": [],
        }

        with self.assertRaisesRegex(ValueError, "missing decisions.*whic"):
            validate_selection(selection, self.review_input)

    def test_accepts_exactly_one_decision_for_every_suspicious_lemma(self) -> None:
        selection = {
            "schema_version": 1,
            "reviewer": "current-host-agent",
            "lemma_keeps": ["transformer"],
            "lemma_replacements": {"pretraine": "pretrain"},
            "lemma_drops": ["whic"],
        }

        replacements, drops, keeps = validate_selection(selection, self.review_input)

        self.assertEqual(replacements, {"pretraine": "pretrain"})
        self.assertEqual(drops, {"whic"})
        self.assertEqual(keeps, {"transformer"})

    def test_rejects_conflicting_decisions(self) -> None:
        selection = {
            "schema_version": 1,
            "reviewer": "current-host-agent",
            "lemma_keeps": ["transformer", "whic"],
            "lemma_replacements": {"pretraine": "pretrain"},
            "lemma_drops": ["whic"],
        }

        with self.assertRaisesRegex(ValueError, "more than one review decision.*whic"):
            validate_selection(selection, self.review_input)


if __name__ == "__main__":
    unittest.main()
