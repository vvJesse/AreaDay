from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "app" / "server.py"
SPEC = importlib.util.spec_from_file_location("local_core_boundary_app", APP_PATH)
assert SPEC is not None and SPEC.loader is not None
APP = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = APP
SPEC.loader.exec_module(APP)


class LocalCoreBoundaryTests(unittest.TestCase):
    def test_personal_vocabulary_prediction_core_is_not_shipped_locally(self) -> None:
        self.assertFalse(hasattr(APP, "CalibrationSession"))
        self.assertFalse(hasattr(APP, "frequency_prior_probability"))
        self.assertFalse(hasattr(APP, "THETA_GRID"))

    def test_local_word_records_contain_only_remote_input_features(self) -> None:
        self.assertEqual(
            set(APP.Word.__dataclass_fields__),
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
        self.assertIn(
            APP.DomainContext.__dataclass_fields__["session"].type,
            {object, "object"},
        )


if __name__ == "__main__":
    unittest.main()
