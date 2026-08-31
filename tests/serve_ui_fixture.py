#!/usr/bin/env python3
"""Serve two synthetic ResearchRamp domains for local UI forward-testing."""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "scripts"))

from domain_registry import DomainRegistry  # noqa: E402
from test_domain_isolation import APP, create_completed_workspace  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8876)
    args = parser.parse_args()

    fixture_root = Path(tempfile.mkdtemp(prefix="researchramp-ui-fixture-"))
    try:
        alpha = create_completed_workspace(fixture_root, "alpha", "Alpha Research")
        beta = create_completed_workspace(fixture_root, "beta", "Beta Research")
        registry_path = fixture_root / "library" / "domains.json"
        registry = DomainRegistry(registry_path)
        registry.register(alpha[0], display_name="Alpha Research", domain_id="alpha")
        registry.register(beta[0], display_name="Beta Research", domain_id="beta")
        print(f"Synthetic fixture root: {fixture_root}", flush=True)
        sys.argv = [
            str(ROOT / "app" / "server.py"),
            "--library",
            str(registry_path),
            "--domain",
            "alpha",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--no-browser",
        ]
        APP.main()
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


if __name__ == "__main__":
    main()
