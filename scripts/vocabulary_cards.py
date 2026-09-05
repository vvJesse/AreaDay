"""Build and read AreaDay's stable, source-grounded vocabulary-card catalog.

The catalog is prepared before calibration.  Calibration only decides which
cards enter the learner's candidate pool; it never creates or rewrites a
definition.  This keeps later research briefs from changing an existing
learning card's meaning.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from researchramp_core import read_json, utc_now, write_json, write_jsonl
from orthography_contract import orthography_summary_is_complete


CATALOG_NAME = "vocabulary-card-catalog.jsonl"
SUMMARY_NAME = "vocabulary-card-summary.json"
REVIEW_INPUT_NAME = "vocabulary-card-review-input.json"
REVIEW_BATCH_DIRECTORY = "vocabulary-card-review-batches"
GLOSS_DATA_NAME = "ecdict_glosses.tsv.gz"
WORD_PATTERN = re.compile(r"^[a-z][a-z0-9'-]*$")
ACRONYM_SURFACE_PATTERN = re.compile(r"^[A-Z][A-Z0-9-]{1,11}s?$")
REVIEW_SCHEMA_VERSION = 3
REVIEW_BATCH_SIZE = 40


def _normalize(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def card_id(lemma: str, part_of_speech: str) -> str:
    """Return an identity independent of mutable definition wording."""

    normalized_lemma = _normalize(lemma)
    normalized_pos = _normalize(part_of_speech) or "unknown"
    return f"word:{normalized_lemma}:{normalized_pos}"


def _source_url(paper: dict[str, Any]) -> str:
    direct = str(paper.get("source_url") or "").strip()
    if direct.startswith(("http://", "https://")):
        return direct
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(paper.get("doi") or "").strip(), flags=re.I)
    if doi:
        return f"https://doi.org/{doi}"
    arxiv = str(paper.get("arxiv_id") or "").strip()
    if arxiv:
        return f"https://arxiv.org/abs/{arxiv}"
    openalex = str(paper.get("openalex_id") or "").strip()
    if openalex:
        return f"https://openalex.org/{openalex}"
    raise ValueError("Vocabulary card source paper has no public source URL")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _pos_matches(card_pos: str, dictionary_pos: str) -> bool:
    normalized = _normalize(dictionary_pos)
    if not normalized:
        return True
    aliases = {
        "noun": ("n", "noun"),
        "verb": ("v", "verb"),
        "adj": ("a", "adj", "adjective"),
        "adjective": ("a", "adj", "adjective"),
        "adv": ("ad", "adv", "adverb"),
        "adverb": ("ad", "adv", "adverb"),
    }
    expected = aliases.get(_normalize(card_pos), ())
    return not expected or any(token in normalized for token in expected)


def _compact_gloss(value: object, *, maximum: int = 420) -> str:
    """Normalize a short dictionary gloss without leaking escaped line breaks."""

    raw = (
        str(value or "")
        .replace("\\r\\n", "\n")
        .replace("\\n", "\n")
        .replace("\\r", "\n")
    )
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
    lines = [line for line in lines if line and not line.startswith("[")]
    if not lines:
        return ""
    text = "；".join(line.rstrip(";；") for line in lines)
    return text if len(text) <= maximum else text[: maximum - 1].rstrip() + "…"


@dataclass(frozen=True)
class DictionaryGloss:
    lemma: str
    part_of_speech: str
    meaning_en: str
    meaning_zh: str


def load_dictionary_glosses(path: Path) -> dict[str, list[DictionaryGloss]]:
    if not path.is_file():
        raise FileNotFoundError(f"AreaDay bilingual ECDICT asset is missing: {path}")
    result: dict[str, list[DictionaryGloss]] = {}
    with gzip.open(path, "rt", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        expected = {"lemma", "part_of_speech", "meaning_en", "meaning_zh"}
        if set(reader.fieldnames or []) != expected:
            raise ValueError("AreaDay bilingual ECDICT asset has an invalid schema")
        for row in reader:
            lemma = _normalize(row.get("lemma"))
            meaning_zh = _compact_gloss(row.get("meaning_zh"))
            if not lemma or not meaning_zh:
                continue
            result.setdefault(lemma, []).append(
                DictionaryGloss(
                    lemma=lemma,
                    part_of_speech=str(row.get("part_of_speech") or "").strip(),
                    meaning_en=_compact_gloss(row.get("meaning_en")),
                    meaning_zh=meaning_zh,
                )
            )
    return result


def select_dictionary_gloss(
    glossary: dict[str, list[DictionaryGloss]], lemma: str, part_of_speech: str
) -> DictionaryGloss | None:
    candidates = glossary.get(_normalize(lemma), [])
    if not candidates:
        return None
    matching = [item for item in candidates if _pos_matches(part_of_speech, item.part_of_speech)]
    if len(matching) == 1:
        return matching[0]
    # A single record is safe even when ECDICT does not expose a POS tag.
    if len(candidates) == 1:
        return candidates[0]
    return None


def _acronym_expansions(path: Path) -> dict[str, list[str]]:
    if not path.is_file():
        return {}
    result: dict[str, set[str]] = {}
    for item in _load_jsonl(path):
        term = _normalize(item.get("term"))
        if not term:
            continue
        for raw_acronym in item.get("acronyms") or []:
            acronym = _normalize(raw_acronym)
            if acronym:
                result.setdefault(acronym, set()).add(term)
    return {key: _collapse_plural_expansions(values) for key, values in result.items()}


def _collapse_plural_expansions(values: Iterable[str]) -> list[str]:
    normalized = {_normalize(value) for value in values if _normalize(value)}
    collapsed: list[str] = []
    for value in sorted(normalized):
        words = value.split()
        if not words:
            continue
        last = words[-1]
        stems = []
        if last.endswith("ies") and len(last) > 3:
            stems.append(last[:-3] + "y")
        if last.endswith("es") and len(last) > 2:
            stems.append(last[:-2])
        if last.endswith("s") and len(last) > 1:
            stems.append(last[:-1])
        singular_variants = {" ".join([*words[:-1], stem]) for stem in stems}
        if singular_variants & normalized:
            continue
        collapsed.append(value)
    return collapsed


def _has_acronym_surface(word: dict[str, Any]) -> bool:
    """Return true only when acronym casing dominates observed corpus usage.

    PDF section headings commonly contribute isolated all-caps forms such as
    ``RESULTS`` and ``INTRODUCTION``.  The old any-match rule treated those as
    acronyms and needlessly sent hundreds of ordinary words to contextual
    review.  A real acronym normally keeps its casing across occurrences.
    """

    uppercase_count = 0
    observed_count = 0
    for item in word.get("surface_forms") or []:
        if not isinstance(item, dict):
            continue
        form = str(item.get("form") or "").strip()
        try:
            count = int(item.get("count") or 0)
        except (TypeError, ValueError):
            count = 0
        if count <= 0:
            continue
        observed_count += count
        if ACRONYM_SURFACE_PATTERN.fullmatch(form):
            uppercase_count += count
    return uppercase_count >= 2 and uppercase_count / max(observed_count, 1) >= 0.5


def _dictionary_marks_abbreviation(candidates: list[DictionaryGloss]) -> bool:
    return any(
        re.search(r"\babbr\.?", f"{item.meaning_en} {item.meaning_zh}", flags=re.I)
        or "缩写" in item.meaning_zh
        for item in candidates
    )


def _context_review_reasons(
    glossary: dict[str, list[DictionaryGloss]],
    word: dict[str, Any],
    acronym_map: dict[str, list[str]],
) -> list[str]:
    lemma = _normalize(word.get("lemma"))
    part_of_speech = str(word.get("part_of_speech") or "").strip()
    candidates = glossary.get(lemma, [])
    selected = select_dictionary_gloss(glossary, lemma, part_of_speech)
    reasons: list[str] = []
    if selected is None:
        reasons.append("no_unambiguous_dictionary_gloss")
    if len(candidates) > 1:
        reasons.append("multiple_dictionary_entries")
    if _has_acronym_surface(word):
        reasons.append("acronym_surface_form")
    if acronym_map.get(lemma):
        reasons.append("corpus_acronym_expansion")
    if _dictionary_marks_abbreviation(candidates):
        reasons.append("dictionary_abbreviation")
    return reasons


def _dictionary_candidates_payload(
    candidates: list[DictionaryGloss],
) -> list[dict[str, str]]:
    return [
        {
            "part_of_speech": item.part_of_speech,
            "meaning_en": item.meaning_en,
            "meaning_zh": item.meaning_zh,
        }
        for item in candidates
    ]


def _suggested_sense_key(expansions: list[str]) -> str:
    if len(expansions) != 1:
        return ""
    return re.sub(r"[^a-z0-9]+", "-", expansions[0].casefold()).strip("-")


def _normalized_semantic_phrase(value: object) -> str:
    words = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    if words and words[-1].endswith("s") and len(words[-1]) > 1:
        words[-1] = words[-1][:-1]
    return " ".join(words)


def _dictionary_matches_expansion(candidate: dict[str, str], expansion: str) -> bool:
    expected = set(_normalized_semantic_phrase(expansion).split())
    actual = set(_normalized_semantic_phrase(candidate.get("meaning_en")).split())
    return bool(expected) and expected <= actual


def _review_glosses(
    selection: dict[str, Any], candidates: list[dict[str, Any]]
) -> tuple[dict[str, dict[str, str]], set[str]]:
    if selection.get("vocabulary_card_review_schema_version") != REVIEW_SCHEMA_VERSION:
        raise ValueError(
            "Learning-asset review does not declare the current vocabulary-card "
            f"review schema version {REVIEW_SCHEMA_VERSION}"
        )
    raw = selection.get("vocabulary_card_glosses", {})
    if not isinstance(raw, dict):
        raise ValueError("vocabulary_card_glosses must be an object")
    raw_drops = selection.get("vocabulary_card_drops", [])
    if not isinstance(raw_drops, list):
        raise ValueError("vocabulary_card_drops must be an array")
    drops = {_normalize(lemma) for lemma in raw_drops}
    if "" in drops or len(drops) != len(raw_drops):
        raise ValueError("vocabulary_card_drops must contain unique non-empty lemmas")
    candidate_by_lemma = {
        _normalize(item.get("observed_lemma")): item for item in candidates
    }
    supplied_lemmas = {_normalize(lemma) for lemma in raw}
    expected_lemmas = set(candidate_by_lemma)
    overlap = sorted(supplied_lemmas & drops)
    missing = sorted(expected_lemmas - supplied_lemmas - drops)
    unknown = sorted((supplied_lemmas | drops) - expected_lemmas)
    if overlap or missing or unknown or len(raw) + len(raw_drops) != len(expected_lemmas):
        raise ValueError(
            "Every .candidates lemma must be either glossed or dropped exactly once; "
            f"overlap={overlap[:8]}, missing={missing[:8]}, unknown={unknown[:8]}"
        )
    result: dict[str, dict[str, str]] = {}
    for raw_lemma, raw_gloss in raw.items():
        lemma = _normalize(raw_lemma)
        if not lemma or not isinstance(raw_gloss, dict):
            raise ValueError("Each vocabulary card gloss must be keyed by a lemma")
        meaning_zh = _compact_gloss(raw_gloss.get("meaning_zh"))
        if not meaning_zh:
            raise ValueError(f"Vocabulary card gloss has no Chinese meaning: {lemma}")
        sense_key = _normalize(raw_gloss.get("sense_key"))
        if not sense_key:
            raise ValueError(f"Vocabulary card gloss has no stable sense key: {lemma}")
        rationale = str(raw_gloss.get("context_rationale") or "").strip()
        if not rationale:
            raise ValueError(f"Vocabulary card gloss has no context rationale: {lemma}")

        candidate = candidate_by_lemma[lemma]
        expansions = [
            str(value).strip()
            for value in candidate.get("acronym_expansions") or []
            if str(value).strip()
        ]
        dictionary_candidates = [
            item
            for item in candidate.get("dictionary_candidates") or []
            if isinstance(item, dict)
        ]
        if len(expansions) == 1:
            expansion = expansions[0]
            expected_sense_key = str(candidate.get("suggested_sense_key") or "").strip()
            if sense_key != _normalize(expected_sense_key):
                raise ValueError(
                    f"Vocabulary card sense_key conflicts with corpus acronym expansion: {lemma}"
                )
            meaning_en = _compact_gloss(raw_gloss.get("meaning_en"))
            if _normalized_semantic_phrase(meaning_en) != _normalized_semantic_phrase(expansion):
                raise ValueError(
                    f"Vocabulary card English meaning conflicts with corpus acronym expansion: {lemma}"
                )
            for dictionary_candidate in dictionary_candidates:
                dictionary_zh = _compact_gloss(dictionary_candidate.get("meaning_zh"))
                if (
                    dictionary_zh
                    and meaning_zh == dictionary_zh
                    and not _dictionary_matches_expansion(dictionary_candidate, expansion)
                ):
                    raise ValueError(
                        f"Vocabulary card copied a dictionary meaning that conflicts with "
                        f"the corpus acronym expansion: {lemma}"
                    )
        result[lemma] = {
            "meaning_en": _compact_gloss(raw_gloss.get("meaning_en")),
            "meaning_zh": meaning_zh,
            "sense_key": sense_key,
        }
    return result, drops


def validate_review_batch(
    selection: dict[str, Any], candidates: list[dict[str, Any]]
) -> None:
    """Validate one completed bounded batch while ignoring preserved batches."""

    raw = selection.get("vocabulary_card_glosses")
    if not isinstance(raw, dict):
        raise ValueError("vocabulary_card_glosses must be an object")
    batch_lemmas = {
        _normalize(candidate.get("observed_lemma")) for candidate in candidates
    }
    subset = {
        **selection,
        "vocabulary_card_glosses": {
            lemma: gloss for lemma, gloss in raw.items() if _normalize(lemma) in batch_lemmas
        },
        "vocabulary_card_drops": [
            lemma
            for lemma in selection.get("vocabulary_card_drops", [])
            if _normalize(lemma) in batch_lemmas
        ],
    }
    _review_glosses(subset, candidates)


def _paper_index(workspace: Path) -> dict[str, dict[str, Any]]:
    records = _load_jsonl(workspace / "analysis" / "papers.jsonl")
    return {
        str(record.get("openalex_id") or "").strip(): record
        for record in records
        if str(record.get("openalex_id") or "").strip()
    }


def _source_for_word(word: dict[str, Any], papers: dict[str, dict[str, Any]]) -> dict[str, str]:
    examples = [item for item in word.get("representative_sentences") or [] if isinstance(item, dict)]
    for example in examples:
        paper_id = str(example.get("openalex_id") or "").strip()
        context = str(example.get("sentence") or "").strip()
        paper = papers.get(paper_id)
        if paper is not None and context:
            return {
                "source_paper_id": paper_id,
                "source_title": str(paper.get("title") or "").strip(),
                "source_url": _source_url(paper),
                "context": context,
            }
    raise ValueError(f"Vocabulary card has no loadable source context: {word.get('lemma')}")


def prepare_review_input(workspace: Path, dictionary_path: Path) -> dict[str, Any]:
    """Prepare context-sensitive glosses after vocabulary spelling is finalized."""

    resolved = workspace.expanduser().resolve()
    analysis = resolved / "analysis"
    orthography = read_json(analysis / "orthography-review-summary.json")
    corpus_stats = read_json(analysis / "corpus-stats.json")
    if (
        not orthography_summary_is_complete(orthography)
        or not isinstance(corpus_stats, dict)
        or corpus_stats.get("orthography_review_applied") is not True
    ):
        raise ValueError(
            "Vocabulary orthography review must be finalized before card preparation"
        )
    glossary = load_dictionary_glosses(dictionary_path)
    vocabulary = _load_jsonl(analysis / "vocabulary-map.jsonl")
    acronym_map = _acronym_expansions(analysis / "terminology-candidates.jsonl")
    candidates = []
    for word in vocabulary:
        lemma = _normalize(word.get("lemma"))
        pos = str(word.get("part_of_speech") or "").strip()
        if not lemma:
            continue
        review_reasons = _context_review_reasons(glossary, word, acronym_map)
        if not review_reasons:
            continue
        expansions = acronym_map.get(lemma, [])
        candidates.append(
            {
                "observed_lemma": lemma,
                "part_of_speech": pos,
                "surface_forms": word.get("surface_forms") or [],
                "representative_sentences": word.get("representative_sentences") or [],
                "source_papers": word.get("source_papers") or [],
                "review_reasons": review_reasons,
                "acronym_expansions": expansions,
                "suggested_sense_key": _suggested_sense_key(expansions),
                "dictionary_candidates": _dictionary_candidates_payload(
                    glossary.get(lemma, [])
                ),
            }
        )
    instruction = (
        "The review array is the top-level .candidates field (not .vocabulary_cards). "
        "First verify that its length equals candidate_count. Review every candidate in "
        "this bounded batch from its corpus evidence; never bulk-fill meanings from the "
        "first dictionary entry. Dictionary candidates are suggestions, never authority "
        "for an acronym or context-sensitive word. For every observed_lemma in this batch, "
        "either add one gloss or add the lemma to vocabulary_card_drops when its meaning "
        "cannot be determined confidently. Preserve entries and drops from earlier "
        "controller-supplied batches, but do not add unreviewed lemmas. Briefly state why "
        "the context supports each retained sense. When there is one "
        "acronym_expansion, use it as the exact English meaning, and use "
        "suggested_sense_key exactly. Every candidate already passed orthography review; "
        "keep its canonical lemma."
    )
    output_schema = {
        "vocabulary_card_review_schema_version": REVIEW_SCHEMA_VERSION,
        "vocabulary_card_glosses": {
            "exact observed_lemma": {
                "meaning_en": "concise contextual English explanation or empty string",
                "meaning_zh": "concise contextual Chinese explanation",
                "sense_key": "stable semantic identifier",
                "context_rationale": "brief candidate-specific sense decision",
            }
        },
        "vocabulary_card_drops": ["exact observed_lemma that cannot be judged confidently"],
    }
    batch_count = max(1, (len(candidates) + REVIEW_BATCH_SIZE - 1) // REVIEW_BATCH_SIZE)
    batch_directory = analysis / REVIEW_BATCH_DIRECTORY
    batch_directory.mkdir(parents=True, exist_ok=True)
    batches = []
    for batch_offset in range(batch_count):
        batch_candidates = candidates[
            batch_offset * REVIEW_BATCH_SIZE : (batch_offset + 1) * REVIEW_BATCH_SIZE
        ]
        batch_path = batch_directory / f"batch-{batch_offset + 1:03d}.json"
        write_json(
            batch_path,
            {
                "schema_version": REVIEW_SCHEMA_VERSION,
                "batch_index": batch_offset + 1,
                "batch_count": batch_count,
                "candidate_count": len(batch_candidates),
                "overall_candidate_count": len(candidates),
                "instruction": instruction,
                "output_schema": output_schema,
                "candidates": batch_candidates,
            },
        )
        batches.append(
            {
                "batch_index": batch_offset + 1,
                "candidate_count": len(batch_candidates),
                "path": str(batch_path),
                "lemmas": [item["observed_lemma"] for item in batch_candidates],
            }
        )
    payload = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "candidate_count": len(candidates),
        "batch_size": REVIEW_BATCH_SIZE,
        "batch_count": batch_count,
        "instruction": instruction,
        "output_schema": output_schema,
        "batches": batches,
        "candidates": candidates,
    }
    write_json(analysis / REVIEW_INPUT_NAME, payload)
    return {
        "candidate_count": len(candidates),
        "batch_count": batch_count,
        "path": str(analysis / REVIEW_INPUT_NAME),
    }


def build_catalog(workspace: Path, selection_path: Path, dictionary_path: Path) -> dict[str, int]:
    """Write one authoritative card per finalized vocabulary lemma."""

    resolved = workspace.expanduser().resolve()
    analysis = resolved / "analysis"
    glossary = load_dictionary_glosses(dictionary_path)
    selection = read_json(selection_path.expanduser().resolve())
    if not isinstance(selection, dict):
        raise ValueError("Learning-asset review must be an object")
    papers = _paper_index(resolved)
    vocabulary = _load_jsonl(analysis / "vocabulary-map.jsonl")
    # Acronym evidence belongs to the corpus, not to the separate decision about
    # whether an expansion is retained as a standalone terminology card.
    acronym_map = _acronym_expansions(analysis / "terminology-candidates.jsonl")
    review_candidates = []
    for word in vocabulary:
        lemma = _normalize(word.get("lemma"))
        if not lemma:
            continue
        review_reasons = _context_review_reasons(glossary, word, acronym_map)
        if not review_reasons:
            continue
        expansions = acronym_map.get(lemma, [])
        review_candidates.append(
            {
                "observed_lemma": lemma,
                "representative_sentences": word.get("representative_sentences") or [],
                "acronym_expansions": expansions,
                "suggested_sense_key": _suggested_sense_key(expansions),
                "dictionary_candidates": _dictionary_candidates_payload(
                    glossary.get(lemma, [])
                ),
            }
        )
    reviewed, drops = _review_glosses(selection, review_candidates)
    cards: list[dict[str, Any]] = []
    ecdict_count = 0
    agent_count = 0
    english_count = 0
    seen: set[str] = set()
    retained_vocabulary: list[dict[str, Any]] = []
    for word in vocabulary:
        lemma = _normalize(word.get("lemma"))
        if lemma in drops:
            continue
        retained_vocabulary.append(word)
        pos = str(word.get("part_of_speech") or "").strip().lower()
        if not lemma or not WORD_PATTERN.fullmatch(lemma):
            raise ValueError("Finalized vocabulary contains an invalid lemma")
        identifier = card_id(lemma, pos)
        if identifier in seen:
            raise ValueError(f"Vocabulary card identity is duplicated: {identifier}")
        seen.add(identifier)
        dictionary_gloss = select_dictionary_gloss(glossary, lemma, pos)
        review_reasons = _context_review_reasons(glossary, word, acronym_map)
        if not review_reasons and dictionary_gloss is not None:
            meaning_en = dictionary_gloss.meaning_en
            meaning_zh = dictionary_gloss.meaning_zh
            origin = "ecdict"
            sense_key = identifier
            ecdict_count += 1
        else:
            agent_gloss = reviewed.get(lemma)
            if agent_gloss is None:
                raise ValueError(f"Vocabulary card gloss is unresolved: {lemma}")
            meaning_en = agent_gloss["meaning_en"]
            meaning_zh = agent_gloss["meaning_zh"]
            sense_key = (
                _suggested_sense_key(acronym_map.get(lemma, []))
                or agent_gloss["sense_key"]
            )
            origin = "agent-contextual" if dictionary_gloss is not None else "agent"
            agent_count += 1
        source = _source_for_word(word, papers)
        if meaning_en:
            english_count += 1
        cards.append(
            {
                "card_id": identifier,
                "sense_key": sense_key,
                "lemma": lemma,
                "part_of_speech": pos,
                "meaning_en": meaning_en,
                "meaning_zh": meaning_zh,
                "meaning_origin": origin,
                **source,
                "total_count": int(word.get("total_count") or 0),
                "document_count": int(word.get("document_count") or 0),
                "document_share": float(word.get("document_share") or 0),
            }
        )
    cards.sort(key=lambda item: (-item["document_count"], -item["total_count"], item["lemma"]))
    if drops:
        from corpus_analysis import _write_vocabulary_tsv

        write_jsonl(analysis / "vocabulary-map.jsonl", retained_vocabulary)
        _write_vocabulary_tsv(analysis / "vocabulary-map.tsv", retained_vocabulary)
        _write_vocabulary_tsv(analysis / "vocabulary.tsv", retained_vocabulary)
        stats_path = analysis / "corpus-stats.json"
        if stats_path.is_file():
            stats = read_json(stats_path)
            if isinstance(stats, dict):
                stats["vocabulary_entry_count"] = len(retained_vocabulary)
                stats["vocabulary_card_drop_count"] = len(drops)
                write_json(stats_path, stats)
    write_jsonl(analysis / CATALOG_NAME, cards)
    result = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "generated_at": utc_now(),
        "semantic_review_contract_version": REVIEW_SCHEMA_VERSION,
        "card_count": len(cards),
        "ecdict_count": ecdict_count,
        "agent_count": agent_count,
        "english_count": english_count,
        "drop_count": len(drops),
    }
    write_json(analysis / SUMMARY_NAME, result)
    return {
        "semantic_review_contract_version": REVIEW_SCHEMA_VERSION,
        **{key: int(value) for key, value in result.items() if key.endswith("_count")},
    }


def load_catalog(workspace: Path) -> list[dict[str, Any]]:
    analysis = workspace.expanduser().resolve() / "analysis"
    path = analysis / CATALOG_NAME
    cards = _load_jsonl(path)
    identifiers = [str(card.get("card_id") or "") for card in cards]
    if not cards or len(identifiers) != len(set(identifiers)):
        raise ValueError(f"AreaDay vocabulary card catalog is invalid: {path}")
    for card in cards:
        required = ("card_id", "sense_key", "lemma", "meaning_zh", "context", "source_title", "source_url")
        if not all(str(card.get(field) or "").strip() for field in required):
            raise ValueError(f"AreaDay vocabulary card is incomplete: {card.get('lemma')}")
    summary_path = analysis / SUMMARY_NAME
    if summary_path.is_file():
        summary = read_json(summary_path)
        if (
            not isinstance(summary, dict)
            or summary.get("semantic_review_contract_version")
            != REVIEW_SCHEMA_VERSION
            or summary.get("card_count") != len(cards)
        ):
            raise ValueError(
                f"AreaDay vocabulary cards require semantic re-review: {analysis.parent}"
            )
    return cards
