#!/usr/bin/env python3
"""Build a deterministic, allowlisted AreaDay Skill release ZIP."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
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


def build_release(skill_root: Path, output: Path, *, version: str) -> dict[str, object]:
    if VERSION_PATTERN.fullmatch(version) is None:
        raise ValueError("version must use MAJOR.MINOR.PATCH")
    root = skill_root.resolve()
    output = output.resolve()
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
        }
        _write_entry(
            archive,
            f"{RELEASE_ROOT}/release.json",
            (json.dumps(release, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            0o644,
        )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return {
        "artifact": output.name,
        "files": len(files) + 1,
        "sha256": digest,
        "version": version,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "dist" / f"AreaDay-v{args.version}.zip"
    print(json.dumps(build_release(root, output, version=args.version), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
