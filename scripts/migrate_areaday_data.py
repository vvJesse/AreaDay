#!/usr/bin/env python3
"""Migrate legacy Skill-local data into AreaDay's upgrade-safe data directory."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def areaday_data_root(platform_name: str | None = None) -> Path:
    override = os.environ.get("AREADAY_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    platform = (platform_name or sys.platform).lower()
    if platform in {"darwin", "mac", "macos"}:
        return Path.home() / "Library" / "Application Support" / "AreaDay" / "data"
    if platform in {"win32", "windows", "win"}:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise RuntimeError("Windows did not provide its local application-data directory.")
        return Path(local_app_data) / "AreaDay" / "data"
    raise RuntimeError("AreaDay supports macOS and Windows x64 only.")


def _legacy_candidates(skill_root: Path) -> tuple[Path, ...]:
    return (
        skill_root / "researchramp-data",
        skill_root.parent / "researchramp" / "researchramp-data",
        skill_root.parent / "ResearchRamp" / "researchramp-data",
    )


def migrate_areaday_data(skill_root: Path, destination: Path) -> dict[str, str]:
    skill_root = skill_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    registry = destination / "real-domains.json"
    if registry.is_file():
        return {"status": "areaday_data_ready", "data_directory": str(destination)}
    source = next(
        (
            candidate
            for candidate in _legacy_candidates(skill_root)
            if (candidate / "real-domains.json").is_file()
        ),
        None,
    )
    if source is None:
        destination.mkdir(parents=True, exist_ok=True)
        return {"status": "areaday_data_ready", "data_directory": str(destination)}
    if destination.exists() and any(destination.iterdir()):
        raise RuntimeError(
            "AreaDay data migration stopped because the destination already contains different data."
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.rmdir()
    shutil.copytree(source, destination, copy_function=shutil.copy2)
    return {
        "status": "legacy_data_migrated",
        "source": str(source),
        "data_directory": str(destination),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill-root", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    skill_root = args.skill_root or Path(__file__).resolve().parents[1]
    destination = args.destination or areaday_data_root()
    print(json.dumps(migrate_areaday_data(skill_root, destination), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
