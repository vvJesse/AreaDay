"""Build corpus-derived lemma and terminology records with local spaCy."""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict, deque
from typing import Any, Iterable


SHARED_TERMINOLOGY_MIN_DOCUMENT_SHARE = 0.10
SPACY_PIPE_BATCH_SIZE = 4


def select_shared_terminology_candidates(
    candidates: list[dict[str, Any]],
    included_document_count: int,
    *,
    minimum_document_share: float = SHARED_TERMINOLOGY_MIN_DOCUMENT_SHARE,
) -> tuple[list[dict[str, Any]], int]:
    """Keep phrases used across enough papers to represent shared field language."""
    if included_document_count < 1:
        return [], 0
    minimum_document_count = max(
        1, math.ceil(included_document_count * minimum_document_share)
    )
    selected = [
        dict(candidate)
        for candidate in candidates
        if int(candidate.get("document_count") or 0) >= minimum_document_count
    ]
    return selected, minimum_document_count


VOCAB_POS = frozenset({"NOUN", "VERB", "ADJ", "ADV"})
TERM_POS = frozenset({"NOUN", "PROPN", "ADJ"})
TERM_CONNECTORS = frozenset({"of", "for", "to", "in", "on", "with", "from", "by"})
LEMMA_RE = re.compile(r"^[a-z][a-z'-]*$")
ACRONYM_RE = re.compile(
    r"\b([A-Za-z][A-Za-z-]*(?:\s+(?:of|for|to|in|on|with|and|[A-Za-z][A-Za-z-]*)){1,8})"
    r"\s*\(([A-Z][A-Z0-9-]{1,12})\)"
)


def load_spacy_pipeline():
    import spacy

    pipeline = spacy.load("en_core_web_sm", disable=["ner"])
    pipeline.max_length = max(pipeline.max_length, 2_000_000)
    return pipeline


def text_chunks(text: str, maximum_characters: int = 350_000) -> Iterable[str]:
    paragraphs = text.split("\n\n")
    current: list[str] = []
    current_size = 0
    for paragraph in paragraphs:
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        if len(paragraph) > maximum_characters:
            if current:
                yield "\n\n".join(current)
                current = []
                current_size = 0
            for start in range(0, len(paragraph), maximum_characters):
                yield paragraph[start : start + maximum_characters]
            continue
        additional = len(paragraph) + (2 if current else 0)
        if current and current_size + additional > maximum_characters:
            yield "\n\n".join(current)
            current = []
            current_size = 0
        current.append(paragraph)
        current_size += additional
    if current:
        yield "\n\n".join(current)


def juilland_dispersion(document_counts: list[int]) -> float:
    count = len(document_counts)
    if count <= 1:
        return 1.0 if sum(document_counts) else 0.0
    mean = sum(document_counts) / count
    if mean == 0:
        return 0.0
    variance = sum((value - mean) ** 2 for value in document_counts) / count
    coefficient = math.sqrt(variance) / mean
    return max(0.0, min(1.0, 1.0 - coefficient / math.sqrt(count - 1)))


def _sentence_text(sentence: Any) -> str:
    return re.sub(r"\s+", " ", sentence.text).strip()


def _term_from_span(span: Any) -> tuple[str, str] | None:
    tokens = list(span)
    while tokens and (tokens[0].is_stop or tokens[0].is_punct):
        tokens.pop(0)
    while tokens and (tokens[-1].is_stop or tokens[-1].is_punct):
        tokens.pop()
    if not 2 <= len(tokens) <= 6:
        return None

    canonical_tokens: list[str] = []
    content_count = 0
    for token in tokens:
        lower = token.text.casefold()
        if token.pos_ in TERM_POS and token.is_alpha:
            canonical = (
                token.text.casefold()
                if token.pos_ == "PROPN"
                else (token.lemma_ or token.text).casefold()
            )
            canonical_tokens.append(canonical)
            content_count += 1
        elif lower in TERM_CONNECTORS and canonical_tokens:
            canonical_tokens.append(lower)
        elif token.text == "-" and canonical_tokens:
            canonical_tokens.append("-")
        else:
            return None
    if content_count < 2 or canonical_tokens[-1] in TERM_CONNECTORS:
        return None
    canonical = " ".join(canonical_tokens).replace(" - ", "-")
    surface = re.sub(r"\s+", " ", span.text).strip()
    if len(canonical) < 5 or not any(character.isalpha() for character in canonical):
        return None
    return canonical, surface


