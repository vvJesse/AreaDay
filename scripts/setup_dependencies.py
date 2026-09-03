#!/usr/bin/env python3
"""One-command, isolated ResearchRamp runtime setup and verification."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
import hashlib
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_VENV_DIR = SKILL_DIR / ".venv"
DEFAULT_MODEL_DIR = Path.home() / ".researchramp" / "models" / "sentence-transformers"
OFFICIAL_HF_ENDPOINT = "https://huggingface.co"
CHINA_HF_MIRROR = "https://hf-mirror.com"
MODEL_MANIFEST = SKILL_DIR / "references" / "embedding-model-manifest.json"
REQUIREMENTS = SKILL_DIR / "requirements.txt"
NLP_VERIFIER = SKILL_DIR / "scripts" / "verify_nlp_runtime.py"
MINIMUM_PYTHON = (3, 10)
OFFICIAL_PYPI_INDEX = "https://pypi.org/simple"
CHINA_PYPI_MIRRORS = (
    "https://mirrors.aliyun.com/pypi/simple",
    "https://mirrors.cloud.tencent.com/pypi/simple",
    "https://pypi.tuna.tsinghua.edu.cn/simple",
)


def venv_python(venv_dir: Path, platform: str = sys.platform) -> Path:
    if platform == "win32":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def runtime_environment(model_dir: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment.setdefault("TOKENIZERS_PARALLELISM", "false")
    environment["ORT_DISABLE_TELEMETRY"] = "1"
    environment.setdefault("HF_HUB_DOWNLOAD_TIMEOUT", "120")
    environment.setdefault("HF_HUB_ETAG_TIMEOUT", "30")
    environment.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
    environment.setdefault("HF_HUB_VERBOSITY", "error")
    environment["RESEARCHRAMP_MODEL_DIR"] = str(model_dir)
    return environment


def run_with_retries(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
    attempts: int = 2,
) -> None:
    for attempt in range(1, attempts + 1):
        completed = subprocess.run(command, cwd=cwd, env=environment, check=False)
        if completed.returncode == 0:
            return
        if attempt < attempts:
            print(f"Command failed; retrying ({attempt + 1}/{attempts})...", flush=True)
            time.sleep(2)
    raise subprocess.CalledProcessError(completed.returncode, command)


def python_version(python: Path) -> tuple[int, int]:
    completed = subprocess.run(
        [
            str(python),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    major, minor = completed.stdout.strip().split(".")
    return int(major), int(minor)


def create_environment(venv_dir: Path, environment: dict[str, str]) -> None:
    uv = shutil.which("uv")
    if uv:
        print(f"Creating isolated environment with uv: {venv_dir}", flush=True)
        subprocess.run(
            [
                uv,
                "venv",
                "--managed-python",
                "--no-config",
                "--python",
                f"{sys.version_info.major}.{sys.version_info.minor}",
                str(venv_dir),
            ],
            cwd=SKILL_DIR,
            env=environment,
            check=True,
        )
        return

    print(f"Creating isolated environment with Python venv: {venv_dir}", flush=True)
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        cwd=SKILL_DIR,
        env=environment,
        check=True,
    )


def missing_pinned_requirements(venv_dir: Path) -> list[str]:
    """Return only top-level pins absent from, or mismatched in, the target venv."""

    requirements = [
        line.strip()
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    completed = subprocess.run(
        [
            str(venv_python(venv_dir)),
            "-c",
            (
                "import importlib.metadata as m, json; "
                "print(json.dumps({d.metadata['Name'].lower().replace('_', '-'): d.version "
                "for d in m.distributions() if d.metadata.get('Name')}))"
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    installed = json.loads(completed.stdout)
    missing: list[str] = []
    for requirement in requirements:
        if "==" not in requirement:
            missing.append(requirement)
            continue
        name, expected = requirement.split("==", 1)
        normalized = name.strip().lower().replace("_", "-")
        if installed.get(normalized) != expected.strip():
            missing.append(requirement)
    return missing


def install_packages(venv_dir: Path, environment: dict[str, str]) -> None:
    python = venv_python(venv_dir)
    requirements = missing_pinned_requirements(venv_dir)
    if not requirements:
        print("All pinned Python packages are already installed.", flush=True)
        return
    uv = shutil.which("uv")
    if uv:
        cache_dir = Path(environment.get("UV_CACHE_DIR", SKILL_DIR / ".cache" / "uv"))
        cache_dir.mkdir(parents=True, exist_ok=True)
        environment["UV_CACHE_DIR"] = str(cache_dir)
        base_command = [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            str(python),
            *requirements,
        ]
        index_flag = "--default-index"
    else:
        subprocess.run(
            [str(python), "-m", "pip", "install", "--upgrade", "pip"],
            cwd=SKILL_DIR,
            env=environment,
            check=True,
        )
        base_command = [
            str(python),
            "-m",
            "pip",
            "install",
            *requirements,
        ]
        index_flag = "--index-url"

    configured_index = environment.get("RESEARCHRAMP_PYPI_INDEX_URL")
    indexes = [configured_index, OFFICIAL_PYPI_INDEX, *CHINA_PYPI_MIRRORS]
    unique_indexes = list(dict.fromkeys(index for index in indexes if index))
    last_error: subprocess.CalledProcessError | None = None
    for index in unique_indexes:
        print(
            f"Installing {', '.join(requirements)} from {index}...",
            flush=True,
        )
        try:
            run_with_retries(
                [*base_command, index_flag, index],
                cwd=SKILL_DIR,
                environment=environment,
                attempts=1,
            )
            return
        except subprocess.CalledProcessError as error:
            last_error = error
            print("That package index failed; trying the next source...", flush=True)
    if last_error is not None:
        raise last_error
    raise RuntimeError("No Python package index is configured")


def ensure_spacy_model(venv_dir: Path, environment: dict[str, str]) -> None:
    python = venv_python(venv_dir)
    installed = subprocess.run(
        [
            str(python),
            "-c",
            "import en_core_web_sm",
        ],
        cwd=SKILL_DIR,
        env=environment,
        capture_output=True,
        check=False,
    ).returncode == 0
    if installed:
        print("spaCy English model is installed in the isolated environment.")
        return

    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    wheel = manifest["spacy_wheel"]
    cache_path = SKILL_DIR / ".cache" / "wheels" / wheel["filename"]
    configured_endpoint = os.environ.get("RESEARCHRAMP_MODEL_ENDPOINT") or manifest.get(
        "asset_endpoint"
    )
    urls = []
    if configured_endpoint:
        urls.append(
            f"{configured_endpoint.rstrip('/')}/{wheel['asset_path'].lstrip('/')}"
        )
    urls.append(wheel["official_url"])

    last_error: Exception | None = None
    for url in urls:
        try:
            if not cache_path.is_file() or file_sha256(cache_path) != wheel["sha256"]:
                print(f"Downloading the pinned spaCy English model from {url}", flush=True)
                download_verified_file(url, cache_path, wheel["sha256"])
            install_wheel(venv_dir, cache_path, environment)
            subprocess.run(
                [str(python), "-c", "import en_core_web_sm"],
                cwd=SKILL_DIR,
                env=environment,
                check=True,
            )
            print("spaCy English model is installed in the isolated environment.")
            return
        except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
            last_error = error
            print("That spaCy model source failed; trying the next source...", flush=True)
    raise RuntimeError(f"Could not install the spaCy English model: {last_error}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_verified_file(url: str, destination: Path, expected_hash: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_suffix(destination.suffix + ".part")
    request = urllib.request.Request(
        url, headers={"User-Agent": "ResearchRamp/0.1 (public asset installer)"}
    )
    try:
        digest = hashlib.sha256()
        with urllib.request.urlopen(request, timeout=120) as response:
            with staging.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    output.write(chunk)
        actual_hash = digest.hexdigest()
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Integrity check failed for {url}: expected {expected_hash}, got {actual_hash}"
            )
        staging.replace(destination)
    finally:
        staging.unlink(missing_ok=True)


def install_wheel(
    venv_dir: Path,
    wheel: Path,
    environment: dict[str, str],
) -> None:
    python = venv_python(venv_dir)
    uv = shutil.which("uv")
    if uv:
        command = [
            uv,
            "pip",
            "install",
            "--no-config",
            "--python",
            str(python),
            str(wheel),
        ]
    else:
        command = [str(python), "-m", "pip", "install", str(wheel)]
    subprocess.run(command, cwd=SKILL_DIR, env=environment, check=True)


def verify(
    venv_dir: Path,
    model_dir: Path,
    *,
    download_models: bool,
    endpoint: str | None = None,
) -> None:
    python = venv_python(venv_dir)
    mode = "--download-models" if download_models else "--offline"
    print(
        "Downloading and testing the embedding model..."
        if download_models
        else "Verifying the complete NLP runtime without model downloads...",
        flush=True,
    )
    environment = runtime_environment(model_dir)
    if endpoint:
        environment["RESEARCHRAMP_MODEL_ENDPOINT"] = endpoint
        if endpoint == CHINA_HF_MIRROR:
            environment.setdefault("HF_HUB_DISABLE_XET", "1")
    subprocess.run(
        [str(python), str(NLP_VERIFIER), mode, "--model-dir", str(model_dir)],
        cwd=SKILL_DIR,
        env=environment,
        check=True,
    )


def embedding_download_endpoints(
    asset_endpoint: str | None,
    configured_endpoint: str | None,
) -> list[str]:
    ordered = [
        asset_endpoint,
        configured_endpoint,
        OFFICIAL_HF_ENDPOINT,
        CHINA_HF_MIRROR,
    ]
    endpoints: list[str] = []
    for endpoint in ordered:
        if endpoint and endpoint not in endpoints:
            endpoints.append(endpoint)
    return endpoints


def download_and_verify_embedding_model(
    venv_dir: Path,
    model_dir: Path,
) -> None:
    manifest = json.loads(MODEL_MANIFEST.read_text(encoding="utf-8"))
    asset_endpoint = os.environ.get("RESEARCHRAMP_MODEL_ENDPOINT") or manifest.get(
        "asset_endpoint"
    )
    endpoints = embedding_download_endpoints(
        asset_endpoint,
        os.environ.get("HF_ENDPOINT"),
    )
    last_error: subprocess.CalledProcessError | None = None
    for index, endpoint in enumerate(endpoints):
        try:
            print(f"Trying embedding model source: {endpoint}", flush=True)
            verify(
                venv_dir,
                model_dir,
                download_models=True,
                endpoint=endpoint,
            )
            return
        except (subprocess.CalledProcessError, RuntimeError) as error:
            last_error = error
            if index + 1 < len(endpoints):
                print(
                    "That model source was unreachable; trying the next source "
                    "with the same pinned file hashes...",
                    flush=True,
                )
    assert last_error is not None
    raise last_error


def check(venv_dir: Path, model_dir: Path) -> bool:
    python = venv_python(venv_dir)
    if not python.exists():
        print("Missing local dependencies: isolated Python environment")
        return False
    missing = missing_pinned_requirements(venv_dir)
    if missing:
        print("Missing local dependencies: " + ", ".join(missing))
        return False
    try:
        version = python_version(python)
        if version < MINIMUM_PYTHON:
            print(f"Python {version[0]}.{version[1]} is too old; Python 3.10+ is required.")
            return False
        verify(venv_dir, model_dir, download_models=False)
    except (OSError, RuntimeError, subprocess.CalledProcessError):
        print("The local NLP runtime is incomplete or failed its inference check.")
        return False
    print("Python packages: verified")
    print(f"NLP models: {model_dir}")
    return True


def install(venv_dir: Path, model_dir: Path) -> None:
    if sys.version_info[:2] < MINIMUM_PYTHON:
        raise SystemExit(
            f"Python 3.10+ is required; this installer is running under {sys.version.split()[0]}."
        )
    environment = runtime_environment(model_dir)
    if not venv_python(venv_dir).exists():
        create_environment(venv_dir, environment)
    if python_version(venv_python(venv_dir)) < MINIMUM_PYTHON:
        raise SystemExit(
            f"The existing environment at {venv_dir} uses Python below 3.10. "
            "Choose a new --venv-dir or replace that environment."
        )
    install_packages(venv_dir, environment)
    ensure_spacy_model(venv_dir, environment)
    download_and_verify_embedding_model(venv_dir, model_dir)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install or verify AreaDay's isolated local runtime."
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install packages and models, then run real NLP inference.",
    )
    parser.add_argument("--venv-dir", type=Path, default=DEFAULT_VENV_DIR)
    parser.add_argument("--model-dir", type=Path, default=DEFAULT_MODEL_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    venv_dir = args.venv_dir.resolve()
    model_dir = args.model_dir.resolve()
    if args.install:
        try:
            install(venv_dir, model_dir)
        except subprocess.CalledProcessError as error:
            raise SystemExit(
                "AreaDay setup could not finish its networked installation. "
                "Check access to the configured Python package index and at least one "
                "configured static model source, then rerun the same command. Verified "
                "downloads are cached and will be reused. No Hugging Face token is required."
            ) from error
    return 0 if check(venv_dir, model_dir) else 1


if __name__ == "__main__":
    raise SystemExit(main())
