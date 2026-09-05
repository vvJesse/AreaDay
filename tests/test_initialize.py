from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from initialize import InitializationController, InitializationError  # noqa: E402
from tests.test_initial_pipeline import valid_test_profile  # noqa: E402


def controller_args(root: Path) -> argparse.Namespace:
    profile = root / "profile.json"
    profile.write_text(json.dumps(valid_test_profile()), encoding="utf-8")
    return argparse.Namespace(
        command="run",
        profile=profile,
        workspace=root / "workspace",
        registry=root / "registry.json",
        target_papers=10,
        download_workers=2,
        download_workers_per_host=1,
        port=43131,
    )


class InitializationControllerTests(unittest.TestCase):
    def test_launch_verification_uses_the_selected_fallback_port(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            analysis = controller.workspace / "analysis"
            analysis.mkdir(parents=True)
            (analysis / "vocabulary-map.tsv").write_text(
                "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
                + "".join(
                    f"word{index}\tnoun\t1\t1\t1.0\n" for index in range(30)
                ),
                encoding="utf-8",
            )
            selected_port = controller.args.port + 1
            launch = {
                "status": "started",
                "instance_id": "fallback-instance",
                "domain_id": "test-domain",
                "port": selected_port,
                "url": (
                    f"http://127.0.0.1:{selected_port}/"
                    "?domain=test-domain#vocabulary"
                ),
            }
            app_state = {
                "domain_id": "test-domain",
                "calibration": {
                    "question_limit": 30,
                    "complete": False,
                    "word": {},
                    "answered": 0,
                },
                "terminology": {"count": 0, "terms": []},
            }
            terms_api = {"count": 0, "terms": []}
            registry = SimpleNamespace(
                register=lambda *_args, **_kwargs: SimpleNamespace(
                    domain_id="test-domain"
                )
            )
            identity_probe = SimpleNamespace(
                kind=SimpleNamespace(value="match"),
                identity={"instance_id": "fallback-instance"},
            )

            with (
                patch("initialize.DomainRegistry", return_value=registry),
                patch(
                    "initialize.launchable_registry_domain_ids",
                    return_value=("test-domain",),
                ),
                patch("initialize.ensure_workbench", return_value=launch),
                patch(
                    "initialize.probe_workbench",
                    return_value=identity_probe,
                ) as probe,
                patch(
                    "initialize._json_get",
                    side_effect=[app_state, terms_api],
                ) as json_get,
                patch(
                    "initialize.load_finalized_terminology",
                    return_value=(
                        [],
                        {},
                        {"selected_terminology_count": 0},
                    ),
                ),
            ):
                result = controller._launch_and_verify(
                    {"profile_id": "test-domain"}
                )

            self.assertEqual(result["port"], selected_port)
            self.assertEqual(probe.call_args.args[0], selected_port)
            self.assertEqual(
                [call.args[0] for call in json_get.call_args_list],
                [selected_port, selected_port],
            )

    def test_host_review_is_nonterminal_and_resumable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            controller.workspace.mkdir(parents=True)
            packet = controller.workspace / "candidate-review-packet.jsonl"
            packet.write_text('{"candidate_id":"W1"}\n', encoding="utf-8")
            selection = controller.workspace / "candidate-review-selection.json"

            payload = controller._host_action(
                "review_candidates",
                input_path=packet,
                output_path=selection,
                checkpoint="candidate_review_needed",
                instructions="Review and resume.",
            )

            self.assertFalse(payload["terminal"])
            self.assertEqual(payload["status"], "host_action_required")
            self.assertEqual(payload["next_action"]["actor"], "current_host_agent")
            self.assertEqual(payload["next_action"]["input"], str(packet))
            self.assertNotIn("input_sha256", payload["next_action"])

    def test_learning_asset_review_has_both_inputs_and_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            controller.workspace.mkdir(parents=True)
            terminology = controller.workspace / "terminology-review-input.json"
            vocabulary_batch = controller.workspace / "vocabulary-card-review-batch.json"
            terminology.write_text("{}\n", encoding="utf-8")
            vocabulary_batch.write_text("{}\n", encoding="utf-8")
            selection = controller.workspace / "selection.json"

            payload = controller._host_action(
                "review_vocabulary_cards_and_terminology",
                input_paths={
                    "terminology": terminology,
                    "vocabulary_card_review_batch": vocabulary_batch,
                },
                output_path=selection,
                checkpoint="learning_asset_review_needed",
                instructions="Review both and resume.",
            )

            self.assertEqual(
                payload["next_action"]["inputs"],
                {
                    "terminology": str(terminology),
                    "vocabulary_card_review_batch": str(vocabulary_batch),
                },
            )
            self.assertEqual(payload["next_action"]["output"], str(selection))

    def test_orthography_review_is_requested_before_card_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            analysis = controller.workspace / "analysis"
            analysis.mkdir(parents=True)
            (controller.workspace / "candidates.jsonl").write_text(
                '{"candidate_id":"W1"}\n', encoding="utf-8"
            )
            (controller.workspace / "candidate-review-packet.jsonl").write_text(
                '{"candidate_id":"W1"}\n', encoding="utf-8"
            )
            (controller.workspace / "candidate-review-selection.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (controller.workspace / "cold-start-summary.json").write_text(
                '{"corpus_is_usable":true}\n', encoding="utf-8"
            )
            (analysis / "orthography-review-input.json").write_text(
                '{"candidates":[{"observed_lemma":"whic"}]}\n',
                encoding="utf-8",
            )
            (analysis / "orthography-review-summary.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reviewer": "current-host-agent",
                        "reviewed_candidate_count": 1,
                        "replacement_count": 0,
                        "drop_count": 0,
                        "unchanged_candidate_count": 1,
                    }
                ),
                encoding="utf-8",
            )
            (analysis / "terminology-review-input.json").write_text(
                '{"candidates":[]}\n', encoding="utf-8"
            )

            with patch("initialize.prepare_review_input") as prepare_cards:
                with self.assertRaises(StopIteration):
                    controller._prepare_assets()

            prepare_cards.assert_not_called()
            status = json.loads(controller.status_path.read_text(encoding="utf-8"))
            self.assertEqual(
                status["next_action"]["type"], "review_vocabulary_orthography"
            )
            self.assertEqual(
                status["next_action"]["input"],
                str(analysis / "orthography-review-input.json"),
            )

    def test_completed_batch_with_a_drop_advances_to_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            analysis = controller.workspace / "analysis"
            analysis.mkdir(parents=True)
            (controller.workspace / "candidates.jsonl").write_text(
                '{"candidate_id":"W1"}\n', encoding="utf-8"
            )
            (controller.workspace / "candidate-review-packet.jsonl").write_text(
                '{"candidate_id":"W1"}\n', encoding="utf-8"
            )
            (controller.workspace / "candidate-review-selection.json").write_text(
                "{}\n", encoding="utf-8"
            )
            (controller.workspace / "cold-start-summary.json").write_text(
                '{"corpus_is_usable":true}\n', encoding="utf-8"
            )
            (analysis / "orthography-review-input.json").write_text(
                '{"candidates":[]}\n', encoding="utf-8"
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
            (analysis / "terminology-review-input.json").write_text(
                '{"candidates":[]}\n', encoding="utf-8"
            )
            candidates = [
                {
                    "observed_lemma": f"word{index}" if index < 6 else "geotagging",
                    "representative_sentences": [],
                    "dictionary_candidates": [],
                    "acronym_expansions": [],
                }
                for index in range(1, 7)
            ]
            batches = []
            for index, candidate in enumerate(candidates, start=1):
                path = analysis / f"batch-{index:03d}.json"
                path.write_text(
                    json.dumps({"candidate_count": 1, "candidates": [candidate]}),
                    encoding="utf-8",
                )
                batches.append(
                    {
                        "batch_index": index,
                        "candidate_count": 1,
                        "path": str(path),
                        "lemmas": [candidate["observed_lemma"]],
                    }
                )
            (analysis / "vocabulary-card-review-input.json").write_text(
                json.dumps(
                    {
                        "candidate_count": 6,
                        "batch_count": 6,
                        "candidates": candidates,
                        "batches": batches,
                    }
                ),
                encoding="utf-8",
            )
            (analysis / "domain-review-selection.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reviewer": "current-host-agent",
                        "terminology": {},
                        "terminology_explanations": {},
                        "vocabulary_card_review_schema_version": 3,
                        "vocabulary_card_glosses": {
                            f"word{index}": {
                                "meaning_zh": f"词{index}",
                                "sense_key": f"word-{index}",
                                "context_rationale": "Meaning is clear in context.",
                            }
                            for index in range(1, 6)
                        },
                        "vocabulary_card_drops": ["geotagging"],
                    }
                ),
                encoding="utf-8",
            )

            def complete_finalization(*_args, **_kwargs):
                (analysis / "domain-assets-summary.json").write_text(
                    json.dumps(
                        {
                            "ready_for_calibration": True,
                            "vocabulary_cards": {
                                "semantic_review_contract_version": 3
                            },
                        }
                    ),
                    encoding="utf-8",
                )

            with (
                patch("initialize.prepare_review_input"),
                patch.object(
                    controller, "_run_helper", side_effect=complete_finalization
                ) as run_helper,
            ):
                controller._prepare_assets()

            run_helper.assert_called_once()
            self.assertIn(
                "finalize_domain_assets.py",
                " ".join(str(value) for value in run_helper.call_args.args[0]),
            )

    def test_verified_service_is_the_only_successful_terminal_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = controller_args(root)
            controller = InitializationController(args)
            service = {
                "status": "started",
                "instance_id": "verified-instance",
                "domain_id": "test-domain",
                "url": "http://127.0.0.1:43131/?domain=test-domain#vocabulary",
                "vocabulary_ready": True,
                "terminology_ready": True,
            }
            with (
                patch.object(controller, "_prepare_assets"),
                patch("initialize.validate_initialized_workspace"),
                patch.object(controller, "_launch_and_verify", return_value=service),
            ):
                payload = controller.run()

            self.assertTrue(payload["terminal"])
            self.assertEqual(payload["status"], "awaiting_user_calibration")
            self.assertEqual(payload["checkpoint"], "calibration_service_ready")
            self.assertEqual(payload["next_action"]["actor"], "user")
            self.assertTrue(payload["service"]["vocabulary_ready"])
            self.assertTrue(payload["service"]["terminology_ready"])


if __name__ == "__main__":
    unittest.main()
