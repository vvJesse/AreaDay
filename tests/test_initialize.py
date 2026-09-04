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

    def test_combined_domain_review_has_both_inputs_and_one_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            controller = InitializationController(controller_args(root))
            controller.workspace.mkdir(parents=True)
            vocabulary = controller.workspace / "orthography-review-input.json"
            terminology = controller.workspace / "terminology-review-input.json"
            vocabulary.write_text("{}\n", encoding="utf-8")
            terminology.write_text("{}\n", encoding="utf-8")
            selection = controller.workspace / "selection.json"

            payload = controller._host_action(
                "review_vocabulary_and_terminology",
                input_paths={"vocabulary": vocabulary, "terminology": terminology},
                output_path=selection,
                checkpoint="domain_review_needed",
                instructions="Review both and resume.",
            )

            self.assertEqual(
                payload["next_action"]["inputs"],
                {"vocabulary": str(vocabulary), "terminology": str(terminology)},
            )
            self.assertEqual(payload["next_action"]["output"], str(selection))

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
