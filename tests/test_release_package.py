from __future__ import annotations

import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_release import build_release  # noqa: E402


class ReleasePackageTests(unittest.TestCase):
    def test_release_is_an_areaday_skill_zip_with_an_explicit_safe_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "AreaDay-v1.0.0.zip"
            manifest = build_release(ROOT, output, version="1.0.0")

            self.assertEqual(manifest["artifact"], output.name)
            self.assertRegex(manifest["sha256"], r"^[0-9a-f]{64}$")
            self.assertGreater(manifest["files"], 20)
            with zipfile.ZipFile(output) as archive:
                names = set(archive.namelist())
                skill = archive.read("areaday/SKILL.md").decode("utf-8")
                metadata = archive.read("areaday/agents/openai.yaml").decode("utf-8")
                text_payload = "\n".join(
                    archive.read(name).decode("utf-8", errors="ignore")
                    for name in names
                    if not name.endswith((".gz", ".zip"))
                )

            self.assertIn("name: areaday", skill)
            self.assertIn('display_name: "AreaDay"', metadata)
            self.assertIn("areaday/scripts/install.sh", names)
            self.assertIn("areaday/scripts/install.ps1", names)
            self.assertIn("areaday/scripts/researchramp_license.py", names)
            self.assertFalse(any(".secrets" in name for name in names))
            self.assertFalse(any("issued/" in name for name in names))
            self.assertFalse(any("cloudflare/" in name for name in names))
            self.assertFalse(any("tests/" in name for name in names))
            self.assertNotIn("areaday/scripts/build_release.py", names)
            self.assertFalse(any("__pycache__" in name for name in names))
            self.assertFalse(any(name.endswith(".rrlicense") for name in names))
            self.assertNotIn("BEGIN PRIVATE KEY", text_payload)
            self.assertNotIn("license-dev.areaday.app", text_payload)
            self.assertNotIn("researchramp-development", text_payload)
            self.assertNotIn("cloudflare-development-2026-01", text_payload)

    def test_release_builder_refuses_a_non_semantic_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "version"):
                build_release(
                    ROOT,
                    Path(temporary) / "AreaDay-latest.zip",
                    version="latest",
                )


if __name__ == "__main__":
    unittest.main()
