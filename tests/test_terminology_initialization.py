from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from domain_registry import (  # noqa: E402
    DomainRegistry,
    validate_completed_workspace,
    validate_corpus_launch_workspace,
    validate_initialized_workspace,
)
from finalize_domain_assets import finalize_assets  # noqa: E402
from finalize_host_review import finalize_review  # noqa: E402
from vocabulary_cards import card_id  # noqa: E402


SPEC = importlib.util.spec_from_file_location(
    "researchramp_app_terminology_initialization_tests",
    ROOT / "app" / "server.py",
)
APP = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")


def write_card_catalog(analysis: Path, lemmas: list[str]) -> None:
    rows = [
        {
            "card_id": card_id(lemma, "noun"),
            "sense_key": card_id(lemma, "noun"),
            "lemma": lemma,
            "part_of_speech": "noun",
            "meaning_en": f"Synthetic definition for {lemma}.",
            "meaning_zh": f"{lemma} 的合成中文解释。",
            "meaning_origin": "agent",
            "source_paper_id": "W1",
            "source_title": "Synthetic paper",
            "source_url": "https://doi.org/10.1000/synthetic",
            "context": f"Example for {lemma}.",
            "total_count": 10,
            "document_count": 2,
            "document_share": 1.0,
        }
        for lemma in lemmas
    ]
    (analysis / "vocabulary-card-catalog.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def make_workspace(root: Path) -> Path:
    workspace = root / "corpus"
    analysis = workspace / "analysis"
    analysis.mkdir(parents=True)
    write_json(
        workspace / "research-profile-input.json",
        {
            "version": 1,
            "profile_id": "test-domain",
            "title": "Test Domain",
            "confirmed": True,
        },
    )
    vocabulary_rows = (
        "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
        + "".join(
            f"model{index}\tnoun\t10\t2\t1.0\n" for index in range(30)
        )
    )
    (analysis / "vocabulary-map.tsv").write_text(vocabulary_rows, encoding="utf-8")
    write_card_catalog(analysis, [f"model{index}" for index in range(30)])
    (analysis / "papers.jsonl").write_text("", encoding="utf-8")
    write_json(analysis / "orthography-review-input.json", {"candidates": []})
    write_json(
        analysis / "orthography-review-summary.json",
        {
            "schema_version": 1,
            "reviewer": "current-host-agent",
            "reviewed_candidate_count": 0,
            "replacement_count": 0,
            "drop_count": 0,
            "explicit_keep_count": 0,
            "unchanged_candidate_count": 0,
        },
    )
    write_json(
        analysis / "corpus-stats.json",
        {"orthography_review_applied": True, "content_lemma_token_count": 300},
    )
    return workspace


def corpus_args(workspace: Path) -> SimpleNamespace:
    return SimpleNamespace(
        cefr_data=Path("unused-cefr.json"),
        corpus=workspace,
        disable_cefr_prior=True,
        disable_exam_prior=True,
        domain=None,
        exam_data=Path("unused-exam.json"),
        exam_profile="cet6",
        instance_id="test-instance",
        label=None,
        library=None,
        mode="vocabulary",
        state=None,
        vocabulary=None,
    )


class ReadyCalibrationSession:
    def public_state(self) -> dict:
        return {
            "answered": 0,
            "question_limit": 30,
            "complete": False,
            "mutation_revision": 0,
            "threshold": {"selected_percent": 90},
            "responses": {"known": 0, "unknown": 0, "unsure": 0},
            "word": {"lemma": "model0", "part_of_speech": "noun"},
        }

    def personal_vocabulary_mastery(self, _mastered: set[str]) -> None:
        return None


class TerminologyInitializationTests(unittest.TestCase):
    def test_one_finalizer_makes_both_assets_ready_before_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            vocabulary = []
            for index in range(30):
                lemma = f"model{index}"
                vocabulary.append(
                    {
                        "lemma": lemma,
                        "part_of_speech": "noun",
                        "total_count": 10,
                        "document_count": 1,
                        "document_share": 1.0,
                        "dispersion": 1.0,
                        "per_document_counts": {"W1": 10},
                        "surface_forms": [{"form": lemma, "count": 10}],
                        "representative_sentences": [
                            {"openalex_id": "W1", "sentence": f"Example for {lemma}."}
                        ],
                        "source_papers": ["W1"],
                    }
                )
            (analysis / "pre-orthography-vocabulary-map.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in vocabulary),
                encoding="utf-8",
            )
            (analysis / "vocabulary-map.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in vocabulary),
                encoding="utf-8",
            )
            write_json(
                analysis / "corpus-stats.json",
                {
                    "orthography_review_applied": True,
                    "content_lemma_token_count": 300,
                    "vocabulary_entry_count": 30,
                },
            )
            (analysis / "paper-decisions.jsonl").write_text(
                json.dumps({"openalex_id": "W1", "analysis_decision": "include"})
                + "\n",
                encoding="utf-8",
            )
            (analysis / "papers.jsonl").write_text(
                json.dumps({"openalex_id": "W1", "title": "Synthetic paper", "doi": "10.1000/synthetic"}) + "\n",
                encoding="utf-8",
            )
            term = {
                "term": "robust analysis",
                "source_papers": ["W1"],
                "representative_sentences": [
                    {"openalex_id": "W1", "sentence": "Robust analysis is reliable."}
                ],
            }
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps(term) + "\n", encoding="utf-8"
            )
            selection = analysis / "domain-review-selection.json"
            write_json(
                selection,
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "terminology": {"robust analysis": 1.0},
                    "terminology_explanations": {
                        "robust analysis": {
                            "meaning_en": "Analysis that remains reliable.",
                            "meaning_zh": "保持可靠的分析。",
                            "concept_role": "Methodological quality criterion.",
                            "sense_key": "robust-analysis",
                        }
                    },
                    "vocabulary_card_glosses": {
                        lemma: {
                            "meaning_en": f"Synthetic definition for {lemma}.",
                            "meaning_zh": f"{lemma} 的合成中文解释。",
                        }
                        for lemma in (f"model{index}" for index in range(30))
                    },
                    "review_summary": "Reviewed together.",
                },
            )

            result = finalize_assets(workspace, selection)

            self.assertTrue(result["ready_for_calibration"])
            self.assertEqual(result["vocabulary"]["vocabulary_entry_count"], 30)
            self.assertEqual(
                result["terminology"]["loadable_terminology_count"], 1
            )
            self.assertTrue((analysis / "domain-assets-summary.json").is_file())
            self.assertTrue((analysis / "vocabulary-card-catalog.jsonl").is_file())
            validate_initialized_workspace(workspace)

    def test_library_can_serve_one_fully_prepared_domain_before_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = make_workspace(root)
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text(
                "", encoding="utf-8"
            )
            (analysis / "terminology-candidates.jsonl").write_text(
                "", encoding="utf-8"
            )
            write_json(analysis / "terminology-explanations.json", {})
            write_json(
                analysis / "host-review-summary.json",
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "review_passes": 1,
                    "terminology_candidate_count": 0,
                    "selected_terminology_count": 0,
                },
            )
            registry_path = root / "registry.json"
            DomainRegistry(registry_path).register(workspace)
            args = corpus_args(workspace)
            args.corpus = None
            args.library = registry_path
            args.domain = "test-domain"
            args.ready_calibration_domain = "test-domain"

            with patch.object(
                APP, "RemoteCalibrationSession", return_value=ReadyCalibrationSession()
            ):
                runtime = APP.build_runtime(args)
            state = runtime.app_state("test-domain")

            self.assertEqual(state["domain_id"], "test-domain")
            self.assertFalse(state["calibration"]["complete"])
            self.assertEqual(state["terminology"]["count"], 0)

    def test_finalization_is_complete_before_any_calibration_answers_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "papers.jsonl").write_text(
                json.dumps(
                    {
                        "openalex_id": "W1",
                        "title": "A synthetic robust-analysis paper",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            candidate = {
                "term": "robust analysis",
                "total_count": 8,
                "document_count": 2,
                "document_share": 1.0,
                "c_value": 3.2,
                "surface_forms": [{"form": "robust analysis", "count": 8}],
                "acronyms": [],
                "representative_sentences": [
                    {
                        "openalex_id": "W1",
                        "sentence": "Robust analysis remains reliable under perturbation.",
                    }
                ],
                "source_papers": ["W1"],
            }
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps(candidate) + "\n", encoding="utf-8"
            )
            selection_path = analysis / "terminology-review-selection.json"
            write_json(
                selection_path,
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "terminology": {"robust analysis": 0.95},
                    "terminology_explanations": {
                        "robust analysis": {
                            "meaning_en": "Analysis designed to remain reliable under perturbation.",
                            "meaning_zh": "在扰动下仍保持可靠的分析。",
                            "concept_role": "A methodological reliability criterion.",
                            "sense_key": "robust-analysis-method",
                        }
                    },
                },
            )

            result = finalize_review(workspace, selection_path)

            self.assertEqual(result["selected_terminology_count"], 1)
            self.assertFalse(
                (analysis / "vocabulary-calibration-session.json").exists()
            )
            self.assertFalse(
                (analysis / "vocabulary-calibration-result.json").exists()
            )
            finalized = json.loads(
                (analysis / "first-terminology-map.jsonl").read_text(encoding="utf-8")
            )
            self.assertEqual(finalized["term"], "robust analysis")
            self.assertEqual(finalized["host_review_confidence"], 0.95)
            self.assertTrue((analysis / "terminology-explanations.json").is_file())
            validate_initialized_workspace(workspace)

            with patch.object(
                APP, "RemoteCalibrationSession", return_value=ReadyCalibrationSession()
            ):
                runtime = APP.build_runtime(corpus_args(workspace))
            store = runtime.context("current-domain").continuous_store
            assert store is not None
            self.assertEqual(len(store.list_terms()), 1)
            app_state = runtime.app_state("current-domain")
            self.assertFalse(app_state["calibration"]["complete"])
            self.assertEqual(app_state["terminology"]["count"], 1)
            self.assertEqual(
                app_state["terminology"]["terms"][0]["term"],
                "robust analysis",
            )

    def test_calibration_readiness_rejects_missing_terminology_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            with self.assertRaisesRegex(
                FileNotFoundError, "first-terminology-map.jsonl"
            ):
                validate_initialized_workspace(workspace)

    def test_corpus_server_refuses_to_open_before_terminology_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))

            with self.assertRaisesRegex(
                FileNotFoundError, "first-terminology-map.jsonl"
            ):
                APP.build_runtime(corpus_args(workspace))

    def test_existing_explanations_without_sense_key_remain_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "papers.jsonl").write_text(
                json.dumps({"openalex_id": "W1", "title": "Legacy paper"}) + "\n",
                encoding="utf-8",
            )
            (analysis / "first-terminology-map.jsonl").write_text(
                json.dumps(
                    {
                        "term": "legacy term",
                        "host_review_classification": "domain-term",
                        "host_review_confidence": 1.0,
                        "representative_sentences": [
                            {
                                "openalex_id": "W1",
                                "sentence": "The legacy term appears in this paper.",
                            }
                        ],
                        "source_papers": ["W1"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps(
                    {
                        "term": "legacy term",
                        "representative_sentences": [
                            {
                                "openalex_id": "W1",
                                "sentence": "The legacy term appears in this paper.",
                            }
                        ],
                        "source_papers": ["W1"],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            write_json(
                analysis / "terminology-explanations.json",
                {
                    "legacy term": {
                        "meaning_en": "An existing reviewed term.",
                        "meaning_zh": "一个既有的已审核术语。",
                        "concept_role": "Legacy compatibility fixture.",
                    }
                },
            )
            write_json(
                analysis / "host-review-summary.json",
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "review_passes": 1,
                    "terminology_candidate_count": 1,
                    "selected_terminology_count": 1,
                },
            )

            with patch.object(
                APP, "RemoteCalibrationSession", return_value=ReadyCalibrationSession()
            ):
                runtime = APP.build_runtime(corpus_args(workspace))
            store = runtime.context("current-domain").continuous_store
            assert store is not None
            terms = store.list_terms()
            self.assertEqual(terms[0]["sense_key"], "legacy term")

    def test_completed_legacy_workspace_does_not_require_review_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text(
                "", encoding="utf-8"
            )
            write_json(analysis / "terminology-explanations.json", {})
            write_json(
                analysis / "vocabulary-calibration-session.json",
                {"answers": [{"lemma": f"word-{index}"} for index in range(30)]},
            )
            export = "lemma\tclassification\nmodel\tretained\n"
            (analysis / "personalized-vocabulary.tsv").write_text(
                export, encoding="utf-8"
            )
            write_json(
                analysis / "vocabulary-calibration-result.json",
                {
                    "counts": {"total": 1},
                    "threshold": {},
                    "importance": {},
                    "vocabulary_snapshot_sha256": "legacy-snapshot",
                    "personalized_vocabulary_sha256": hashlib.sha256(
                        export.encode("utf-8")
                    ).hexdigest(),
                },
            )

            validate_completed_workspace(workspace)
            validate_corpus_launch_workspace(workspace)

    def test_unloadable_calibration_outputs_do_not_block_recalibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
            write_json(analysis / "terminology-explanations.json", {})
            write_json(
                analysis / "host-review-summary.json",
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "review_passes": 1,
                    "terminology_candidate_count": 0,
                    "selected_terminology_count": 0,
                },
            )
            calibration_paths = (
                analysis / "vocabulary-calibration-session.json",
                analysis / "vocabulary-calibration-result.json",
                analysis / "personalized-vocabulary.tsv",
            )
            calibration_paths[0].write_text("not json", encoding="utf-8")
            calibration_paths[1].write_text("{}", encoding="utf-8")
            calibration_paths[2].write_text(
                "lemma\tclassification\nmodel\tlikely_unknown\n",
                encoding="utf-8",
            )

            validate_corpus_launch_workspace(workspace)

            self.assertTrue(all(path.exists() for path in calibration_paths))

    def test_completed_workspace_rejects_review_count_that_disagrees_with_term_map(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
            write_json(analysis / "terminology-explanations.json", {})
            write_json(
                analysis / "host-review-summary.json",
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "review_passes": 1,
                    "terminology_candidate_count": 48,
                    "selected_terminology_count": 48,
                },
            )
            write_json(
                analysis / "vocabulary-calibration-session.json",
                {"answers": [{"lemma": f"word-{index}"} for index in range(30)]},
            )
            export = "lemma\tclassification\nmodel\tretained\n"
            (analysis / "personalized-vocabulary.tsv").write_text(export, encoding="utf-8")
            write_json(
                analysis / "vocabulary-calibration-result.json",
                {
                    "counts": {"total": 1},
                    "threshold": {},
                    "importance": {},
                    "vocabulary_snapshot_sha256": "snapshot",
                    "personalized_vocabulary_sha256": hashlib.sha256(
                        export.encode("utf-8")
                    ).hexdigest(),
                },
            )

            with self.assertRaisesRegex(ValueError, "review summary"):
                validate_completed_workspace(workspace)

    def test_library_server_preloads_terminology_before_serving_a_domain(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "registered-domain"
            catalog = Mock()
            store = Mock()
            store.terminology_catalog.return_value = catalog
            registration = APP.DomainRegistration(
                domain_id="registered-domain",
                display_name="Registered Domain",
                workspace=str(workspace),
                registered_at="2026-09-01T00:00:00Z",
            )
            with (
                patch.object(APP, "validate_completed_workspace"),
                patch.object(APP, "load_words", return_value=[]),
                patch.object(APP, "ContinuousStore", return_value=store),
                patch.object(APP, "RemoteCalibrationSession", return_value=Mock()),
            ):
                context = APP._workspace_context(
                    registration,
                    SimpleNamespace(exam_profile="cet6"),
                    None,
                    None,
                    Mock(),
                )

            self.assertIs(context.continuous_store, store)
            catalog.list_terms.assert_called_once_with()

    def test_mismatched_term_assets_do_not_count_as_finalized_terminology(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text(
                json.dumps(
                    {
                        "term": "unfinished term",
                        "host_review_classification": "domain-term",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps({"term": "unfinished term"}) + "\n",
                encoding="utf-8",
            )
            write_json(analysis / "terminology-explanations.json", {})
            write_json(
                analysis / "host-review-summary.json",
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "review_passes": 1,
                    "terminology_candidate_count": 1,
                    "selected_terminology_count": 1,
                },
            )

            with self.assertRaisesRegex(
                ValueError, "terminology and explanations disagree"
            ):
                validate_initialized_workspace(workspace)

    def test_empty_assets_without_review_summary_do_not_start_calibration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "first-terminology-map.jsonl").write_text(
                "", encoding="utf-8"
            )
            write_json(analysis / "terminology-explanations.json", {})

            with self.assertRaisesRegex(
                FileNotFoundError, "host-review-summary.json"
            ):
                validate_initialized_workspace(workspace)

    def test_finalizer_rejects_invalid_review_identity_before_writing(self) -> None:
        invalid_reviews = (
            {
                "schema_version": 999,
                "reviewer": "current-host-agent",
                "terminology": {},
                "terminology_explanations": {},
            },
            {
                "schema_version": 1,
                "reviewer": None,
                "terminology": {},
                "terminology_explanations": {},
            },
        )
        for review in invalid_reviews:
            with self.subTest(review=review), tempfile.TemporaryDirectory() as temporary:
                workspace = make_workspace(Path(temporary))
                analysis = workspace / "analysis"
                (analysis / "terminology-candidates.jsonl").write_text(
                    "", encoding="utf-8"
                )
                selection_path = analysis / "terminology-review-selection.json"
                write_json(selection_path, review)

                with self.assertRaises(ValueError):
                    finalize_review(workspace, selection_path)

                self.assertFalse(
                    (analysis / "first-terminology-map.jsonl").exists()
                )
                self.assertFalse(
                    (analysis / "terminology-explanations.json").exists()
                )

    def test_finalizer_rejects_selected_term_without_loadable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps(
                    {
                        "term": "unsupported term",
                        "source_papers": ["W1"],
                        "representative_sentences": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            selection_path = analysis / "terminology-review-selection.json"
            write_json(
                selection_path,
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "terminology": {"unsupported term": 1.0},
                    "terminology_explanations": {
                        "unsupported term": {
                            "meaning_en": "A term without displayable evidence.",
                            "meaning_zh": "一个没有可展示证据的术语。",
                            "concept_role": "Negative test fixture.",
                            "sense_key": "unsupported-term",
                        }
                    },
                },
            )

            with self.assertRaisesRegex(ValueError, "no loadable paper evidence"):
                finalize_review(workspace, selection_path)

            self.assertFalse((analysis / "first-terminology-map.jsonl").exists())
            self.assertFalse((analysis / "terminology-explanations.json").exists())

    def test_incomplete_explanation_never_writes_final_assets(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = make_workspace(Path(temporary))
            analysis = workspace / "analysis"
            (analysis / "terminology-candidates.jsonl").write_text(
                json.dumps({"term": "robust analysis"}) + "\n", encoding="utf-8"
            )
            selection_path = analysis / "terminology-review-selection.json"
            write_json(
                selection_path,
                {
                    "schema_version": 1,
                    "reviewer": "current-host-agent",
                    "terminology": {"robust analysis": 1.0},
                    "terminology_explanations": {
                        "robust analysis": {
                            "meaning_en": "Reliable analysis.",
                            "meaning_zh": "可靠分析。",
                            "concept_role": "Methodological criterion.",
                        }
                    },
                },
            )

            with self.assertRaisesRegex(ValueError, "explanation is incomplete"):
                finalize_review(workspace, selection_path)

            self.assertFalse((analysis / "first-terminology-map.jsonl").exists())
            self.assertFalse((analysis / "terminology-explanations.json").exists())


if __name__ == "__main__":
    unittest.main()
