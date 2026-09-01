from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SKILL_DIR = Path(__file__).resolve().parents[1]
APP_PATH = SKILL_DIR / "app" / "server.py"
SPEC = importlib.util.spec_from_file_location("vocabulary_calibration_app", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def word(lemma: str, document_count: int) -> object:
    return APP.Word(
        lemma=lemma,
        part_of_speech="noun",
        total_count=document_count * 2,
        document_count=document_count,
        document_share=document_count / 100,
        zipf=3.0,
        frequency_prior_probability=0.5,
        cefr_level=None,
        cefr_adjustment=0.0,
        exam_tags=(),
        exam_adjustment=0.0,
        education_adjustment=0.0,
        prior_probability=0.5,
    )


class VocabularyThresholdTests(unittest.TestCase):
    def test_completed_result_is_frozen_when_the_app_reopens(self) -> None:
        words = [word(f"word{index}", 2) for index in range(30)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "known_threshold": 0.9,
                        "answers": [
                            {"lemma": item.lemma, "response": "known"}
                            for item in words
                        ],
                    }
                ),
                encoding="utf-8",
            )
            frozen = {
                "counts": {
                    "total": 30,
                    "likely_known": 20,
                    "uncertain": 7,
                    "likely_unknown": 3,
                    "remaining_after_conservative_exclusion": 10,
                    "important_boundary_protected": 0,
                },
                "threshold": {"selected_percent": 90},
                "importance": {
                    "corpus_document_count": 100,
                    "priority_word_count": 4,
                    "occasional_word_count": 6,
                    "tiers": [],
                },
                "known_boundary": [],
                "remaining_boundary": [],
            }
            export_path = state_path.with_name("personalized-vocabulary.tsv")
            export_path.write_text(
                "lemma\tclassification\n"
                + "".join(f"word{index}\tlikely_known\n" for index in range(30)),
                encoding="utf-8",
            )
            frozen["vocabulary_snapshot_sha256"] = "fixture-snapshot"
            frozen["personalized_vocabulary_sha256"] = hashlib.sha256(
                export_path.read_bytes()
            ).hexdigest()
            state_path.with_name("vocabulary-calibration-result.json").write_text(
                json.dumps(frozen), encoding="utf-8"
            )

            session = APP.CalibrationSession(words, state_path, "test corpus")
            with patch.object(session, "result", side_effect=AssertionError("recalculated")):
                reopened = session.public_state()["result"]

        self.assertEqual(reopened["counts"]["remaining_after_conservative_exclusion"], 10)
        self.assertEqual(reopened["threshold"]["selected_percent"], 90)

    def test_completed_result_never_expands_to_a_changed_raw_map(self) -> None:
        words = [word(f"word{index}", 2) for index in range(700)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "known_threshold": 0.9,
                        "answers": [
                            {"lemma": f"word{index}", "response": "known"}
                            for index in range(30)
                        ],
                    }
                ),
                encoding="utf-8",
            )
            frozen = {
                "counts": {
                    "total": 300,
                    "likely_known": 0,
                    "uncertain": 200,
                    "likely_unknown": 100,
                    "remaining_after_conservative_exclusion": 300,
                    "important_boundary_protected": 0,
                },
                "threshold": {"selected_percent": 90},
                "importance": {
                    "corpus_document_count": 100,
                    "priority_word_count": 80,
                    "occasional_word_count": 220,
                    "tiers": [],
                },
                "known_boundary": [],
                "remaining_boundary": [],
            }
            result_path = state_path.with_name("vocabulary-calibration-result.json")
            export_path = state_path.with_name("personalized-vocabulary.tsv")
            export_path.write_text(
                "lemma\tclassification\n"
                + "".join(f"word{index}\tlikely_unknown\n" for index in range(300)),
                encoding="utf-8",
            )
            frozen["vocabulary_snapshot_sha256"] = "original-raw-map"
            frozen["personalized_vocabulary_sha256"] = hashlib.sha256(
                export_path.read_bytes()
            ).hexdigest()
            result_path.write_text(json.dumps(frozen), encoding="utf-8")
            result_hash = result_path.read_bytes()
            export_hash = export_path.read_bytes()

            session = APP.CalibrationSession(words, state_path, "changed raw map")
            reopened = session.public_state()["result"]

            self.assertEqual(reopened["counts"]["total"], 300)
            self.assertEqual(
                reopened["counts"]["remaining_after_conservative_exclusion"], 300
            )
            self.assertEqual(session.persisted_export_tsv(), export_hash.decode())
            self.assertEqual(result_path.read_bytes(), result_hash)
            self.assertEqual(export_path.read_bytes(), export_hash)

    def test_completed_result_with_missing_export_fails_closed(self) -> None:
        words = [word(f"word{index}", 2) for index in range(30)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "answers": [
                            {"lemma": item.lemma, "response": "known"} for item in words
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result_path = state_path.with_name("vocabulary-calibration-result.json")
            result_path.write_text(
                json.dumps(
                    {
                        "counts": {"total": 30},
                        "importance": {"tiers": []},
                    }
                ),
                encoding="utf-8",
            )
            before = result_path.read_bytes()
            with self.assertRaisesRegex(RuntimeError, "静默重算"):
                APP.CalibrationSession(words, state_path, "corrupt completed corpus")
            self.assertEqual(result_path.read_bytes(), before)
            self.assertFalse(state_path.with_name("personalized-vocabulary.tsv").exists())

    def test_out_of_order_threshold_and_reset_mutations_cannot_win(self) -> None:
        words = [word(f"word{index}", 2) for index in range(30)]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path = root / "session.json"
            state_path.write_text(
                json.dumps(
                    {
                        "known_threshold": 0.9,
                        "mutation_revision": 0,
                        "answers": [
                            {"lemma": item.lemma, "response": "known"} for item in words
                        ],
                    }
                ),
                encoding="utf-8",
            )
            seed = APP.CalibrationSession(words, root / "fresh.json", "seed")
            seed.answers = [
                {"lemma": item.lemma, "response": "known"} for item in words
            ]
            seed.state_path = state_path
            seed._result_path = state_path.with_name("vocabulary-calibration-result.json")
            seed._export_path = state_path.with_name("personalized-vocabulary.tsv")
            seed._write_final_outputs()

            session = APP.CalibrationSession(words, state_path, "ordered mutations")
            session.set_threshold_percent(95, mutation_revision=2)
            session.set_threshold_percent(80, mutation_revision=1)
            self.assertEqual(session.public_state()["threshold"]["selected_percent"], 95)

            session.reset(mutation_revision=3)
            session.set_threshold_percent(80, mutation_revision=2)
            state = session.public_state()
            self.assertEqual(state["answered"], 0)
            self.assertEqual(state["threshold"]["selected_percent"], 90)

    def test_important_words_are_protected_only_near_the_boundary(self) -> None:
        words = [word("core", 10), word("edge", 2), word("unknown", 5)]
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(APP, "QUESTION_LIMIT", len(words)):
                session = APP.CalibrationSession(
                    words, Path(temporary) / "session.json", "test corpus"
                )
            with patch.object(
                session,
                "probabilities",
                return_value={"core": 0.92, "edge": 0.92, "unknown": 0.20},
            ):
                result = session.result()

        self.assertEqual(result["counts"]["likely_known"], 1)
        self.assertEqual(result["counts"]["remaining_after_conservative_exclusion"], 2)
        self.assertEqual(result["counts"]["important_boundary_protected"], 1)
        self.assertEqual(result["threshold"]["selected_percent"], 90)
        self.assertEqual(result["threshold"]["default_percent"], 90)

    def test_threshold_accepts_each_integer_in_the_visible_range(self) -> None:
        words = [word("sample", 2)]
        with tempfile.TemporaryDirectory() as temporary:
            with patch.object(APP, "QUESTION_LIMIT", len(words)):
                session = APP.CalibrationSession(
                    words, Path(temporary) / "session.json", "test corpus"
                )
            session.set_threshold_percent(75)
            self.assertEqual(session.known_threshold, 0.75)
            session.set_threshold_percent(98)
            self.assertEqual(session.known_threshold, 0.98)
            with self.assertRaises(ValueError):
                session.set_threshold_percent(74)
            with self.assertRaises(ValueError):
                session.set_threshold_percent(99)


if __name__ == "__main__":
    unittest.main()
