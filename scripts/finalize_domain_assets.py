#!/usr/bin/env python3
"""Finalize vocabulary and terminology together before calibration starts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from apply_orthography_review import finalize_review as finalize_vocabulary
from domain_registry import validate_initialized_workspace
from finalize_host_review import finalize_review as finalize_terminology
from researchramp_core import read_json, utc_now, write_json
from terminology_assets import load_finalized_terminology


def finalize_assets(workspace: Path, selection: Path) -> dict[str, object]:
    resolved = workspace.expanduser().resolve()
    review = read_json(selection.expanduser().resolve())
    if review.get("schema_version") != 1:
        raise ValueError("Combined review must use schema_version 1")
    if review.get("reviewer") != "current-host-agent":
        raise ValueError("Combined review reviewer must be current-host-agent")

    vocabulary = finalize_vocabulary(resolved, selection)
    terminology = finalize_terminology(resolved, selection)
    validate_initialized_workspace(resolved)
    terms, _explanations, _summary = load_finalized_terminology(
        resolved,
        require_review_summary=True,
    )
    result: dict[str, object] = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "reviewer": "current-host-agent",
        "vocabulary": vocabulary,
        "terminology": {
            **terminology,
            "loadable_terminology_count": len(terms),
        },
        "ready_for_calibration": True,
    }
    write_json(resolved / "analysis" / "domain-assets-summary.json", result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(finalize_assets(args.workspace, args.selection), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
