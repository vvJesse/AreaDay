from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from prepare_release_assets import PLATFORMS, prepare  # noqa: E402


class PublishReleaseTests(unittest.TestCase):
    def _delivery(self, root: Path, platform: str, version: str = "1.0.4") -> Path:
        artifact = root / platform / f"AreaDay-{platform}-v{version}.zip"
        artifact.parent.mkdir(parents=True)
        runtime = f"AreaDay-runtime-{platform}-v{version}.zip"
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("INSTALL.md", "install")
            archive.writestr("areaday/SKILL.md", "skill")
            archive.writestr(
                "areaday/release.json",
                json.dumps(
                    {
                        "product": "areaday",
                        "version": version,
                        "platform": platform,
                        "runtime_artifact": runtime,
                    }
                ),
            )
            archive.writestr(f"areaday/runtime-packs/{runtime}", b"runtime")
        return artifact

    def test_prepare_selects_only_customer_zips_and_writes_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            incoming = root / "incoming"
            for platform in PLATFORMS:
                self._delivery(incoming, platform)
            output = root / "release"
            staged = prepare(incoming, output, "1.0.4")
            self.assertEqual(len(staged), 3)
            self.assertEqual(
                {path.name for path in staged},
                {
                    "AreaDay-windows-x64-v1.0.4.zip",
                    "AreaDay-macos-arm64-v1.0.4.zip",
                    "SHA256SUMS.txt",
                },
            )
            self.assertEqual(
                len((output / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()),
                2,
            )

    def test_prepare_rejects_a_missing_platform(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._delivery(root / "incoming", "macos-arm64")
            with self.assertRaisesRegex(RuntimeError, "windows-x64"):
                prepare(root / "incoming", root / "release", "1.0.4")


if __name__ == "__main__":
    unittest.main()
