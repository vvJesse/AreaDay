#!/usr/bin/env python3
"""Point an extracted AreaDay virtual environment at its bundled Python."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


SEALED_HOME = "__AREADAY_PORTABLE_PYTHON_HOME__"


def bundled_python(venv: Path, platform_name: str | None = None) -> Path:
    selected = platform_name or os.name
    if selected in {"nt", "windows-x64"}:
        return venv / "base-python" / "python.exe"
    return venv / "base-python" / "bin" / "python3.12"


def _write_config(venv: Path, home: str, executable: str | None) -> None:
    config = venv / "pyvenv.cfg"
    lines = config.read_text(encoding="utf-8").splitlines()
    retained = [
        line
        for line in lines
        if not line.startswith(("home = ", "executable = ", "command = "))
    ]
    updated = [f"home = {home}"]
    if executable is not None:
        updated.append(f"executable = {executable}")
    updated.extend(retained)
    config.write_text("\n".join(updated) + "\n", encoding="utf-8")


def prepare_runtime(venv: Path, platform_name: str | None = None) -> Path:
    """Make the environment runnable at its current absolute location."""

    selected = platform_name or os.name
    python = bundled_python(venv, selected)
    if not python.is_file():
        raise RuntimeError(f"Bundled Python is missing: {python}")

    if selected not in {"nt", "windows-x64"}:
        launcher = venv / "bin" / "python"
        if launcher.exists() or launcher.is_symlink():
            launcher.unlink()
        launcher.symlink_to("../base-python/bin/python3.12")

    _write_config(venv, str(python.parent), str(python))
    return python


def seal_runtime(venv: Path) -> None:
    """Remove build-machine paths before archiving the environment."""

    _write_config(venv, SEALED_HOME, None)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venv-dir", required=True, type=Path)
    args = parser.parse_args()
    prepare_runtime(args.venv_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

