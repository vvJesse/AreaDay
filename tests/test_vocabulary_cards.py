from __future__ import annotations

import csv
import gzip
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from continuous_state import ContinuousStore  # noqa: E402
from vocabulary_cards import build_catalog, card_id, prepare_review_input  # noqa: E402


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def card(lemma: str, *, meaning_en: str = "English definition.", meaning_zh: str = "中文释义。") -> dict:
    return {
        "card_id": card_id(lemma, "noun"),
        "sense_key": card_id(lemma, "noun"),
        "lemma": lemma,
        "part_of_speech": "noun",
        "meaning_en": meaning_en,
        "meaning_zh": meaning_zh,
        "meaning_origin": "ecdict",
        "source_paper_id": "W1",
        "source_title": "Source paper",
        "source_url": "https://doi.org/10.1000/source",
        "context": f"A source sentence containing {lemma}.",
        "total_count": 10,
        "document_count": 2,
        "document_share": 0.5,
    }


def brief_payload() -> dict:
    return {
        "brief_id": "brief-card-canonicalization",
        "period_start": "2026-09-01",
        "period_end": "2026-09-07",
        "headline": "Synthetic brief",
        "summary": "Synthetic summary.",
        "items": [
            {
                "item_id": "paper-one",
                "item_type": "new_paper",
                "title": "First source",
                "source_url": "https://example.invalid/one",
                "value_reason": "Synthetic value.",
                "shadow_preview": "Synthetic preview.",
                "vocabulary": [
                    {
                        "lemma": "beta",
                        "part_of_speech": "noun",
                        "context": "A fresh paper context for beta.",
                        "evidence_context_id": "paper-one:beta",
                    }
                ],
            },
            {
                "item_id": "paper-two",
                "item_type": "public_report",
                "title": "Second source",
                "source_url": "https://example.invalid/two",
                "value_reason": "Synthetic value.",
                "shadow_preview": "Synthetic preview.",
                "vocabulary": [],
            },
        ],
    }


class VocabularyCardStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        analysis = self.workspace / "analysis"
        analysis.mkdir()
        (analysis / "papers.jsonl").write_text("", encoding="utf-8")
        (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
        (analysis / "terminology-explanations.json").write_text("{}\n", encoding="utf-8")
        write_jsonl(
            analysis / "vocabulary-card-catalog.jsonl",
            [
                card("alpha", meaning_zh="阿尔法释义。"),
                card("beta", meaning_en="", meaning_zh="贝塔释义。"),
                card("gamma", meaning_zh="伽马释义。"),
            ],
        )
        (analysis / "personalized-vocabulary.tsv").write_text(
            "lemma\tpart_of_speech\tclassification\timportance_tier\n"
            "alpha\tnoun\tlikely_known\tA\n"
            "beta\tnoun\tlikely_unknown\tB\n"
            "gamma\tnoun\timportant_boundary\tA\n",
            encoding="utf-8",
        )
        self.store = ContinuousStore(self.workspace, domain_id="test", display_name="Test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_new_word_candidates_are_local_and_follow_calibrated_threshold_output(self) -> None:
        candidates = self.store.new_word_candidates(limit=5)
        self.assertEqual([item["lemma"] for item in candidates], ["gamma", "beta"])
        self.assertEqual(candidates[0]["classification"], "important_boundary")
        self.assertNotIn("alpha", [item["lemma"] for item in candidates])
        self.assertEqual(candidates[1]["meaning_en"], "")
        self.assertEqual(candidates[1]["meaning_zh"], "贝塔释义。")

    def test_user_choice_creates_one_local_fsrs_record_without_a_brief(self) -> None:
        beta = self.store.new_word_candidates(limit=5)[1]
        with self.assertRaisesRegex(ValueError, "新词范围"):
            self.store.set_new_word_status(card_id("alpha", "noun"), "learning")
        item_id = self.store.set_new_word_status(beta["card_id"], "learning")
        self.assertEqual(self.store.summary()["learning_count"], 1)
        due = self.store.due_words()
        self.assertEqual([item.item_id for item in due], [item_id])
        self.assertEqual(due[0].meaning_en, "")
        self.store.set_new_word_status(beta["card_id"], "mastered")
        self.assertEqual(self.store.summary()["mastered_count"], 1)

    def test_future_brief_reuses_canonical_meanings_and_keeps_its_own_context(self) -> None:
        payload = brief_payload()
        stored = self.store.import_brief(payload)
        word = stored["items"][0]["vocabulary"][0]
        self.assertEqual(word["meaning_zh"], "贝塔释义。")
        self.assertEqual(word["meaning_en"], "")
        self.assertEqual(word["sense_key"], card_id("beta", "noun"))
        self.assertEqual(word["context"], "A fresh paper context for beta.")

        replay = brief_payload()
        replay["items"][0]["vocabulary"][0].update(
            {"meaning_zh": "Wrong replacement.", "meaning_en": "Wrong replacement."}
        )
        self.assertEqual(self.store.import_brief(replay), stored)

        self.store.start_preheat("paper-one")
        later = brief_payload()
        later["brief_id"] = "brief-card-canonicalization-later"
        later["items"][0].update(
            {
                "item_id": "paper-three",
                "title": "Later source",
                "source_url": "https://example.invalid/three",
            }
        )
        later["items"][1]["item_id"] = "paper-four"
        later_word = later["items"][0]["vocabulary"][0]
        later_word.update(
            {
                "context": "A later paper context for beta.",
                "evidence_context_id": "paper-three:beta",
                "meaning_zh": "Another incorrect meaning.",
            }
        )
        self.store.import_brief(later)
        self.store.start_preheat("paper-three")
        due = self.store.due_words()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].meaning_zh, "贝塔释义。")


