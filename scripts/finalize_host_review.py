#!/usr/bin/env python3
"""Validate and persist a host agent's one-pass terminology review."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from researchramp_core import read_json, utc_now, write_json, write_jsonl


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_flat_tsv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t")
        writer.writeheader()
        for item in rows:
            row = {field: item.get(field) for field in fields}
            for field, value in row.items():
                if isinstance(value, (list, dict)):
                    row[field] = json.dumps(
                        value, ensure_ascii=False, separators=(",", ":")
                    )
            writer.writerow(row)
    temporary.replace(path)


def merge_terminology_selection(
    candidates: list[dict[str, Any]], selections: Any
) -> list[dict[str, Any]]:
    if not isinstance(selections, dict):
        raise ValueError("Host review must include a terminology object")

    candidate_by_term = {str(item["term"]): item for item in candidates}
    missing = sorted(set(selections) - set(candidate_by_term))
    if missing:
        raise ValueError(f"Host review contains unknown term values: {missing}")

    confidences: dict[str, float] = {}
    for term, raw_confidence in selections.items():
        if isinstance(raw_confidence, bool):
            raise ValueError(f"Terminology confidence must be numeric: {term}")
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Terminology confidence must be numeric: {term}"
            ) from error
        if not 0.0 <= confidence <= 1.0:
            raise ValueError(f"Terminology confidence must be between 0 and 1: {term}")
        confidences[str(term)] = confidence

    selected: list[dict[str, Any]] = []
    for candidate in candidates:
        term = str(candidate["term"])
        if term not in confidences:
            continue
        record = dict(candidate)
        record["host_review_classification"] = "domain-term"
        record["host_review_confidence"] = confidences[term]
        selected.append(record)
    return selected


def normalize_explanations(
    selected_terminology: list[dict[str, Any]], explanations: Any
) -> dict[str, dict[str, str]]:
    if not isinstance(explanations, dict):
        raise ValueError("Host review must include terminology_explanations")

    selected_terms = {
        str(item["term"]).strip().casefold() for item in selected_terminology
    }
    explanation_terms = {str(term).strip().casefold() for term in explanations}
    if selected_terms != explanation_terms:
        raise ValueError(
            "terminology_explanations must contain exactly the selected terminology entries"
        )

    normalized: dict[str, dict[str, str]] = {}
    for raw_term, raw_explanation in explanations.items():
        term = str(raw_term).strip().casefold()
        if not isinstance(raw_explanation, dict):
            raise ValueError(f"terminology explanation must be an object: {term}")
        record = {
            "meaning_en": str(raw_explanation.get("meaning_en") or "").strip(),
            "meaning_zh": str(raw_explanation.get("meaning_zh") or "").strip(),
            "concept_role": str(raw_explanation.get("concept_role") or "").strip(),
            "sense_key": str(raw_explanation.get("sense_key") or "").strip(),
        }
        if not all(record.values()):
            raise ValueError(f"terminology explanation is incomplete: {term}")
        normalized[term] = record
    return normalized


def validate_review_identity(selection: Any) -> str:
    if (
        not isinstance(selection, dict)
        or isinstance(selection.get("schema_version"), bool)
        or selection.get("schema_version") != 1
    ):
        raise ValueError("Host terminology review must use schema_version 1")
    reviewer = selection.get("reviewer")
    if reviewer != "current-host-agent":
        raise ValueError("Host terminology review reviewer must be current-host-agent")
    return reviewer


def validate_selected_evidence(
    selected_terminology: list[dict[str, Any]], papers: list[dict[str, Any]]
) -> None:
    paper_ids = {
        str(item.get("openalex_id") or "").strip()
        for item in papers
        if isinstance(item, dict) and str(item.get("openalex_id") or "").strip()
    }
    for item in selected_terminology:
        term = str(item.get("term") or "").strip()
        source_papers = {
            str(paper_id).strip()
            for paper_id in item.get("source_papers") or []
            if str(paper_id).strip()
        }
        has_loadable_evidence = any(
            str(example.get("openalex_id") or "").strip() in paper_ids
            and str(example.get("openalex_id") or "").strip() in source_papers
            and bool(str(example.get("sentence") or "").strip())
            for example in item.get("representative_sentences") or []
            if isinstance(example, dict)
        )
        if not has_loadable_evidence:
            raise ValueError(
                f"selected terminology has no loadable paper evidence: {term}"
            )


def finalize_review(workspace: Path, selection_path: Path) -> dict[str, int]:
    analysis_dir = workspace.resolve() / "analysis"
    terminology = read_jsonl(analysis_dir / "terminology-candidates.jsonl")
    papers = read_jsonl(analysis_dir / "papers.jsonl")
    selection = read_json(selection_path.resolve())
    reviewer = validate_review_identity(selection)
    selected_terminology = merge_terminology_selection(
        terminology, selection.get("terminology")
    )
    explanations = normalize_explanations(
        selected_terminology, selection.get("terminology_explanations")
    )
    validate_selected_evidence(selected_terminology, papers)

    write_jsonl(analysis_dir / "first-terminology-map.jsonl", selected_terminology)
    write_json(analysis_dir / "terminology-explanations.json", explanations)
    write_flat_tsv(
        analysis_dir / "first-terminology-map.tsv",
        selected_terminology,
        [
            "term",
            "total_count",
            "document_count",
            "document_share",
            "c_value",
            "surface_forms",
            "acronyms",
            "representative_sentences",
            "source_papers",
            "host_review_classification",
            "host_review_confidence",
        ],
    )
    write_json(
        analysis_dir / "host-review-summary.json",
        {
            "schema_version": 1,
            "generated_at": utc_now(),
            "reviewer": reviewer,
            "review_passes": 1,
            "source": "full-text terminology candidates and representative sentences",
            "terminology_candidate_count": len(terminology),
            "selected_terminology_count": len(selected_terminology),
            "outputs": {
                "terminology": "analysis/first-terminology-map.tsv",
                "terminology_explanations": "analysis/terminology-explanations.json",
            },
        },
    )
    return {
        "terminology_candidate_count": len(terminology),
        "selected_terminology_count": len(selected_terminology),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(finalize_review(args.workspace, args.selection), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
