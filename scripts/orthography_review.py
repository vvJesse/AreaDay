"""Find likely extraction/spelling anomalies for contextual host-agent review."""

from __future__ import annotations

import importlib.resources
from typing import Any

from symspellpy import SymSpell, Verbosity
from wordfreq import zipf_frequency


def load_symspell() -> SymSpell:
    spell = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    dictionary = (
        importlib.resources.files("symspellpy")
        / "frequency_dictionary_en_82_765.txt"
    )
    if not spell.load_dictionary(str(dictionary), term_index=0, count_index=1):
        raise RuntimeError(f"Could not load SymSpell dictionary: {dictionary}")
    return spell


def build_orthography_review_candidates(
    vocabulary: list[dict[str, Any]],
    *,
    spell: SymSpell | None = None,
) -> list[dict[str, Any]]:
    """Return every vocabulary lemma missing from SymSpell's English lexicon.

    This is deliberately a high-recall review queue, not an automatic spelling
    corrector. Valid technical terms are expected and must be kept by the host
    agent when their corpus contexts support them.
    """
    spell = spell or load_symspell()
    candidates: list[dict[str, Any]] = []
    for record in vocabulary:
        lemma = str(record["lemma"])
        in_dictionary = bool(
            spell.lookup(
                lemma,
                Verbosity.TOP,
                max_edit_distance=0,
                include_unknown=False,
            )
        )
        if in_dictionary:
            continue
        segmentation = spell.word_segmentation(lemma, max_edit_distance=0)
        suggestions = spell.lookup(
            lemma,
            Verbosity.CLOSEST,
            max_edit_distance=2,
            include_unknown=False,
            transfer_casing=False,
        )
        candidates.append(
            {
                "observed_lemma": lemma,
                "zipf_frequency": round(float(zipf_frequency(lemma, "en")), 3),
                "segmentation_suggestion": segmentation.corrected_string,
                "segmentation_distance_sum": segmentation.distance_sum,
                "spelling_suggestions": [
                    {
                        "term": suggestion.term,
                        "edit_distance": suggestion.distance,
                        "frequency": suggestion.count,
                    }
                    for suggestion in suggestions[:5]
                ],
                "total_count": record["total_count"],
                "document_count": record["document_count"],
                "surface_forms": record.get("surface_forms", []),
                "representative_sentences": record.get(
                    "representative_sentences", []
                ),
                "source_papers": record.get("source_papers", [])[:10],
            }
        )
    candidates.sort(
        key=lambda item: (
            -int(item["document_count"]),
            -int(item["total_count"]),
            str(item["observed_lemma"]),
        )
    )
    return candidates
