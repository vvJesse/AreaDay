#!/usr/bin/env python3
"""Build a deterministic, allowlisted AreaDay Skill release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import zipfile
from pathlib import Path


RELEASE_ROOT = "areaday"
ALLOWED_ROOT_FILES = {"SKILL.md", "requirements.txt"}
ALLOWED_DIRECTORIES = {"agents", "app", "assets", "references", "scripts"}
ALLOWED_SUFFIXES = {
    ".css",
    ".gz",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".sh",
    ".tsv",
    ".txt",
    ".yaml",
    ".yml",
}
EXCLUDED_NAMES = {
    ".DS_Store",
    "build_release.py",
    "session.json",
}
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
PLATFORMS = {"windows-x64", "macos-arm64"}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_files(skill_root: Path) -> list[Path]:
    files: list[Path] = []
    for name in sorted(ALLOWED_ROOT_FILES):
        candidate = skill_root / name
        if not candidate.is_file():
            raise FileNotFoundError(f"Required release file is missing: {candidate}")
        files.append(candidate)
    for directory_name in sorted(ALLOWED_DIRECTORIES):
        directory = skill_root / directory_name
        if not directory.is_dir():
            raise FileNotFoundError(f"Required release directory is missing: {directory}")
        for candidate in sorted(directory.rglob("*")):
            relative = candidate.relative_to(skill_root)
            if (
                not candidate.is_file()
                or any(part.startswith(".") or part == "__pycache__" for part in relative.parts)
                or candidate.name in EXCLUDED_NAMES
                or candidate.suffix.lower() not in ALLOWED_SUFFIXES
            ):
                continue
            files.append(candidate)
    return files


def _write_entry(archive: zipfile.ZipFile, name: str, payload: bytes, mode: int) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    archive.writestr(info, payload)


def _write_file_entry(
    archive: zipfile.ZipFile,
    name: str,
    source: Path,
    mode: int,
) -> None:
    info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = (mode & 0xFFFF) << 16
    with source.open("rb") as source_handle, archive.open(info, "w") as archive_handle:
        shutil.copyfileobj(source_handle, archive_handle, length=1024 * 1024)


def _runtime_manifest(runtime_archive: Path) -> dict[str, object]:
    with zipfile.ZipFile(runtime_archive) as archive:
        try:
            payload = archive.read("runtime/runtime.json")
        except KeyError as error:
            raise ValueError("runtime archive is missing runtime/runtime.json") from error
    manifest = json.loads(payload.decode("utf-8"))
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("product") != "areaday"
    ):
        raise ValueError("runtime archive has an unsupported manifest")
    return manifest


def build_release(
    skill_root: Path,
    output: Path,
    *,
    version: str,
    runtime_archive: Path | None = None,
    platform: str | None = None,
) -> dict[str, object]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    if (runtime_archive is None) != (platform is None):
        raise ValueError("runtime_archive and platform must be provided together")
    if platform is not None and platform not in PLATFORMS:
        raise ValueError(f"unsupported release platform: {platform}")
    root = skill_root.resolve()
    output = output.resolve()
    selected_runtime = runtime_archive.resolve() if runtime_archive is not None else None
    runtime_manifest = None
    if selected_runtime is not None:
        runtime_manifest = _runtime_manifest(selected_runtime)
        if runtime_manifest.get("platform") != platform:
            raise ValueError("runtime archive platform does not match the release platform")
        if runtime_manifest.get("runtime_version") != version:
            raise ValueError("runtime archive version does not match the release version")
    output.parent.mkdir(parents=True, exist_ok=True)
    files = _release_files(root)
    with zipfile.ZipFile(output, "w") as archive:
        for source in files:
            relative = source.relative_to(root).as_posix()
            mode = 0o755 if source.suffix in {".sh", ".py"} else 0o644
            _write_entry(
                archive,
                f"{RELEASE_ROOT}/{relative}",
                source.read_bytes(),
                mode,
            )
        release = {
            "brand": "AreaDay",
            "product": "areaday",
            "version": version,
            **({"platform": platform} if platform is not None else {}),
            **(
                {
                    "runtime_artifact": selected_runtime.name,
                    "runtime_sha256": _sha256_file(selected_runtime),
                }
                if selected_runtime is not None
                else {}
            ),
        }
        _write_entry(
            archive,
            f"{RELEASE_ROOT}/release.json",
            (json.dumps(release, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o644,
        )
        if selected_runtime is not None:
            _write_file_entry(
                archive,
                f"{RELEASE_ROOT}/runtime-packs/{selected_runtime.name}",
                selected_runtime,
                0o644,
            )
    digest = _sha256_file(output)
    return {
        "artifact": output.name,
        "files": len(files) + 1 + (1 if selected_runtime is not None else 0),
        "sha256": digest,
        "version": version,
        **({"platform": platform} if platform is not None else {}),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--runtime-archive", type=Path)
    parser.add_argument("--platform", choices=sorted(PLATFORMS))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    default_name = (
        f"AreaDay-{args.platform}-v{args.version}.zip"
        if args.platform
        else f"AreaDay-v{args.version}.zip"
    )
    output = args.output or root / "dist" / default_name
    print(
        json.dumps(
            build_release(
                root,
                output,
                version=args.version,
                runtime_archive=args.runtime_archive,
                platform=args.platform,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