class VocabularyCardBuildTests(unittest.TestCase):
    def test_review_candidates_come_from_finalized_vocabulary_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            analysis = workspace / "analysis"
            analysis.mkdir()
            write_jsonl(
                analysis / "pre-orthography-vocabulary-map.jsonl",
                [
                    {
                        "lemma": lemma,
                        "part_of_speech": "noun",
                        "representative_sentences": [],
                        "source_papers": ["W1"],
                    }
                    for lemma in ("whic", "ery", "tly")
                ],
            )
            write_jsonl(
                analysis / "vocabulary-map.jsonl",
                [
                    {
                        "lemma": "canonicalterm",
                        "part_of_speech": "noun",
                        "representative_sentences": [
                            {"openalex_id": "W1", "sentence": "Canonicalterm is valid."}
                        ],
                        "source_papers": ["W1"],
                    }
                ],
            )
            (analysis / "orthography-review-summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reviewer": "current-host-agent",
                        "reviewed_candidate_count": 0,
                        "replacement_count": 0,
                        "drop_count": 0,
                        "explicit_keep_count": 0,
                        "unchanged_candidate_count": 0,
                    }
                ),
                encoding="utf-8",
            )
            (analysis / "corpus-stats.json").write_text(
                json.dumps({"orthography_review_applied": True}),
                encoding="utf-8",
            )
            dictionary = analysis / "dictionary.tsv.gz"
            with gzip.open(dictionary, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["lemma", "part_of_speech", "meaning_en", "meaning_zh"],
                    delimiter="\t",
                )
                writer.writeheader()

            result = prepare_review_input(workspace, dictionary)
            payload = json.loads(
                (analysis / "vocabulary-card-review-input.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result["candidate_count"], 1)
            self.assertEqual(
                [item["observed_lemma"] for item in payload["candidates"]],
                ["canonicalterm"],
            )

    def test_dictionary_first_and_agent_fallback_write_complete_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            analysis = workspace / "analysis"
            analysis.mkdir()
            write_jsonl(
                analysis / "papers.jsonl",
                [{"openalex_id": "W1", "title": "Source", "doi": "10.1000/source"}],
            )
            write_jsonl(
                analysis / "vocabulary-map.jsonl",
                [
                    {
                        "lemma": "alpha",
                        "part_of_speech": "noun",
                        "total_count": 5,
                        "document_count": 2,
                        "document_share": 0.5,
                        "representative_sentences": [{"openalex_id": "W1", "sentence": "Alpha is here."}],
                    },
                    {
                        "lemma": "beta",
                        "part_of_speech": "noun",
                        "total_count": 3,
                        "document_count": 1,
                        "document_share": 0.25,
                        "representative_sentences": [{"openalex_id": "W1", "sentence": "Beta is here."}],
                    },
                ],
            )
            selection = analysis / "domain-review-selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "vocabulary_card_glosses": {
                            "beta": {"meaning_zh": "贝塔的人工释义。"}
                        }
                    }
                ),
                encoding="utf-8",
            )
            dictionary = analysis / "dictionary.tsv.gz"
            with gzip.open(dictionary, "wt", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["lemma", "part_of_speech", "meaning_en", "meaning_zh"],
                    delimiter="\t",
                )
                writer.writeheader()
                writer.writerow(
                    {"lemma": "alpha", "part_of_speech": "n.", "meaning_en": "first", "meaning_zh": "第一个"}
                )

            result = build_catalog(workspace, selection, dictionary)
            cards = [json.loads(line) for line in (analysis / "vocabulary-card-catalog.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result, {"card_count": 2, "ecdict_count": 1, "agent_count": 1, "english_count": 1})
            self.assertEqual(cards[0]["meaning_origin"], "ecdict")
            self.assertEqual(cards[1]["meaning_origin"], "agent")
            self.assertEqual(cards[1]["meaning_en"], "")


if __name__ == "__main__":
    unittest.main()
