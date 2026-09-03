#!/usr/bin/env python3
"""Validate delivery ZIPs from a successful run and stage GitHub Release assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path


PLATFORMS = ("windows-x64", "macos-arm64")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare(input_root: Path, output: Path, version: str) -> list[Path]:
    output.mkdir(parents=True, exist_ok=False)
    staged: list[Path] = []
    checksums: list[str] = []
    for platform in PLATFORMS:
        filename = f"AreaDay-{platform}-v{version}.zip"
        matches = list(input_root.rglob(filename))
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one {filename}, found {len(matches)}")
        source = matches[0]
        with zipfile.ZipFile(source) as archive:
            names = set(archive.namelist())
            required = {"INSTALL.md", "areaday/SKILL.md", "areaday/release.json"}
            if not required.issubset(names):
                raise RuntimeError(f"{filename} is missing its installation entry points")
            manifest = json.loads(archive.read("areaday/release.json"))
            if (
                manifest.get("product") != "areaday"
                or manifest.get("version") != version
                or manifest.get("platform") != platform
            ):
                raise RuntimeError(f"{filename} has mismatched release metadata")
            runtime_name = manifest.get("runtime_artifact")
            if f"areaday/runtime-packs/{runtime_name}" not in names:
                raise RuntimeError(f"{filename} does not contain its declared Runtime")
        destination = output / filename
        shutil.copy2(source, destination)
        checksums.append(f"{sha256(destination)}  {filename}")
        staged.append(destination)

    checksum_file = output / "SHA256SUMS.txt"
    checksum_file.write_text("\n".join(checksums) + "\n", encoding="ascii")
    staged.append(checksum_file)
    return staged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    for path in prepare(args.input, args.output, args.version):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
