from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from migrate_areaday_data import migrate_areaday_data  # noqa: E402
from domain_registry import default_registry_path  # noqa: E402


class AreaDayDataMigrationTests(unittest.TestCase):
    def test_registry_uses_stable_areaday_data_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            selected = Path(temporary) / "AreaDayData"
            with patch.dict(os.environ, {"AREADAY_DATA_DIR": str(selected)}):
                self.assertEqual(
                    default_registry_path(),
                    selected.resolve() / "real-domains.json",
                )

    def test_legacy_skill_data_is_copied_once_without_deleting_the_source(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "areaday"
            legacy = root / "researchramp" / "researchramp-data"
            destination = root / "application-data"
            skill_root.mkdir()
            legacy.mkdir(parents=True)
            registry = {"schema_version": 1, "active_domain_id": "alpha", "domains": []}
            (legacy / "real-domains.json").write_text(json.dumps(registry), encoding="utf-8")
            (legacy / "global-learning.json").write_text("{}", encoding="utf-8")

            first = migrate_areaday_data(skill_root, destination)
            second = migrate_areaday_data(skill_root, destination)

            self.assertEqual(first["status"], "legacy_data_migrated")
            self.assertEqual(second["status"], "areaday_data_ready")
            self.assertEqual(
                json.loads((destination / "real-domains.json").read_text(encoding="utf-8")),
                registry,
            )
            self.assertTrue((destination / "global-learning.json").is_file())
            self.assertTrue((legacy / "real-domains.json").is_file())

    def test_existing_areaday_registry_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill_root = root / "areaday"
            legacy = root / "researchramp" / "researchramp-data"
            destination = root / "application-data"
            skill_root.mkdir()
            legacy.mkdir(parents=True)
            destination.mkdir()
            (legacy / "real-domains.json").write_text('{"legacy":true}', encoding="utf-8")
            (destination / "real-domains.json").write_text('{"current":true}', encoding="utf-8")

            result = migrate_areaday_data(skill_root, destination)

            self.assertEqual(result["status"], "areaday_data_ready")
            self.assertEqual(
                (destination / "real-domains.json").read_text(encoding="utf-8"),
                '{"current":true}',
            )


if __name__ == "__main__":
    unittest.main()
