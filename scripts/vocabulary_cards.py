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
GLOSS_DATA_NAME = "ecdict_glosses.tsv.gz"
WORD_PATTERN = re.compile(r"^[a-z][a-z0-9'-]*$")


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
    """Keep the first useful short dictionary sense without copied clutter."""

    lines = [re.sub(r"\s+", " ", line).strip() for line in str(value or "").splitlines()]
    lines = [line for line in lines if line and not line.startswith("[")]
    if not lines:
        return ""
    text = lines[0]
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


def _review_glosses(selection: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = selection.get("vocabulary_card_glosses", {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("vocabulary_card_glosses must be an object")
    result: dict[str, dict[str, str]] = {}
    for raw_lemma, raw_gloss in raw.items():
        lemma = _normalize(raw_lemma)
        if not lemma or not isinstance(raw_gloss, dict):
            raise ValueError("Each vocabulary card gloss must be keyed by a lemma")
        meaning_zh = _compact_gloss(raw_gloss.get("meaning_zh"))
        if not meaning_zh:
            raise ValueError(f"Vocabulary card gloss has no Chinese meaning: {lemma}")
        result[lemma] = {
            "meaning_en": _compact_gloss(raw_gloss.get("meaning_en")),
            "meaning_zh": meaning_zh,
        }
    return result


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
    """Prepare dictionary misses only after vocabulary spelling is finalized."""

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
    candidates = []
    for word in vocabulary:
        lemma = _normalize(word.get("lemma"))
        pos = str(word.get("part_of_speech") or "").strip()
        if not lemma or select_dictionary_gloss(glossary, lemma, pos) is not None:
            continue
        candidates.append(
            {
                "observed_lemma": lemma,
                "part_of_speech": pos,
                "representative_sentences": word.get("representative_sentences") or [],
                "source_papers": word.get("source_papers") or [],
                "reason": "ECDICT has no unambiguous Chinese gloss for this lemma and part of speech.",
            }
        )
    payload = {
        "schema_version": 1,
        "instruction": (
            "For each unresolved vocabulary card, write a concise Chinese explanation "
            "grounded in the supplied representative sentence. Also write a concise "
            "English explanation when it is useful. Every candidate already passed the "
            "separate orthography review; use its supplied canonical lemma unchanged."
        ),
        "candidates": candidates,
    }
    write_json(analysis / REVIEW_INPUT_NAME, payload)
    return {"candidate_count": len(candidates), "path": str(analysis / REVIEW_INPUT_NAME)}


def build_catalog(workspace: Path, selection_path: Path, dictionary_path: Path) -> dict[str, int]:
    """Write one authoritative card per finalized vocabulary lemma."""

    resolved = workspace.expanduser().resolve()
    analysis = resolved / "analysis"
    glossary = load_dictionary_glosses(dictionary_path)
    selection = read_json(selection_path.expanduser().resolve())
    if not isinstance(selection, dict):
        raise ValueError("Learning-asset review must be an object")
    reviewed = _review_glosses(selection)
    papers = _paper_index(resolved)
    vocabulary = _load_jsonl(analysis / "vocabulary-map.jsonl")
    cards: list[dict[str, Any]] = []
    ecdict_count = 0
    agent_count = 0
    english_count = 0
    seen: set[str] = set()
    for word in vocabulary:
        lemma = _normalize(word.get("lemma"))
        pos = str(word.get("part_of_speech") or "").strip().lower()
        if not lemma or not WORD_PATTERN.fullmatch(lemma):
            raise ValueError("Finalized vocabulary contains an invalid lemma")
        identifier = card_id(lemma, pos)
        if identifier in seen:
            raise ValueError(f"Vocabulary card identity is duplicated: {identifier}")
        seen.add(identifier)
        dictionary_gloss = select_dictionary_gloss(glossary, lemma, pos)
        if dictionary_gloss is not None:
            meaning_en = dictionary_gloss.meaning_en
            meaning_zh = dictionary_gloss.meaning_zh
            origin = "ecdict"
            ecdict_count += 1
        else:
            agent_gloss = reviewed.get(lemma)
            if agent_gloss is None:
                raise ValueError(f"Vocabulary card gloss is unresolved: {lemma}")
            meaning_en = agent_gloss["meaning_en"]
            meaning_zh = agent_gloss["meaning_zh"]
            origin = "agent"
            agent_count += 1
        source = _source_for_word(word, papers)
        if meaning_en:
            english_count += 1
        cards.append(
            {
                "card_id": identifier,
                "sense_key": identifier,
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
    write_jsonl(analysis / CATALOG_NAME, cards)
    result = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "card_count": len(cards),
        "ecdict_count": ecdict_count,
        "agent_count": agent_count,
        "english_count": english_count,
    }
    write_json(analysis / SUMMARY_NAME, result)
    return {key: int(value) for key, value in result.items() if key.endswith("_count")}


def load_catalog(workspace: Path) -> list[dict[str, Any]]:
    path = workspace.expanduser().resolve() / "analysis" / CATALOG_NAME
    cards = _load_jsonl(path)
    identifiers = [str(card.get("card_id") or "") for card in cards]
    if not cards or len(identifiers) != len(set(identifiers)):
        raise ValueError(f"AreaDay vocabulary card catalog is invalid: {path}")
    for card in cards:
        required = ("card_id", "sense_key", "lemma", "meaning_zh", "context", "source_title", "source_url")
        if not all(str(card.get(field) or "").strip() for field in required):
            raise ValueError(f"AreaDay vocabulary card is incomplete: {card.get('lemma')}")
    return cards
