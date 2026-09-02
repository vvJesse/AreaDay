from __future__ import annotations

import hashlib
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

from remote_calibration import RemoteCalibrationSession  # noqa: E402


def word(index: int) -> SimpleNamespace:
    return SimpleNamespace(
        lemma=f"word{index}",
        part_of_speech="noun",
        total_count=4,
        document_count=2,
        document_share=0.02,
        zipf=3.0,
        cefr_level=None,
        exam_tags=(),
    )


def write_completed_result(root: Path, total: int = 30) -> tuple[Path, bytes, bytes]:
    state_path = root / "vocabulary-calibration-session.json"
    answers = [
        {"lemma": f"word{index}", "response": "known"} for index in range(30)
    ]
    state_path.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "remote_session_id": "11111111-1111-1111-1111-111111111111",
                "answers": answers,
            }
        ),
        encoding="utf-8",
    )
    export_path = root / "personalized-vocabulary.tsv"
    export_path.write_text(
        "lemma\tclassification\n"
        + "".join(f"word{index}\tlikely_known\n" for index in range(total)),
        encoding="utf-8",
    )
    result = {
        "counts": {
            "total": total,
            "likely_known": total,
            "uncertain": 0,
            "likely_unknown": 0,
            "remaining_after_conservative_exclusion": 0,
            "important_boundary_protected": 0,
        },
        "threshold": {"selected_percent": 90},
        "importance": {"tiers": []},
        "known_boundary": [],
        "remaining_boundary": [],
        "answers": answers,
        "vocabulary_snapshot_sha256": "original-server-snapshot",
        "personalized_vocabulary_sha256": hashlib.sha256(
            export_path.read_bytes()
        ).hexdigest(),
    }
    result_path = root / "vocabulary-calibration-result.json"
    result_path.write_text(json.dumps(result), encoding="utf-8")
    return state_path, result_path.read_bytes(), export_path.read_bytes()


class NoNetworkClient:
    def request(self, _action: str, _payload: dict) -> dict:
        raise AssertionError("a completed local result must not contact the server")


class VocabularyResultPersistenceTests(unittest.TestCase):
    def test_completed_result_is_frozen_and_remains_viewable_offline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path, result_before, export_before = write_completed_result(root)

            session = RemoteCalibrationSession(
                [word(index) for index in range(700)],
                state_path,
                "changed local corpus",
                client=NoNetworkClient(),
                license_path=root / "missing-license.rrlicense",
            )

            self.assertTrue(session.public_state()["complete"])
            self.assertEqual(session.public_state()["result"]["counts"]["total"], 30)
            self.assertEqual(session.result_path.read_bytes(), result_before)
            self.assertEqual(session.export_path.read_bytes(), export_before)

    def test_completed_result_with_missing_export_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state_path, result_before, _ = write_completed_result(root)
            (root / "personalized-vocabulary.tsv").unlink()

            with self.assertRaisesRegex(RuntimeError, "静默重算"):
                RemoteCalibrationSession(
                    [word(index) for index in range(30)],
                    state_path,
                    "damaged completed result",
                    client=NoNetworkClient(),
                    license_path=root / "missing-license.rrlicense",
                )

            self.assertEqual(
                (root / "vocabulary-calibration-result.json").read_bytes(),
                result_before,
            )


if __name__ == "__main__":
    unittest.main()
