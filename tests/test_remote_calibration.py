from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from remote_calibration import (  # noqa: E402
    CalibrationServiceError,
    RemoteCalibrationSession,
    serialize_word_statistics,
    vocabulary_snapshot_sha256,
)


def word(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        lemma=f"word{index}",
        part_of_speech="noun",
        total_count=20 + index,
        document_count=2 + index % 10,
        document_share=(2 + index % 10) / 70,
        zipf=3.5,
        cefr_level="B2" if index < 3 else None,
        exam_tags=("cet6",) if index < 5 else (),
        representative_sentences=["must stay local"],
        source_papers=["must-stay-local"],
        per_document_counts={"must-stay-local": 1},
    )


class FakeCalibrationClient:
    def __init__(self, responses: list[dict[str, object]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, object]]] = []

    def request(self, action: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((action, payload))
        return self.responses.pop(0)


def ready_response(
    snapshot: str,
    session_id: str = "11111111-1111-1111-1111-111111111111",
) -> dict[str, object]:
    return {
        "status": "calibration_ready",
        "session_id": session_id,
        "vocabulary_snapshot_sha256": snapshot,
        "calibration": {
            "answered": 0,
            "question_limit": 30,
            "complete": False,
            "mutation_revision": 0,
            "threshold": {
                "selected_percent": 90,
                "default_percent": 90,
                "minimum_percent": 75,
                "maximum_percent": 98,
                "step_percent": 1,
            },
            "responses": {"known": 0, "unknown": 0, "unsure": 0},
            "word": {"lemma": "word0", "part_of_speech": "noun"},
        },
    }


def complete_response(snapshot: str) -> dict[str, object]:
    rows = [
        {
            **serialize_word_statistics(word(index)),
            "frequency_prior_probability": 0.5,
            "cefr_adjustment": 0.0,
            "exam_adjustment": 0.0,
            "education_adjustment": 0.0,
            "prior_probability": 0.5,
            "probability_known": 0.25,
            "classification": "likely_unknown",
            "direct_response": "unknown" if index == 0 else None,
            "importance_tier": "B",
            "important_boundary_protected": False,
            "selected_threshold": 0.9,
        }
        for index in range(30)
    ]
    return {
        "status": "calibration_complete",
        "session_id": "11111111-1111-1111-1111-111111111111",
        "vocabulary_snapshot_sha256": snapshot,
        "calibration": {
            "answered": 30,
            "question_limit": 30,
            "complete": True,
            "mutation_revision": 0,
            "threshold": {
                "selected_percent": 90,
                "default_percent": 90,
                "minimum_percent": 75,
                "maximum_percent": 98,
                "step_percent": 1,
            },
            "responses": {"known": 20, "unknown": 10, "unsure": 0},
            "result": {
                "counts": {
                    "total": 30,
                    "likely_known": 0,
                    "uncertain": 0,
                    "likely_unknown": 30,
                    "important_boundary_protected": 0,
                    "remaining_after_conservative_exclusion": 30,
                },
                "threshold": {"selected_percent": 90},
                "importance": {"tiers": []},
                "known_boundary": [],
                "remaining_boundary": [],
                "answers": [
                    {
                        "lemma": f"word{index}",
                        "response": "unknown" if index % 3 == 0 else "known",
                    }
                    for index in range(30)
                ],
                "rows": rows,
            },
        },
    }


class RemoteCalibrationTests(unittest.TestCase):
    def test_only_compact_word_statistics_leave_the_computer(self) -> None:
        serialized = serialize_word_statistics(word(0))

        self.assertEqual(
            set(serialized),
            {
                "lemma",
                "part_of_speech",
                "total_count",
                "document_count",
                "document_share",
                "zipf",
                "cefr_level",
                "exam_tags",
            },
        )
        self.assertNotIn("representative_sentences", serialized)
        self.assertNotIn("source_papers", serialized)
        self.assertNotIn("per_document_counts", serialized)

    def test_snapshot_changes_when_any_allowed_prediction_statistic_changes(self) -> None:
        original = word(0)
        changed = word(0)
        changed.cefr_level = None
        changed.exam_tags = ()

        self.assertNotEqual(
            vocabulary_snapshot_sha256([original]),
            vocabulary_snapshot_sha256([changed]),
        )

    def test_new_session_uploads_statistics_and_persists_only_remote_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            license_path = root / "license.rrlicense"
            license_path.write_text(json.dumps({"format": "test-license"}), encoding="utf-8")
            words = [word(index) for index in range(30)]
            snapshot = vocabulary_snapshot_sha256(words)
            client = FakeCalibrationClient([ready_response(snapshot)])

            session = RemoteCalibrationSession(
                words,
                root / "session.json",
                "private local label",
                client=client,
                license_path=license_path,
            )

            action, payload = client.calls[0]
            self.assertEqual(action, "start")
            self.assertEqual(len(payload["words"]), 30)
            self.assertNotIn("corpus_label", payload)
            persisted = json.loads((root / "session.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["remote_session_id"], session.session_id)
            self.assertNotIn("words", persisted)

    def test_completed_server_result_is_atomically_saved_as_local_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            license_path = root / "license.rrlicense"
            license_path.write_text(json.dumps({"format": "test-license"}), encoding="utf-8")
            words = [word(index) for index in range(30)]
            snapshot = vocabulary_snapshot_sha256(words)
            client = FakeCalibrationClient(
                [ready_response(snapshot), complete_response(snapshot)]
            )
            session = RemoteCalibrationSession(
                words,
                root / "session.json",
                "local label",
                client=client,
                license_path=license_path,
            )

            session.answer("word0", "unknown")

            result = json.loads(session.result_path.read_text(encoding="utf-8"))
            export = session.export_path.read_text(encoding="utf-8")
            state = json.loads(session.state_path.read_text(encoding="utf-8"))
            self.assertEqual(result["counts"]["total"], 30)
            self.assertEqual(len(state["answers"]), 30)
            self.assertIn("word0\tnoun\t0.250000\tlikely_unknown", export)
            self.assertEqual(session.public_state()["complete"], True)

    def test_remote_failure_is_not_reported_as_an_invalid_license(self) -> None:
        error = CalibrationServiceError(
            "calibration_service_unavailable",
            "The vocabulary prediction service could not be reached.",
        )
        self.assertNotIn("license_invalid", error.code)


if __name__ == "__main__":
    unittest.main()
