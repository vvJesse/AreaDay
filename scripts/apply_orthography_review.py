#!/usr/bin/env python3
"""Apply a host agent's contextual spelling review to aggregated lemma records."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from corpus_analysis import _write_vocabulary_tsv
from lexical_assets import juilland_dispersion
from researchramp_core import read_json, utc_now, write_json, write_jsonl


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def validate_selection(
    selection: dict[str, Any], review_input: dict[str, Any]
) -> tuple[dict[str, str], set[str]]:
    candidates = {
        str(item["observed_lemma"])
        for item in review_input.get("candidates", [])
    }
    replacements = {
        str(source).casefold(): str(target).casefold()
        for source, target in selection.get("lemma_replacements", {}).items()
    }
    drops = {str(value).casefold() for value in selection.get("lemma_drops", [])}
    unknown = (set(replacements) | drops) - candidates
    if unknown:
        raise ValueError(f"Review contains lemmas outside the suspicious queue: {sorted(unknown)}")
    overlap = set(replacements) & drops
    if overlap:
        raise ValueError(f"Lemmas cannot be both replaced and dropped: {sorted(overlap)}")
    invalid_targets = [
        target
        for target in replacements.values()
        if not target or not all(part.replace("'", "").isalpha() for part in target.split("-"))
    ]
    if invalid_targets:
        raise ValueError(f"Invalid canonical lemmas: {sorted(set(invalid_targets))}")
    return replacements, drops


def merge_vocabulary(
    vocabulary: list[dict[str, Any]],
    replacements: dict[str, str],
    drops: set[str],
    work_ids: list[str],
    total_tokens: int,
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in vocabulary:
        observed = str(record["lemma"])
        if observed in drops:
            continue
        target = replacements.get(observed, observed)
        grouped.setdefault(target, []).append(record)

    merged: list[dict[str, Any]] = []
    for lemma, records in grouped.items():
        per_document: Counter[str] = Counter()
        surfaces: Counter[str] = Counter()
        pos_counts: Counter[str] = Counter()
        examples: list[dict[str, str]] = []
        seen_examples: set[tuple[str, str]] = set()
        for record in records:
            per_document.update(
                {
                    str(work_id): int(count)
                    for work_id, count in record.get("per_document_counts", {}).items()
                }
            )
            pos_counts[str(record.get("part_of_speech") or "")] += int(
                record.get("total_count", 0)
            )
            for surface in record.get("surface_forms", []):
                surfaces[str(surface["form"])] += int(surface["count"])
            for example in record.get("representative_sentences", []):
                identity = (
                    str(example.get("openalex_id") or ""),
                    str(example.get("sentence") or ""),
                )
                if identity in seen_examples or not identity[1]:
                    continue
                seen_examples.add(identity)
                examples.append(
                    {"openalex_id": identity[0], "sentence": identity[1]}
                )

        total_count = sum(per_document.values())
        source_papers = [work_id for work_id in work_ids if per_document[work_id] > 0]
        vector = [per_document[work_id] for work_id in work_ids]
        merged.append(
            {
                "lemma": lemma,
                "part_of_speech": pos_counts.most_common(1)[0][0],
                "total_count": total_count,
                "frequency_per_million": round(
                    total_count * 1_000_000 / total_tokens, 3
                )
                if total_tokens
                else 0.0,
                "document_count": len(source_papers),
                "document_share": round(len(source_papers) / len(work_ids), 6)
                if work_ids
                else 0.0,
                "dispersion": round(juilland_dispersion(vector), 6),
                "per_document_counts": dict(per_document),
                "surface_forms": [
                    {"form": form, "count": count}
                    for form, count in surfaces.most_common(8)
                ],
                "representative_sentences": examples[:3],
                "source_papers": source_papers,
            }
        )

    merged.sort(
        key=lambda item: (
            -item["document_count"],
            -item["dispersion"],
            -item["total_count"],
            item["lemma"],
        )
    )
    return merged


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    args = parser.parse_args()

    analysis_dir = args.workspace.resolve() / "analysis"
    review_input = read_json(analysis_dir / "orthography-review-input.json")
    selection = read_json(args.selection.resolve())
    replacements, drops = validate_selection(selection, review_input)
    vocabulary = read_jsonl(analysis_dir / "pre-orthography-vocabulary-map.jsonl")
    stats = read_json(analysis_dir / "corpus-stats.json")
    paper_decisions = read_jsonl(analysis_dir / "paper-decisions.jsonl")
    work_ids = [
        str(item["openalex_id"])
        for item in paper_decisions
        if item.get("analysis_decision") == "include"
    ]
    merged = merge_vocabulary(
        vocabulary,
        replacements,
        drops,
        work_ids,
        int(stats.get("content_lemma_token_count", 0)),
    )

    _write_vocabulary_tsv(analysis_dir / "vocabulary-map.tsv", merged)
    _write_vocabulary_tsv(analysis_dir / "vocabulary.tsv", merged)
    write_jsonl(analysis_dir / "vocabulary-map.jsonl", merged)

    stats["vocabulary_entry_count"] = len(merged)
    stats["orthography_review_applied"] = True
    stats["orthography_replacement_count"] = len(replacements)
    stats["orthography_drop_count"] = len(drops)
    write_json(analysis_dir / "corpus-stats.json", stats)
    write_json(
        analysis_dir / "orthography-review-summary.json",
        {
            "schema_version": 1,
            "generated_at": utc_now(),
            "reviewer": selection.get("reviewer", "current-host-agent"),
            "reviewed_candidate_count": len(review_input.get("candidates", [])),
            "replacement_count": len(replacements),
            "drop_count": len(drops),
            "unchanged_candidate_count": (
                len(review_input.get("candidates", []))
                - len(replacements)
                - len(drops)
            ),
            "review_summary": selection.get("review_summary", ""),
        },
    )
    print(
        json.dumps(
            {
                "vocabulary_entry_count": len(merged),
                "replacement_count": len(replacements),
                "drop_count": len(drops),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
