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
                            "beta": {
                                "meaning_zh": "贝塔的人工释义。",
                                "sense_key": "beta-concept",
                            }
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
            self.assertEqual(cards[1]["sense_key"], "beta-concept")

    def test_domain_acronym_is_reviewed_in_context_instead_of_trusting_ecdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            analysis = workspace / "analysis"
            analysis.mkdir()
            write_jsonl(
                analysis / "papers.jsonl",
                [{"openalex_id": "W1", "title": "LLM research", "doi": "10.1000/llm"}],
            )
            vocabulary = [
                {
                    "lemma": "llm",
                    "part_of_speech": "NOUN",
                    "surface_forms": [
                        {"form": "LLMs", "count": 12},
                        {"form": "LLM", "count": 5},
                    ],
                    "total_count": 17,
                    "document_count": 2,
                    "document_share": 1.0,
                    "representative_sentences": [
                        {
                            "openalex_id": "W1",
                            "sentence": "Large language models (LLMs) generate text.",
                        }
                    ],
                    "source_papers": ["W1"],
                },
                {
                    "lemma": "alpha",
                    "part_of_speech": "NOUN",
                    "surface_forms": [{"form": "alpha", "count": 3}],
                    "total_count": 3,
                    "document_count": 1,
                    "document_share": 0.5,
                    "representative_sentences": [
                        {"openalex_id": "W1", "sentence": "Alpha is first."}
                    ],
                    "source_papers": ["W1"],
                },
            ]
            write_jsonl(analysis / "vocabulary-map.jsonl", vocabulary)
            terminology = [
                {
                    "term": "large language model",
                    "acronyms": ["LLM"],
                    "source_papers": ["W1"],
                    "representative_sentences": [
                        {
                            "openalex_id": "W1",
                            "sentence": "Large language models (LLMs) generate text.",
                        }
                    ],
                },
                {
                    "term": "large language models",
                    "acronyms": ["LLM"],
                    "source_papers": ["W1"],
                    "representative_sentences": [
                        {
                            "openalex_id": "W1",
                            "sentence": "Large language models (LLMs) generate text.",
                        }
                    ],
                },
            ]
            write_jsonl(analysis / "terminology-candidates.jsonl", terminology)
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
                json.dumps({"orthography_review_applied": True}), encoding="utf-8"
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
                    {
                        "lemma": "llm",
                        "part_of_speech": "",
                        "meaning_en": "n an advanced law degree",
                        "meaning_zh": "abbr. 法学硕士；法律硕士",
                    }
                )
                writer.writerow(
                    {
                        "lemma": "alpha",
                        "part_of_speech": "n.",
                        "meaning_en": "first",
                        "meaning_zh": "第一个",
                    }
                )

            result = prepare_review_input(workspace, dictionary)
            payload = json.loads(
                (analysis / "vocabulary-card-review-input.json").read_text(encoding="utf-8")
            )
            self.assertEqual(result["candidate_count"], 1)
            candidate = payload["candidates"][0]
            self.assertEqual(candidate["observed_lemma"], "llm")
            self.assertEqual(candidate["acronym_expansions"], ["large language model"])
            self.assertEqual(candidate["suggested_sense_key"], "large-language-model")
            self.assertIn("法学硕士", candidate["dictionary_candidates"][0]["meaning_zh"])

            write_jsonl(analysis / "first-terminology-map.jsonl", terminology[:1])
            selection = analysis / "domain-review-selection.json"
            selection.write_text(
                json.dumps(
                    {
                        "vocabulary_card_glosses": {
                            "llm": {
                                "meaning_en": "Large language model.",
                                "meaning_zh": "大语言模型。",
                                "sense_key": "agent-proposed-llm-sense",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            catalog_result = build_catalog(workspace, selection, dictionary)
            cards = {
                row["lemma"]: row
                for row in (
                    json.loads(line)
                    for line in (analysis / "vocabulary-card-catalog.jsonl")
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
            }
            self.assertEqual(catalog_result["ecdict_count"], 1)
            self.assertEqual(catalog_result["agent_count"], 1)
            self.assertEqual(cards["llm"]["meaning_zh"], "大语言模型。")
            self.assertEqual(cards["llm"]["meaning_origin"], "agent-contextual")
            self.assertEqual(cards["llm"]["sense_key"], "large-language-model")
            self.assertEqual(cards["alpha"]["meaning_origin"], "ecdict")

    def test_literal_escaped_newlines_are_not_written_to_dictionary_glosses(self) -> None:
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
                        "lemma": "formalize",
                        "part_of_speech": "verb",
                        "total_count": 2,
                        "document_count": 1,
                        "document_share": 1.0,
                        "representative_sentences": [
                            {"openalex_id": "W1", "sentence": "We formalize the method."}
                        ],
                    }
                ],
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
                    {
                        "lemma": "formalize",
                        "part_of_speech": "v.",
                        "meaning_en": "make formal or official",
                        "meaning_zh": "vt. 使正式, 使整形, 形式化\\nvi. 拘泥于形式",
                    }
                )
            selection = analysis / "domain-review-selection.json"
            selection.write_text(json.dumps({"vocabulary_card_glosses": {}}), encoding="utf-8")

            build_catalog(workspace, selection, dictionary)
            stored = json.loads(
                (analysis / "vocabulary-card-catalog.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()[0]
            )
            self.assertNotIn("\\n", stored["meaning_zh"])
            self.assertEqual(
                stored["meaning_zh"], "vt. 使正式, 使整形, 形式化；vi. 拘泥于形式"
            )


if __name__ == "__main__":
    unittest.main()
