from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_runtime import build_runtime, current_platform_id  # noqa: E402


class RuntimeBuildContractTests(unittest.TestCase):
    def test_supported_native_platform_mapping_is_explicit(self) -> None:
        self.assertEqual(current_platform_id("darwin", "arm64"), "macos-arm64")
        self.assertEqual(current_platform_id("darwin", "x86_64"), "macos-x64")
        self.assertEqual(current_platform_id("win32", "AMD64"), "windows-x64")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            current_platform_id("linux", "x86_64")

    def test_runtime_builder_rejects_unsafe_version_before_building(self) -> None:
        with self.assertRaisesRegex(ValueError, "MAJOR.MINOR.PATCH"):
            build_runtime(
                ROOT / "dist" / "invalid.zip",
                version="../../invalid",
                expected_platform="macos-arm64",
            )

    def test_runtime_dependencies_match_customer_requirements(self) -> None:
        requirements = {
            line.strip()
            for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        dependencies_match = re.search(
            r"dependencies\s*=\s*\[(.*?)\]", project, flags=re.DOTALL
        )
        self.assertIsNotNone(dependencies_match)
        dependencies = set(re.findall(r'"([^\"]+==[^\"]+)"', dependencies_match.group(1)))
        self.assertEqual(dependencies, requirements)

    def test_frozen_runtime_inputs_and_three_runner_workflow_exist(self) -> None:
        self.assertTrue((ROOT / "uv.lock").is_file())
        lock = (ROOT / "runtime-requirements.lock").read_text(encoding="utf-8")
        self.assertIn("--hash=sha256:", lock)
        workflow = (ROOT / ".github" / "workflows" / "build-runtimes.yml").read_text(
            encoding="utf-8"
        )
        for platform in ("windows-x64", "macos-arm64", "macos-x64"):
            self.assertIn(f"platform: {platform}", workflow)
        self.assertIn("--runtime-only", workflow)


if __name__ == "__main__":
    unittest.main()