def _add_example(
    examples: dict[str, list[dict[str, str]]],
    key: str,
    sentence: str,
    work_id: str,
    *,
    limit: int = 3,
) -> None:
    if not 35 <= len(sentence) <= 500:
        return
    records = examples[key]
    if len(records) >= limit:
        return
    if any(record["sentence"] == sentence for record in records):
        return
    records.append({"openalex_id": work_id, "sentence": sentence})


def build_lexical_assets(
    documents: list[dict[str, Any]],
    *,
    nlp: Any | None = None,
) -> dict[str, Any]:
    """Create lemma records and unreviewed multiword terminology candidates."""
    nlp = nlp or load_spacy_pipeline()
    lemma_by_document: dict[str, Counter[str]] = {}
    lemma_surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    lemma_pos: dict[str, Counter[str]] = defaultdict(Counter)
    lemma_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    term_by_document: dict[str, Counter[str]] = {}
    term_surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    term_examples: dict[str, list[dict[str, str]]] = defaultdict(list)
    acronym_expansions: dict[str, Counter[str]] = defaultdict(Counter)
    total_processed_tokens = 0

    document_work_ids = [str(document["openalex_id"]) for document in documents]
    document_lemma_counts = [Counter() for _ in documents]
    document_term_counts = [Counter() for _ in documents]

    pending_document_indexes: deque[int] = deque()

    def chunk_stream() -> Iterable[str]:
        for document_index, document in enumerate(documents):
            for raw_chunk in text_chunks(str(document.get("clean_text") or "")):
                for expansion, acronym in ACRONYM_RE.findall(raw_chunk):
                    cleaned_expansion = re.sub(r"\s+", " ", expansion).strip().casefold()
                    acronym_expansions[acronym][cleaned_expansion] += 1
                pending_document_indexes.append(document_index)
                yield raw_chunk

    parsed_chunks = nlp.pipe(
        chunk_stream(),
        batch_size=SPACY_PIPE_BATCH_SIZE,
        n_process=1,
    )
    for parsed in parsed_chunks:
        document_index = pending_document_indexes.popleft()
        work_id = document_work_ids[document_index]
        document_lemmas = document_lemma_counts[document_index]
        document_terms = document_term_counts[document_index]
        total_processed_tokens += len(parsed)
        for token in parsed:
            if (
                token.pos_ not in VOCAB_POS
                or token.is_stop
                or not token.is_alpha
                or token.like_num
            ):
                continue
            lemma = (token.lemma_ or token.text).casefold().strip("'-")
            if len(lemma) < 2 or not LEMMA_RE.fullmatch(lemma):
                continue
            document_lemmas[lemma] += 1
            lemma_surfaces[lemma][token.text] += 1
            lemma_pos[lemma][token.pos_] += 1

        sentence_text_cache: dict[tuple[int, int], str] = {}

        def cached_sentence_text(sentence: Any) -> str:
            key = (
                int(getattr(sentence, "start", id(sentence))),
                int(getattr(sentence, "end", id(sentence))),
            )
            if key not in sentence_text_cache:
                sentence_text_cache[key] = _sentence_text(sentence)
            return sentence_text_cache[key]

        for sentence in parsed.sents:
            sentence_text = cached_sentence_text(sentence)
            sentence_lemmas = {
                (token.lemma_ or token.text).casefold().strip("'-")
                for token in sentence
                if token.pos_ in VOCAB_POS
                and not token.is_stop
                and token.is_alpha
                and not token.like_num
            }
            for lemma in sentence_lemmas:
                if LEMMA_RE.fullmatch(lemma):
                    _add_example(lemma_examples, lemma, sentence_text, work_id)

        for chunk in parsed.noun_chunks:
            normalized = _term_from_span(chunk)
            if normalized is None:
                continue
            canonical, surface = normalized
            document_terms[canonical] += 1
            term_surfaces[canonical][surface] += 1
            _add_example(
                term_examples,
                canonical,
                cached_sentence_text(chunk.sent),
                work_id,
            )

    for work_id, document_lemmas, document_terms in zip(
        document_work_ids,
        document_lemma_counts,
        document_term_counts,
    ):
        lemma_by_document[work_id] = document_lemmas
        term_by_document[work_id] = document_terms

    work_ids = list(lemma_by_document)
    total_lemma_tokens = sum(sum(counts.values()) for counts in lemma_by_document.values())
    minimum_documents = 2 if len(work_ids) >= 3 else 1
    minimum_total_count = 3 if len(work_ids) >= 3 else 2
    vocabulary: list[dict[str, Any]] = []
    all_lemmas = set().union(*(counts.keys() for counts in lemma_by_document.values())) if work_ids else set()
    for lemma in all_lemmas:
        per_document = [lemma_by_document[work_id][lemma] for work_id in work_ids]
        total_count = sum(per_document)
        document_count = sum(value > 0 for value in per_document)
        if total_count < minimum_total_count or document_count < minimum_documents:
            continue
        surface_forms = [
            {"form": form, "count": count}
            for form, count in lemma_surfaces[lemma].most_common(8)
        ]
        source_papers = [work_id for work_id in work_ids if lemma_by_document[work_id][lemma]]
        vocabulary.append(
            {
                "lemma": lemma,
                "part_of_speech": lemma_pos[lemma].most_common(1)[0][0],
                "total_count": total_count,
                "frequency_per_million": round(
                    total_count * 1_000_000 / total_lemma_tokens, 3
                )
                if total_lemma_tokens
                else 0.0,
                "document_count": document_count,
                "document_share": round(document_count / len(work_ids), 6),
                "dispersion": round(juilland_dispersion(per_document), 6),
                "per_document_counts": {
                    work_id: lemma_by_document[work_id][lemma]
                    for work_id in work_ids
                    if lemma_by_document[work_id][lemma]
                },
                "surface_forms": surface_forms,
                "representative_sentences": lemma_examples[lemma],
                "source_papers": source_papers,
            }
        )
    vocabulary.sort(
        key=lambda item: (
            -item["document_count"],
            -item["dispersion"],
            -item["total_count"],
            item["lemma"],
        )
    )

    term_document_counts: Counter[str] = Counter()
    term_total_counts: Counter[str] = Counter()
    for counts in term_by_document.values():
        term_total_counts.update(counts)
        term_document_counts.update(counts.keys())

    eligible_terms = {
        term
        for term, count in term_total_counts.items()
        if count >= 2 and term_document_counts[term] >= minimum_documents
    }
    parent_frequencies: dict[str, list[int]] = defaultdict(list)
    for parent in eligible_terms:
        parent_tokens = parent.split()
        if len(parent_tokens) <= 2:
            continue
        for length in range(2, len(parent_tokens)):
            for start in range(0, len(parent_tokens) - length + 1):
                child = " ".join(parent_tokens[start : start + length])
                if child in eligible_terms:
                    parent_frequencies[child].append(term_total_counts[parent])

    terminology: list[dict[str, Any]] = []
    for term in eligible_terms:
        count = term_total_counts[term]
        nested = parent_frequencies.get(term) or []
        adjusted = count - (sum(nested) / len(nested) if nested else 0)
        c_value = max(0.0, math.log2(len(term.split())) * adjusted)
        aliases = [
            acronym
            for acronym, expansions in acronym_expansions.items()
            if term in expansions
        ]
        terminology.append(
            {
                "term": term,
                "total_count": count,
                "document_count": term_document_counts[term],
                "document_share": round(term_document_counts[term] / len(work_ids), 6),
                "c_value": round(c_value, 6),
                "surface_forms": [
                    {"form": form, "count": surface_count}
                    for form, surface_count in term_surfaces[term].most_common(6)
                ],
                "acronyms": sorted(aliases),
                "representative_sentences": term_examples[term],
                "source_papers": [
                    work_id for work_id in work_ids if term_by_document[work_id][term]
                ],
            }
        )
    terminology.sort(
        key=lambda item: (
            -item["document_count"],
            -item["c_value"],
            -item["total_count"],
            item["term"],
        )
    )
    return {
        "vocabulary": vocabulary,
        "terminology_candidates": terminology,
        "included_document_count": len(work_ids),
        "processed_spacy_token_count": total_processed_tokens,
        "content_lemma_token_count": total_lemma_tokens,
        "minimum_document_count": minimum_documents,
    }
