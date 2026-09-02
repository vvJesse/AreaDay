#!/usr/bin/env python3
"""Build one relocatable, platform-native AreaDay runtime archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCKFILE = ROOT / "uv.lock"
REQUIREMENTS = ROOT / "requirements.txt"
MODEL_MANIFEST = ROOT / "references" / "embedding-model-manifest.json"
SETUP_SCRIPT = ROOT / "scripts" / "setup_dependencies.py"
RUNTIME_SCHEMA = 1
PYTHON_REQUEST = "3.12"
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PLATFORM_IDS = {
    ("darwin", "arm64"): "macos-arm64",
    ("darwin", "aarch64"): "macos-arm64",
    ("darwin", "x86_64"): "macos-x64",
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_platform_id(
    system: str | None = None,
    machine: str | None = None,
) -> str:
    selected_system = (system or sys.platform).lower()
    selected_machine = (machine or platform.machine()).lower()
    try:
        return PLATFORM_IDS[(selected_system, selected_machine)]
    except KeyError as error:
        raise RuntimeError(
            f"Unsupported runtime build platform: {selected_system}/{selected_machine}"
        ) from error


def runtime_python(venv: Path, platform_id: str) -> Path:
    if platform_id == "windows-x64":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def run(
    command: list[str],
    *,
    environment: dict[str, str],
    capture: bool = False,
) -> str:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=True,
        text=True,
        capture_output=capture,
    )
    return completed.stdout.strip() if capture else ""


def installed_packages(python: Path, environment: dict[str, str]) -> list[dict[str, str]]:
    payload = run(
        [
            str(python),
            "-c",
            (
                "import importlib.metadata as m,json;"
                "print(json.dumps(sorted(({'name':d.metadata['Name'],'version':d.version} "
                "for d in m.distributions() if d.metadata.get('Name')),"
                "key=lambda x:x['name'].lower())))"
            ),
        ],
        environment=environment,
        capture=True,
    )
    return json.loads(payload)


def write_zip_tree(source: Path, output: Path) -> None:
    """Write a stable ZIP on Windows, where the runtime contains no symlinks."""

    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source.parent).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(relative.rstrip("/") + "/", (2026, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = (0o755 | stat.S_IFDIR) << 16
                archive.writestr(info, b"")
                continue
            if path.is_symlink():
                raise RuntimeError(f"Windows runtime unexpectedly contains a symlink: {path}")
            info = zipfile.ZipInfo(relative, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (path.stat().st_mode & 0xFFFF) << 16
            with path.open("rb") as source_handle, archive.open(info, "w") as archive_handle:
                shutil.copyfileobj(source_handle, archive_handle, length=1024 * 1024)


def archive_runtime(source: Path, output: Path, platform_id: str) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    if platform_id.startswith("macos-"):
        # ditto preserves the symlinks used by a macOS Python environment.
        archive_environment = os.environ.copy()
        archive_environment["COPYFILE_DISABLE"] = "1"
        subprocess.run(
            [
                "ditto",
                "-c",
                "-k",
                "--norsrc",
                "--keepParent",
                str(source),
                str(output),
            ],
            env=archive_environment,
            check=True,
        )
    else:
        write_zip_tree(source, output)


def build_runtime(output: Path, *, version: str, expected_platform: str) -> dict[str, object]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    actual_platform = current_platform_id()
    if actual_platform != expected_platform:
        raise RuntimeError(
            f"This runner is {actual_platform}, not the requested {expected_platform}."
        )
    if not LOCKFILE.is_file():
        raise RuntimeError("uv.lock is missing; regenerate the frozen lock first.")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to build an AreaDay runtime.")

    environment = os.environ.copy()
    environment["UV_MANAGED_PYTHON"] = "1"
    environment["UV_NO_CONFIG"] = "1"
    environment.setdefault("TOKENIZERS_PARALLELISM", "false")

    with tempfile.TemporaryDirectory(prefix="areaday-runtime-build-") as temporary:
        work = Path(temporary)
        runtime = work / "runtime"
        venv = runtime / "venv"
        models = runtime / "models" / "sentence-transformers"
        environment.setdefault("UV_PYTHON_INSTALL_DIR", str(work / "managed-python"))
        environment.setdefault("UV_CACHE_DIR", str(work / "uv-cache"))

        run([uv, "python", "install", "--managed-python", PYTHON_REQUEST], environment=environment)
        run(
            [
                uv,
                "venv",
                "--managed-python",
                "--relocatable",
                "--python",
                PYTHON_REQUEST,
                str(venv),
            ],
            environment=environment,
        )
        python = runtime_python(venv, expected_platform)
        environment["UV_PROJECT_ENVIRONMENT"] = str(venv)
        run(
            [
                uv,
                "sync",
                "--frozen",
                "--no-config",
                "--no-dev",
                "--no-install-project",
            ],
            environment=environment,
        )
        run(
            [
                str(python),
                str(SETUP_SCRIPT),
                "--install",
                "--venv-dir",
                str(venv),
                "--model-dir",
                str(models),
            ],
            environment=environment,
        )

        model_manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
        packages = installed_packages(python, environment)
        python_version = run(
            [str(python), "-c", "import platform; print(platform.python_version())"],
            environment=environment,
            capture=True,
        )
        uv_version = run([uv, "--version"], environment=environment, capture=True)
        manifest = {
            "schema_version": RUNTIME_SCHEMA,
            "brand": "AreaDay",
            "product": "areaday",
            "runtime_version": version,
            "platform": expected_platform,
            "python_version": python_version,
            "uv_version": uv_version,
            "requirements_sha256": sha256(REQUIREMENTS),
            "lock_sha256": sha256(LOCKFILE),
            "model_manifest_sha256": sha256(MODEL_MANIFEST),
            "embedding_repository": model_manifest["repository"],
            "embedding_revision": model_manifest["revision"],
        }
        (runtime / "runtime.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        (runtime / "packages.json").write_text(
            json.dumps(packages, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        # Prove the environment still works after its containing directory moves.
        relocated_parent = work / "relocated"
        relocated_parent.mkdir()
        relocated = relocated_parent / "runtime"
        runtime.rename(relocated)
        relocated_python = runtime_python(relocated / "venv", expected_platform)
        run(
            [
                str(relocated_python),
                str(SETUP_SCRIPT),
                "--venv-dir",
                str(relocated / "venv"),
                "--model-dir",
                str(relocated / "models" / "sentence-transformers"),
            ],
            environment=environment,
        )
        archive_runtime(relocated, output.resolve(), expected_platform)

    return {
        "status": "runtime_built",
        "artifact": output.name,
        "platform": expected_platform,
        "runtime_version": version,
        "bytes": output.stat().st_size,
        "sha256": sha256(output),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--platform", required=True, choices=sorted(set(PLATFORM_IDS.values())))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_runtime(args.output, version=args.version, expected_platform=args.platform)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
