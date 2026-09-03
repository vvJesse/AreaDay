from __future__ import annotations

import hashlib
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from build_runtime import build_runtime, current_platform_id  # noqa: E402
from verify_nlp_runtime import ensure_model_files  # noqa: E402
from prepare_portable_runtime import SEALED_HOME, prepare_runtime, seal_runtime  # noqa: E402


class RuntimeBuildContractTests(unittest.TestCase):
    def test_portable_runtime_config_drops_build_machine_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            venv = Path(temporary) / "venv"
            base = venv / "base-python" / "bin"
            base.mkdir(parents=True)
            (base / "python3.12").write_bytes(b"python")
            (venv / "bin").mkdir()
            (venv / "pyvenv.cfg").write_text(
                "home = /runner/python/bin\ncommand = /runner/python -m venv\n",
                encoding="utf-8",
            )
            prepare_runtime(venv, "macos-arm64")
            self.assertEqual(
                (venv / "bin" / "python").readlink(),
                Path("../base-python/bin/python3.12"),
            )
            seal_runtime(venv)
            config = (venv / "pyvenv.cfg").read_text(encoding="utf-8")
            self.assertIn(f"home = {SEALED_HOME}", config)
            self.assertNotIn("/runner", config)

    def test_supported_native_platform_mapping_is_explicit(self) -> None:
        self.assertEqual(current_platform_id("darwin", "arm64"), "macos-arm64")
        self.assertEqual(current_platform_id("win32", "AMD64"), "windows-x64")
        with self.assertRaisesRegex(RuntimeError, "Unsupported"):
            current_platform_id("darwin", "x86_64")
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

    def test_frozen_runtime_inputs_and_two_runner_workflow_exist(self) -> None:
        self.assertTrue((ROOT / "uv.lock").is_file())
        lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
        self.assertIn("sha256:", lock)
        workflow = (ROOT / ".github" / "workflows" / "build-runtimes.yml").read_text(
            encoding="utf-8"
        )
        for platform in ("windows-x64", "macos-arm64"):
            self.assertIn(f"platform: {platform}", workflow)
        self.assertNotIn("platform: macos-x64", workflow)
        self.assertIn("--runtime-only", workflow)
        builder = (ROOT / "scripts" / "build_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("UV_TORCH_BACKEND", workflow)
        self.assertNotIn("UV_TORCH_BACKEND", builder)
        self.assertIn('"sync"', builder)
        self.assertIn('"--frozen"', builder)
        self.assertNotIn("--require-hashes", builder)
        self.assertIn('archive_environment["COPYFILE_DISABLE"] = "1"', builder)
        self.assertIn('"--norsrc"', builder)
        self.assertIn("areaday-runtime-proof-", builder)

    def test_embedding_download_uses_verified_streaming_payload(self) -> None:
        payload = b"pinned model payload"
        expected_hash = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "urllib.request.urlopen", return_value=io.BytesIO(payload)
        ) as request:
            target = Path(temporary)
            ensure_model_files(
                target,
                endpoint="https://models.example.test",
                repository="owner/model",
                revision="abc123",
                files={"model.bin": expected_hash},
                offline=False,
            )

            self.assertEqual((target / "model.bin").read_bytes(), payload)
            self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
