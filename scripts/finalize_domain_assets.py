#!/usr/bin/env python3
"""Finalize vocabulary cards and terminology after orthography review."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from domain_registry import validate_initialized_workspace
from finalize_host_review import finalize_review as finalize_terminology
from orthography_contract import orthography_summary_is_complete
from researchramp_core import read_json, utc_now, write_json
from terminology_assets import load_finalized_terminology
from vocabulary_cards import GLOSS_DATA_NAME, build_catalog


def finalize_assets(workspace: Path, selection: Path) -> dict[str, object]:
    resolved = workspace.expanduser().resolve()
    analysis = resolved / "analysis"
    review = read_json(selection.expanduser().resolve())
    if review.get("schema_version") != 1:
        raise ValueError("Combined review must use schema_version 1")
    if review.get("reviewer") != "current-host-agent":
        raise ValueError("Combined review reviewer must be current-host-agent")

    orthography = read_json(analysis / "orthography-review-summary.json")
    if not orthography_summary_is_complete(orthography):
        raise ValueError("Vocabulary orthography review must be finalized first")
    corpus_stats = read_json(analysis / "corpus-stats.json")
    if (
        not isinstance(corpus_stats, dict)
        or corpus_stats.get("orthography_review_applied") is not True
    ):
        raise ValueError("Vocabulary orthography review must be applied first")
    terminology = finalize_terminology(resolved, selection)
    cards = build_catalog(
        resolved,
        selection,
        Path(__file__).resolve().parents[1] / "app" / "data" / GLOSS_DATA_NAME,
    )
    corpus_stats = read_json(analysis / "corpus-stats.json")
    vocabulary = {
        "vocabulary_entry_count": int(corpus_stats.get("vocabulary_entry_count") or 0),
        "replacement_count": int(orthography.get("replacement_count") or 0),
        "drop_count": int(orthography.get("drop_count") or 0),
        "card_review_drop_count": int(cards.get("drop_count") or 0),
    }
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
        "vocabulary_cards": cards,
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
