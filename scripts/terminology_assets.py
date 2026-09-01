"""One strict loader for finalized ResearchRamp terminology assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        values = [json.loads(line) for line in handle if line.strip()]
    if not all(isinstance(value, dict) for value in values):
        raise ValueError(f"Invalid finalized terminology in workspace: {path.parent.parent}")
    return values


def load_finalized_terminology(
    workspace: Path,
    *,
    require_review_summary: bool,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Load one internally consistent terminology snapshot.

    Completed legacy workspaces may lack a review summary. When a summary is
    present, however, its selected count is always authoritative and must agree
    with the finalized term rows. This prevents an old or empty term map from
    being served beside a newer non-zero review summary.
    """

    resolved = workspace.expanduser().resolve()
    analysis = resolved / "analysis"
    term_path = analysis / "first-terminology-map.jsonl"
    explanation_path = analysis / "terminology-explanations.json"
    summary_path = analysis / "host-review-summary.json"
    required = [term_path, explanation_path]
    if require_review_summary:
        required.append(summary_path)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "ResearchRamp initialization is incomplete; missing: " + ", ".join(missing)
        )

    try:
        terms = _read_jsonl(term_path)
        explanations = json.loads(explanation_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Invalid finalized terminology in workspace: {resolved}"
        ) from error
    if not isinstance(explanations, dict):
        raise ValueError(f"Invalid finalized terminology in workspace: {resolved}")

    selected_terms = [
        str(item.get("term") or "").strip().casefold()
        for item in terms
        if item.get("host_review_classification") == "domain-term"
    ]
    if len(selected_terms) != len(terms) or not all(selected_terms):
        raise ValueError(f"Invalid finalized terminology in workspace: {resolved}")
    if len(set(selected_terms)) != len(selected_terms):
        raise ValueError(f"Duplicate finalized terminology in workspace: {resolved}")
    explanation_terms = {str(term).strip().casefold() for term in explanations}
    if set(selected_terms) != explanation_terms:
        raise ValueError(
            f"Finalized terminology and explanations disagree in workspace: {resolved}"
        )
    for term, explanation in explanations.items():
        if not isinstance(explanation, dict) or not all(
            str(explanation.get(field) or "").strip()
            for field in ("meaning_en", "meaning_zh", "concept_role")
        ):
            raise ValueError(f"Finalized terminology explanation is incomplete: {term}")

    summary: dict[str, Any] | None = None
    if summary_path.is_file():
        try:
            loaded = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(
                f"Invalid terminology review summary in workspace: {resolved}"
            ) from error
        if not isinstance(loaded, dict):
            raise ValueError(
                f"Invalid terminology review summary in workspace: {resolved}"
            )
        summary = loaded
        candidate_count = summary.get("terminology_candidate_count")
        selected_count = summary.get("selected_terminology_count")
        if (
            isinstance(summary.get("schema_version"), bool)
            or summary.get("schema_version") != 1
            or summary.get("reviewer") != "current-host-agent"
            or isinstance(summary.get("review_passes"), bool)
            or summary.get("review_passes") != 1
            or isinstance(candidate_count, bool)
            or not isinstance(candidate_count, int)
            or candidate_count < len(terms)
            or isinstance(selected_count, bool)
            or not isinstance(selected_count, int)
            or selected_count != len(terms)
        ):
            raise ValueError(
                f"Invalid terminology review summary in workspace: {resolved}"
            )

    return terms, explanations, summary
