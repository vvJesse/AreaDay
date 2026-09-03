"""Version deduplication and conservative profile-relevance filtering."""

from __future__ import annotations

import re
from typing import Any, Callable

import numpy as np

from onnx_embeddings import embed_texts


def normalize_title(title: str | None) -> str:
    if not title:
        return ""
    normalized = title.casefold()
    normalized = re.sub(r"\b(?:preprint|accepted manuscript|author version)\b", " ", normalized)
    normalized = re.sub(r"[^a-z0-9]+", " ", normalized)
    return " ".join(normalized.split())


def normalize_identifier(value: str | None) -> str:
    if not value:
        return ""
    normalized = value.casefold().strip()
    normalized = re.sub(r"^https?://doi\.org/", "", normalized)
    normalized = re.sub(r"^doi:\s*", "", normalized)
    normalized = re.sub(r"v\d+$", "", normalized)
    return normalized


def title_token_jaccard(first: str | None, second: str | None) -> float:
    first_tokens = set(normalize_title(first).split())
    second_tokens = set(normalize_title(second).split())
    if not first_tokens or not second_tokens:
        return 0.0
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


def profile_anchors(profile: dict[str, Any] | None) -> list[str]:
    if not profile:
        return []
    title = str(profile.get("title") or "").strip()
    queries = [
        str(item.get("query") if isinstance(item, dict) else item).strip()
        for item in profile.get("search_queries") or []
    ]
    queries = [query for query in queries if query]
    summary = " ".join([title, *queries]).strip()
    anchors = [summary] if summary else []
    anchors.extend(queries)
    return anchors


def metadata_text(document: dict[str, Any]) -> str:
    title = str(document.get("title") or "").strip()
    abstract = str(document.get("abstract") or "").strip()
    return f"{title}. {abstract}".strip()


def _exact_keys(document: dict[str, Any]) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    doi = normalize_identifier(document.get("doi"))
    arxiv_id = normalize_identifier(document.get("arxiv_id"))
    title = normalize_title(document.get("title"))
    if doi:
        keys.append(("doi", doi))
    if arxiv_id:
        keys.append(("arxiv", arxiv_id))
    if title:
        keys.append(("title", title))
    return keys


def select_analysis_documents(
    documents: list[dict[str, Any]],
    profile: dict[str, Any] | None,
    *,
    embedding_fn: Callable[[list[str]], np.ndarray] = embed_texts,
) -> dict[str, Any]:
    """Mark, but never delete, duplicate and clearly off-profile documents."""
    extracted = [document for document in documents if document.get("status") == "extracted"]
    for document in extracted:
        document["analysis_decision"] = "include"
        document["analysis_reasons"] = []
        document["duplicate_of"] = None

    if not extracted:
        return {
            "included": [],
            "duplicate_count": 0,
            "low_relevance_count": 0,
            "relevance_cutoff": None,
        }

    anchors = profile_anchors(profile)
    embeddings: np.ndarray | None = None
    anchor_count = 0
    if anchors:
        combined = anchors + [metadata_text(document) for document in extracted]
        embedded = embedding_fn(combined)
        anchor_count = len(anchors)
        anchor_embeddings = embedded[:anchor_count]
        embeddings = embedded[anchor_count:]
        summary_scores = embeddings @ anchor_embeddings[0]
        if anchor_count > 1:
            query_scores = embeddings @ anchor_embeddings[1:].T
            top_count = min(3, query_scores.shape[1])
            top_query_means = np.sort(query_scores, axis=1)[:, -top_count:].mean(axis=1)
            relevance_scores = 0.6 * summary_scores + 0.4 * top_query_means
        else:
            relevance_scores = summary_scores
        for document, score in zip(extracted, relevance_scores, strict=True):
            document["relevance_score"] = round(float(score), 6)
    else:
        for document in extracted:
            document["relevance_score"] = None

    # Keep the best-extracted version when exact identifiers or titles collide.
    ranked_indexes = sorted(
        range(len(extracted)),
        key=lambda index: (
            -int(extracted[index].get("body_word_count") or 0),
            int(extracted[index].get("discovery_order") or 10**9),
        ),
    )
    key_owner: dict[tuple[str, str], int] = {}
    for index in ranked_indexes:
        document = extracted[index]
        duplicate_matches = [key_owner[key] for key in _exact_keys(document) if key in key_owner]
        if duplicate_matches:
            owner_index = duplicate_matches[0]
            document["analysis_decision"] = "duplicate"
            document["duplicate_of"] = extracted[owner_index]["openalex_id"]
            document["analysis_reasons"].append("exact identifier or normalized-title match")
            continue
        for key in _exact_keys(document):
            key_owner[key] = index

    # Embeddings only nominate a near-version when the normalized titles also nearly match.
    if embeddings is not None:
        kept_indexes: list[int] = []
        for index in ranked_indexes:
            document = extracted[index]
            if document["analysis_decision"] == "duplicate":
                continue
            matched_index: int | None = None
            for owner_index in kept_indexes:
                title_overlap = title_token_jaccard(
                    document.get("title"), extracted[owner_index].get("title")
                )
                similarity = float(embeddings[index] @ embeddings[owner_index])
                if title_overlap >= 0.85 and similarity >= 0.97:
                    matched_index = owner_index
                    document["near_duplicate_similarity"] = round(similarity, 6)
                    document["near_duplicate_title_overlap"] = round(title_overlap, 6)
                    break
            if matched_index is None:
                kept_indexes.append(index)
            else:
                document["analysis_decision"] = "duplicate"
                document["duplicate_of"] = extracted[matched_index]["openalex_id"]
                document["analysis_reasons"].append(
                    "near-identical title and title/abstract embedding"
                )

    relevance_cutoff: float | None = None
    relevance_pool = [
        document
        for document in extracted
        if document["analysis_decision"] == "include"
        and len(str(document.get("abstract") or "").split()) >= 20
        and document.get("relevance_score") is not None
    ]
    if len(relevance_pool) >= 8:
        scores = np.asarray([document["relevance_score"] for document in relevance_pool])
        relevance_cutoff = float(min(0.22, np.quantile(scores, 0.10)))
        for document in relevance_pool:
            if float(document["relevance_score"]) < relevance_cutoff:
                document["analysis_decision"] = "low-relevance"
                document["analysis_reasons"].append(
                    "extreme low title/abstract similarity to the confirmed research profile"
                )

    included = [
        document for document in extracted if document["analysis_decision"] == "include"
    ]
    return {
        "included": included,
        "duplicate_count": sum(
            document["analysis_decision"] == "duplicate" for document in extracted
        ),
        "low_relevance_count": sum(
            document["analysis_decision"] == "low-relevance" for document in extracted
        ),
        "relevance_cutoff": (
            round(relevance_cutoff, 6) if relevance_cutoff is not None else None
        ),
        "embedding_anchor_count": anchor_count,
    }
