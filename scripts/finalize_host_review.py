#!/usr/bin/env python3
"""Validate and merge a host agent's one-pass lexical review."""

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
                    row[field] = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            writer.writerow(row)
    temporary.replace(path)


def merge_selection(
    candidates: list[dict[str, Any]],
    selections: dict[str, float],
    key: str,
    classification: str,
) -> list[dict[str, Any]]:
    candidate_by_key = {str(item[key]): item for item in candidates}
    missing = sorted(set(selections) - set(candidate_by_key))
    if missing:
        raise ValueError(f"Host review contains unknown {key} values: {missing}")
    merged: list[dict[str, Any]] = []
    for candidate in candidates:
        candidate_key = str(candidate[key])
        if candidate_key not in selections:
            continue
        record = dict(candidate)
        record["host_review_classification"] = classification
        record["host_review_confidence"] = float(selections[candidate_key])
        merged.append(record)
    return merged


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def write_review_markdown(
    path: Path,
    stats: dict[str, Any],
    paper_decisions: list[dict[str, Any]],
    vocabulary: list[dict[str, Any]],
    terminology: list[dict[str, Any]],
) -> None:
    lines = [
        "# ResearchRamp lexical maps — first pass for review",
        "",
        "This file presents the first generated pass without post-hoc threshold tuning.",
        "",
        f"- Downloaded and extracted PDFs: {stats['analyzed_pdf_count']}",
        f"- Included in full-text lexical statistics: {stats['included_paper_count']}",
        f"- Duplicate versions excluded: {stats['duplicate_paper_count']}",
        f"- Extreme low-relevance papers excluded: {stats['low_relevance_paper_count']}",
        f"- Selected vocabulary entries: {len(vocabulary)}",
        f"- Selected terminology entries: {len(terminology)}",
        "",
        "## Papers not counted in the lexical statistics",
        "",
        "| Decision | Title | Relevance | Reason |",
        "| --- | --- | ---: | --- |",
    ]
    for paper in paper_decisions:
        if paper.get("analysis_decision") == "include":
            continue
        lines.append(
            "| {decision} | {title} | {score} | {reason} |".format(
                decision=paper.get("analysis_decision", ""),
                title=markdown_cell(str(paper.get("title") or "")),
                score=paper.get("relevance_score", ""),
                reason=markdown_cell("; ".join(paper.get("analysis_reasons") or [])),
            )
        )

    lines.extend(
        [
            "",
            "## First Vocabulary Map",
            "",
            "| Lemma | POS | Papers | Count | Dispersion | Common surface forms |",
            "| --- | --- | ---: | ---: | ---: | --- |",
        ]
    )
    for item in vocabulary:
        surfaces = ", ".join(
            str(record["form"]) for record in item.get("surface_forms", [])[:4]
        )
        lines.append(
            f"| {markdown_cell(item['lemma'])} | {item['part_of_speech']} | "
            f"{item['document_count']} | {item['total_count']} | {item['dispersion']} | "
            f"{markdown_cell(surfaces)} |"
        )

    lines.extend(
        [
            "",
            "## First Terminology Map",
            "",
            "| Term | Papers | Count | Representative corpus sentence |",
            "| --- | ---: | ---: | --- |",
        ]
    )
    for item in terminology:
        examples = item.get("representative_sentences") or []
        example = str(examples[0].get("sentence") or "") if examples else ""
        if len(example) > 260:
            example = example[:257].rstrip() + "..."
        lines.append(
            f"| {markdown_cell(item['term'])} | {item['document_count']} | "
            f"{item['total_count']} | {markdown_cell(example)} |"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()

    analysis_dir = args.workspace.resolve() / "analysis"
    selection = read_json(args.selection)
    vocabulary = read_jsonl(analysis_dir / "vocabulary-map.jsonl")
    terminology = read_jsonl(analysis_dir / "terminology-candidates.jsonl")
    stats = read_json(analysis_dir / "corpus-stats.json")
    paper_decisions = read_jsonl(analysis_dir / "paper-decisions.jsonl")
    selected_vocabulary = merge_selection(
        vocabulary,
        selection["vocabulary"],
        "lemma",
        "domain-vocabulary",
    )
    selected_terminology = merge_selection(
        terminology,
        selection["terminology"],
        "term",
        "domain-term",
    )
    explanations = selection.get("terminology_explanations")
    if not isinstance(explanations, dict):
        raise ValueError("Host review must include terminology_explanations")
    selected_terms = {str(item["term"]).strip().casefold() for item in selected_terminology}
    explanation_terms = {str(term).strip().casefold() for term in explanations}
    if selected_terms != explanation_terms:
        raise ValueError(
            "terminology_explanations must contain exactly the selected terminology entries"
        )
    normalized_explanations: dict[str, dict[str, str]] = {}
    for raw_term, raw_explanation in explanations.items():
        term = str(raw_term).strip().casefold()
        if not isinstance(raw_explanation, dict):
            raise ValueError(f"terminology explanation must be an object: {term}")
        record = {
            "meaning_en": str(raw_explanation.get("meaning_en") or "").strip(),
            "meaning_zh": str(raw_explanation.get("meaning_zh") or "").strip(),
            "concept_role": str(raw_explanation.get("concept_role") or "").strip(),
            "sense_key": str(raw_explanation.get("sense_key") or term).strip(),
        }
        if not record["meaning_en"] or not record["meaning_zh"] or not record["concept_role"]:
            raise ValueError(f"terminology explanation is incomplete: {term}")
        normalized_explanations[term] = record

    write_jsonl(analysis_dir / "first-vocabulary-map.jsonl", selected_vocabulary)
    write_jsonl(analysis_dir / "first-terminology-map.jsonl", selected_terminology)
    write_json(
        analysis_dir / "terminology-explanations.json", normalized_explanations
    )
    write_flat_tsv(
        analysis_dir / "first-vocabulary-map.tsv",
        selected_vocabulary,
        [
            "lemma",
            "part_of_speech",
            "total_count",
            "frequency_per_million",
            "document_count",
            "document_share",
            "dispersion",
            "surface_forms",
            "representative_sentences",
            "source_papers",
            "host_review_classification",
            "host_review_confidence",
        ],
    )
    write_review_markdown(
        analysis_dir / "first-map-review.md",
        stats,
        paper_decisions,
        selected_vocabulary,
        selected_terminology,
    )
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
            "reviewer": "current-host-agent",
            "review_passes": 1,
            "source": "full-text corpus statistics and representative sentences",
            "vocabulary_candidate_count": len(vocabulary),
            "selected_vocabulary_count": len(selected_vocabulary),
            "terminology_candidate_count": len(terminology),
            "selected_terminology_count": len(selected_terminology),
            "outputs": {
                "vocabulary": "analysis/first-vocabulary-map.tsv",
                "terminology": "analysis/first-terminology-map.tsv",
                "terminology_explanations": "analysis/terminology-explanations.json",
                "review": "analysis/first-map-review.md",
            },
        },
    )
    print(
        json.dumps(
            {
                "selected_vocabulary_count": len(selected_vocabulary),
                "selected_terminology_count": len(selected_terminology),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
