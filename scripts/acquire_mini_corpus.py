#!/usr/bin/env python3
"""Acquire a fresh local mini corpus through configured scholarly providers."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

import requests

from arxiv_client import make_arxiv_client
from candidate_review import build_candidate_review_packet
from concurrent_downloads import (
    DEFAULT_MAX_DOWNLOADS,
    DEFAULT_MAX_DOWNLOADS_PER_HOST,
    download_candidates_concurrently,
)
from provider_discovery import collect_provider_lanes
from run_timing import RunTimeline
from corpus_analysis import analyze_corpus
from domain_registry import DomainRegistry, default_registry_path
from research_profile import validate_profile
from researchramp_core import (
    OpenAlexClient,
    candidate_from_openalex_work,
    load_openalex_api_key,
    normalize_doi,
    read_json,
    stable_hash,
    utc_now,
    write_json,
    write_jsonl,
)


SKILL_DIR = Path(__file__).resolve().parents[1]
S2_SEARCH_URL = "https://api.semanticscholar.org/graph/v1/paper/search"
S2_FIELDS = (
    "paperId,title,abstract,year,externalIds,openAccessPdf,isOpenAccess,"
    "fieldsOfStudy,publicationTypes,venue,publicationDate,citationCount"
)
ARXIV_ID_RE = re.compile(
    r"(?:arxiv(?:\.org/(?:abs|pdf)/|:)|10\.48550/arxiv\.)"
    r"((?:[a-z][a-z.\-]+/\d{7})|(?:\d{4}\.\d{4,5}))(?:v\d+)?",
    re.IGNORECASE,
)
ARXIV_CANDIDATE_CACHE_KIND = "researchramp.arxiv.candidates"
ARXIV_CANDIDATE_CACHE_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class SearchAttempt:
    query_id: str
    provider: str
    status: str
    returned: int
    message: str | None = None


def _title_key(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def _title_is_excluded(title: str, prefixes: Iterable[str]) -> bool:
    normalized = _title_key(title)
    return any(normalized.startswith(_title_key(prefix)) for prefix in prefixes)


def _safe_arxiv_phrase(value: str) -> str:
    phrase = re.sub(r'["\r\n]+', " ", value).strip()
    if not phrase:
        raise ValueError("arXiv phrase cannot be empty")
    return phrase


def build_arxiv_query(query: dict[str, Any], scope: dict[str, Any]) -> str:
    """Build a title/abstract, category, and confirmed-date constrained query."""
    phrases = query.get("phrases") or ([query["query"]] if query.get("query") else [])
    if not phrases:
        raise ValueError(f"{query.get('id', 'arXiv query')} has no phrases")
    text_parts = []
    for raw_phrase in phrases:
        phrase = _safe_arxiv_phrase(str(raw_phrase))
        text_parts.append(f'(ti:"{phrase}" OR abs:"{phrase}")')
    text_clause = "(" + " OR ".join(text_parts) + ")"

    categories = query.get("categories") or scope.get("arxiv_categories") or []
    if not categories:
        raise ValueError(f"{query.get('id', 'arXiv query')} has no categories")
    category_clause = "(" + " OR ".join(f"cat:{item}" for item in categories) + ")"

    lane = query.get("date_lane", "recent")
    current_year = datetime.now(UTC).year
    if lane == "recent":
        start_year = int(scope["recent_from_year"])
        end_year = current_year
    elif lane == "foundation":
        start_year = int(scope["foundation_from_year"])
        end_year = int(scope["foundation_before_year"]) - 1
    else:
        raise ValueError(f"Unsupported date_lane: {lane}")
    date_clause = (
        f"submittedDate:[{start_year}01010000 TO {end_year}12312359]"
    )
    return f"{text_clause} AND {category_clause} AND {date_clause}"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _arxiv_id_from_paper(paper: dict[str, Any]) -> str | None:
    external = paper.get("externalIds") or {}
    explicit = external.get("ArXiv")
    if explicit:
        return re.sub(r"v\d+$", "", str(explicit), flags=re.I)
    pdf_url = str((paper.get("openAccessPdf") or {}).get("url") or "")
    match = ARXIV_ID_RE.search(pdf_url)
    return match.group(1) if match else None


def _candidate_from_s2(
    paper: dict[str, Any],
    query: dict[str, str],
    *,
    excluded_title_prefixes: Iterable[str] = (),
) -> dict[str, Any] | None:
    pdf = paper.get("openAccessPdf") or {}
    pdf_url = str(pdf.get("url") or "").strip()
    if (
        not paper.get("paperId")
        or not paper.get("title")
        or not pdf_url
        or _title_is_excluded(str(paper["title"]), excluded_title_prefixes)
    ):
        return None
    external = paper.get("externalIds") or {}
    return {
        "candidate_id": f"S2:{paper['paperId']}",
        "provider": "semantic-scholar",
        "title": paper["title"],
        "abstract": paper.get("abstract") or "",
        "year": paper.get("year"),
        "publication_date": paper.get("publicationDate"),
        "venue": paper.get("venue") or "",
        "doi": normalize_doi(external.get("DOI")),
        "arxiv_id": _arxiv_id_from_paper(paper),
        "semantic_scholar_id": paper["paperId"],
        "fields_of_study": paper.get("fieldsOfStudy") or [],
        "publication_types": paper.get("publicationTypes") or [],
        "citation_count": paper.get("citationCount") or 0,
        "pdf_url": pdf_url,
        "pdf_status": pdf.get("status"),
        "query_hits": [
            {
                "provider": "semantic-scholar",
                "query_id": query["id"],
                "label": query["label"],
                "query": query["query"],
            }
        ],
    }


class SemanticScholarClient:
    def __init__(
        self,
        *,
        api_key: str | None,
        timeout: int = 90,
        max_attempts: int = 5,
    ) -> None:
        self.api_key = api_key
        self.timeout = timeout
        self.max_attempts = max_attempts if api_key else 1
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "ResearchRamp/0.1 (local academic research client)",
                "Accept": "application/json",
            }
        )
        if api_key:
            self.session.headers["x-api-key"] = api_key

    def search(
        self,
        query: str,
        *,
        year: str,
        fields_of_study: Iterable[str],
        publication_types: Iterable[str],
        limit: int,
    ) -> list[dict[str, Any]]:
        params = {
            "query": query,
            "fields": S2_FIELDS,
            "openAccessPdf": "",
            "year": year,
            "fieldsOfStudy": ",".join(fields_of_study),
            "publicationTypes": ",".join(publication_types),
            "limit": str(limit),
        }
        last_error = "unknown error"
        for attempt in range(self.max_attempts):
            response = self.session.get(
                S2_SEARCH_URL,
                params=params,
                timeout=self.timeout,
            )
            if response.status_code == 200:
                payload = response.json()
                return list(payload.get("data") or [])
            if response.status_code != 429:
                response.raise_for_status()
            last_error = response.text[:500]
            if attempt + 1 < self.max_attempts:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else min(30, 5 * (2**attempt))
                time.sleep(max(1.0, delay))
        raise RuntimeError(f"Semantic Scholar remained rate limited: {last_error}")


def collect_semantic_scholar_candidates(
    queries: Iterable[dict[str, str]],
    *,
    api_key: str | None,
    inter_request_seconds: float,
    scope: dict[str, Any],
    max_results_per_query: int,
) -> tuple[list[dict[str, Any]], list[SearchAttempt]]:
    client = SemanticScholarClient(api_key=api_key)
    by_identity: dict[str, dict[str, Any]] = {}
    attempts: list[SearchAttempt] = []
    query_list = list(queries)
    per_query_results: list[list[dict[str, Any]]] = []
    for index, query in enumerate(query_list):
        try:
            results = client.search(
                query["query"],
                year=f"{scope['recent_from_year']}-",
                fields_of_study=scope["semantic_scholar_fields"],
                publication_types=scope["publication_types"],
                limit=max_results_per_query,
            )
            attempts.append(
                SearchAttempt(query["id"], "semantic-scholar", "ok", len(results))
            )
        except Exception as error:
            attempts.append(
                SearchAttempt(
                    query["id"],
                    "semantic-scholar",
                    "failed",
                    0,
                    f"{type(error).__name__}: {error}",
                )
            )
            results = []
        per_query_results.append(
            [
                candidate
                for raw in results
                if (
                    candidate := _candidate_from_s2(
                        raw,
                        query,
                        excluded_title_prefixes=scope["exclude_title_prefixes"],
                    )
                )
                is not None
            ]
        )
        if index + 1 < len(query_list) and inter_request_seconds:
            time.sleep(inter_request_seconds)

    for rank in range(max((len(items) for items in per_query_results), default=0)):
        for items in per_query_results:
            if rank >= len(items):
                continue
            candidate = items[rank]
            identity = (
                f"doi:{candidate['doi']}"
                if candidate.get("doi")
                else f"s2:{candidate['semantic_scholar_id']}"
            )
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = candidate
            else:
                known_hits = {item["query_id"] for item in existing["query_hits"]}
                existing["query_hits"].extend(
                    item
                    for item in candidate["query_hits"]
                    if item["query_id"] not in known_hits
                )
    return list(by_identity.values()), attempts


def _candidate_from_openalex(
    work: dict[str, Any],
    query: dict[str, str],
    *,
    api_key_configured: bool,
    excluded_title_prefixes: Iterable[str] = (),
) -> dict[str, Any] | None:
    normalized = candidate_from_openalex_work(work)
    if normalized is None or _title_is_excluded(
        str(normalized["title"]), excluded_title_prefixes
    ):
        return None
    locations = [
        location
        for location in normalized.get("oa_locations") or []
        if str(location.get("pdf_url") or "").startswith(("https://", "http://"))
    ]
    direct_urls = []
    for location in locations:
        url = str(location["pdf_url"])
        if url.startswith("http://"):
            url = "https://" + url.removeprefix("http://")
        direct_urls.append(
            {
                "provider": "openalex-oa-location",
                "url": url,
                "license": location.get("license"),
                "source": location.get("source"),
            }
        )
    has_content = bool(normalized.get("has_openalex_pdf"))
    if not direct_urls and not normalized.get("arxiv_id") and not (
        api_key_configured and has_content
    ):
        return None
    primary = direct_urls[0]["url"] if direct_urls else ""
    return {
        "candidate_id": f"OpenAlex:{normalized['openalex_id']}",
        "provider": "openalex",
        "title": normalized["title"],
        "abstract": normalized.get("abstract") or "",
        "year": normalized.get("publication_year"),
        "publication_date": normalized.get("publication_date"),
        "venue": normalized.get("source") or "",
        "doi": normalized.get("doi"),
        "arxiv_id": normalized.get("arxiv_id"),
        "semantic_scholar_id": None,
        "openalex_id": normalized["openalex_id"],
        "fields_of_study": normalized.get("topics") or [],
        "primary_topic": normalized.get("primary_topic"),
        "topic_metadata": normalized.get("topic_metadata") or [],
        "publication_types": [normalized["type"]] if normalized.get("type") else [],
        "citation_count": normalized.get("cited_by_count") or 0,
        "pdf_url": primary,
        "pdf_status": normalized.get("open_access_status"),
        "has_openalex_pdf": has_content,
        "alternate_pdf_urls": direct_urls[1:],
        "query_hits": [
            {
                "provider": "openalex",
                "query_id": query["id"],
                "label": query["label"],
                "query": query["query"],
            }
        ],
    }


def _openalex_primary_filter(scope: dict[str, Any]) -> tuple[str, str, list[str]]:
    configured = scope["openalex_primary_filter"]
    level = str(configured["level"])
    ids = [str(item).rsplit("/", 1)[-1] for item in configured["ids"]]
    filter_name = {
        "domain": "primary_topic.domain.id",
        "field": "primary_topic.field.id",
        "subfield": "primary_topic.subfield.id",
        "topic": "primary_topic.id",
    }[level]
    return filter_name, "|".join(ids), ids


def collect_openalex_candidates(
    queries: Iterable[dict[str, str]],
    *,
    api_key: str | None,
    cache_dir: Path,
    scope: dict[str, Any],
    max_results_per_query: int,
    refresh: bool = True,
) -> tuple[list[dict[str, Any]], list[SearchAttempt]]:
    """Search OpenAlex with or without a key and interleave query result lists."""
    client = OpenAlexClient(
        cache_dir,
        api_key=api_key,
        refresh=refresh,
    )
    current_year = datetime.now(UTC).year
    discipline_filter, discipline_ids, configured_ids = _openalex_primary_filter(scope)
    from_date = str(
        scope.get("publication_from_date")
        or f"{int(scope['foundation_from_year'])}-01-01"
    )
    to_date = str(scope.get("publication_to_date") or f"{current_year}-12-31")
    filters = ",".join(
        [
            f"from_publication_date:{from_date}",
            f"to_publication_date:{to_date}",
            f"language:{scope.get('language', 'en')}",
            "is_oa:true",
            f"{discipline_filter}:{discipline_ids}",
        ]
    )
    attempts: list[SearchAttempt] = []
    per_query_candidates: list[list[dict[str, Any]]] = []
    for query in queries:
        try:
            response = client.search(
                query["query"], per_page=max_results_per_query, filters=filters
            )
            results = list(response.get("results") or [])
            attempts.append(SearchAttempt(query["id"], "openalex", "ok", len(results)))
        except Exception as error:
            attempts.append(
                SearchAttempt(
                    query["id"],
                    "openalex",
                    "failed",
                    0,
                    f"{type(error).__name__}: {error}",
                )
            )
            results = []
        query_candidates: list[dict[str, Any]] = []
        for work in results:
            candidate = _candidate_from_openalex(
                work,
                query,
                api_key_configured=bool(api_key),
                excluded_title_prefixes=scope["exclude_title_prefixes"],
            )
            if candidate is None:
                continue
            candidate["discipline_constraint"] = {
                "provider": "openalex",
                "primary_topic_level": scope["openalex_primary_filter"]["level"],
                "ids": configured_ids,
                "labels": list(scope["openalex_primary_filter"]["labels"]),
            }
            query_candidates.append(candidate)
        per_query_candidates.append(query_candidates)

    by_identity: dict[str, dict[str, Any]] = {}
    for rank in range(max((len(items) for items in per_query_candidates), default=0)):
        for items in per_query_candidates:
            if rank >= len(items):
                continue
            candidate = items[rank]
            identity = str(candidate["openalex_id"])
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = candidate
                continue
            known_hits = {item["query_id"] for item in existing["query_hits"]}
            existing["query_hits"].extend(
                item for item in candidate["query_hits"] if item["query_id"] not in known_hits
            )
    return list(by_identity.values()), attempts


def collect_arxiv_candidates(
    queries: Iterable[dict[str, Any]],
    *,
    max_results_per_query: int,
    scope: dict[str, Any],
    cache_dir: Path | None = None,
    refresh: bool = True,
) -> tuple[list[dict[str, Any]], list[SearchAttempt]]:
    import arxiv

    client = make_arxiv_client(
        page_size=max_results_per_query,
        delay_seconds=3,
        num_retries=2,
        request_timeout_seconds=30,
    )
    by_identity: dict[str, dict[str, Any]] = {}
    attempts: list[SearchAttempt] = []
    per_query_candidates: list[list[dict[str, Any]]] = []
    for query in queries:
        provider_query = build_arxiv_query(query, scope)
        cache_identity = {
            "kind": ARXIV_CANDIDATE_CACHE_KIND,
            "schema_version": ARXIV_CANDIDATE_CACHE_SCHEMA_VERSION,
            "provider_query": provider_query,
            "max_results": max_results_per_query,
            "sort": "relevance",
            "query_metadata": {
                "id": str(query["id"]),
                "label": str(query["label"]),
                "date_lane": str(query.get("date_lane", "recent")),
                "categories": [
                    str(value) for value in (query.get("categories") or [])
                ],
            },
            "exclude_title_prefixes": [
                str(value) for value in scope["exclude_title_prefixes"]
            ],
        }
        cache_path = (
            cache_dir
            / (stable_hash(cache_identity, 24) + ".json")
            if cache_dir is not None
            else None
        )
        query_candidates: list[dict[str, Any]] | None = None
        if cache_path is not None and cache_path.is_file() and not refresh:
            try:
                cached = read_json(cache_path)
                cached_candidates = cached.get("candidates")
                if (
                    cached.get("cache") == cache_identity
                    and isinstance(cached_candidates, list)
                    and all(
                        isinstance(item, dict)
                        and item.get("provider") == "arxiv"
                        and item.get("candidate_id")
                        for item in cached_candidates
                    )
                ):
                    query_candidates = cached_candidates
            except (OSError, UnicodeError, json.JSONDecodeError):
                query_candidates = None

        if query_candidates is not None:
            attempts.append(
                SearchAttempt(
                    query["id"],
                    "arxiv",
                    "ok",
                    len(query_candidates),
                    "cache-hit",
                )
            )
            per_query_candidates.append(query_candidates)
            continue

        results: list[Any] = []
        try:
            search = arxiv.Search(
                query=provider_query,
                max_results=max_results_per_query,
                sort_by=arxiv.SortCriterion.Relevance,
            )
            results = list(client.results(search))
            attempts.append(SearchAttempt(query["id"], "arxiv", "ok", len(results)))
        except Exception as error:
            attempts.append(
                SearchAttempt(
                    query["id"],
                    "arxiv",
                    "failed",
                    0,
                    f"{type(error).__name__}: {error}",
                )
            )
        query_candidates = []
        for result in results:
            title = result.title.strip()
            if _title_is_excluded(title, scope["exclude_title_prefixes"]):
                continue
            arxiv_id = re.sub(r"v\d+$", "", result.get_short_id(), flags=re.I)
            pdf_url = str(result.pdf_url or "")
            if pdf_url.startswith("http://"):
                pdf_url = "https://" + pdf_url[len("http://") :]
            candidate = {
                "candidate_id": f"arXiv:{arxiv_id}",
                "provider": "arxiv",
                "title": title,
                "abstract": result.summary.strip(),
                "year": result.published.year if result.published else None,
                "publication_date": result.published.date().isoformat() if result.published else None,
                "venue": result.journal_ref or "arXiv",
                "doi": normalize_doi(result.doi),
                "arxiv_id": arxiv_id,
                "semantic_scholar_id": None,
                "fields_of_study": [item for item in result.categories],
                "publication_types": ["Preprint"],
                "citation_count": None,
                "pdf_url": pdf_url,
                "pdf_status": "ARXIV",
                "query_hits": [
                    {
                        "provider": "arxiv",
                        "query_id": query["id"],
                        "label": query["label"],
                        "query": provider_query,
                        "date_lane": query.get("date_lane", "recent"),
                    }
                ],
                "discipline_constraint": {
                    "provider": "arxiv",
                    "categories": list(query.get("categories") or []),
                },
            }
            query_candidates.append(candidate)
        per_query_candidates.append(query_candidates)
        if cache_path is not None and attempts[-1].status == "ok":
            write_json(
                cache_path,
                {
                    "cache": cache_identity,
                    "candidates": query_candidates,
                },
            )

    foundation_used = 0
    foundation_limit = int(scope.get("foundation_limit") or 0)
    for rank in range(max((len(items) for items in per_query_candidates), default=0)):
        for items in per_query_candidates:
            if rank >= len(items):
                continue
            candidate = items[rank]
            lane = candidate["query_hits"][0]["date_lane"]
            if lane == "foundation" and foundation_used >= foundation_limit:
                continue
            identity = f"arxiv:{str(candidate['arxiv_id']).casefold()}"
            existing = by_identity.get(identity)
            if existing is None:
                by_identity[identity] = candidate
                if lane == "foundation":
                    foundation_used += 1
            else:
                known_hits = {
                    (
                        str(item.get("provider") or existing["provider"]),
                        str(item["query_id"]),
                    )
                    for item in existing["query_hits"]
                }
                existing["query_hits"].extend(
                    item
                    for item in candidate["query_hits"]
                    if (
                        str(item.get("provider") or candidate["provider"]),
                        str(item["query_id"]),
                    )
                    not in known_hits
                )
    session = getattr(client, "_session", None)
    close_session = getattr(session, "close", None)
    if callable(close_session):
        close_session()
    return list(by_identity.values()), attempts


def merge_candidates(*groups: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    def conflicting_strong_identity(
        left: dict[str, Any], right: dict[str, Any]
    ) -> bool:
        for field in ("doi", "arxiv_id"):
            left_value = str(left.get(field) or "").strip().casefold()
            right_value = str(right.get(field) or "").strip().casefold()
            if left_value and right_value and left_value != right_value:
                return True
        return False

    merged: list[dict[str, Any]] = []
    identities: dict[str, int] = {}
    title_identities: dict[str, int] = {}
    for group in groups:
        for candidate in group:
            strong_keys = []
            if candidate.get("doi"):
                strong_keys.append(f"doi:{candidate['doi'].casefold()}")
            if candidate.get("arxiv_id"):
                strong_keys.append(f"arxiv:{candidate['arxiv_id'].casefold()}")
            if candidate.get("semantic_scholar_id"):
                strong_keys.append(f"s2:{candidate['semantic_scholar_id']}")
            title_key = _title_key(candidate.get("title"))
            match_index = next(
                (identities[key] for key in strong_keys if key in identities), None
            )
            if match_index is None and title_key in title_identities:
                title_match = title_identities[title_key]
                if not conflicting_strong_identity(merged[title_match], candidate):
                    match_index = title_match
            if match_index is None:
                match_index = len(merged)
                candidate = dict(candidate)
                candidate["discovery_order"] = match_index + 1
                merged.append(candidate)
            else:
                existing = merged[match_index]
                known_hits = {
                    (
                        str(item.get("provider") or existing["provider"]),
                        str(item["query_id"]),
                    )
                    for item in existing["query_hits"]
                }
                existing["query_hits"].extend(
                    item
                    for item in candidate["query_hits"]
                    if (
                        str(item.get("provider") or candidate["provider"]),
                        str(item["query_id"]),
                    )
                    not in known_hits
                )
                for field in ("doi", "openalex_id", "semantic_scholar_id", "arxiv_id"):
                    if not existing.get(field) and candidate.get(field):
                        existing[field] = candidate[field]
                if candidate.get("has_openalex_pdf"):
                    existing["has_openalex_pdf"] = True
                routes = []
                if candidate.get("pdf_url"):
                    routes.append(
                        {"provider": candidate["provider"], "url": candidate["pdf_url"]}
                    )
                routes.extend(candidate.get("alternate_pdf_urls") or [])
                known_urls = {
                    str(item.get("url"))
                    for item in existing.get("alternate_pdf_urls") or []
                    if item.get("url")
                }
                if existing.get("pdf_url"):
                    known_urls.add(str(existing["pdf_url"]))
                for route in routes:
                    url = str(route.get("url") or "")
                    if not url or url in known_urls:
                        continue
                    existing.setdefault("alternate_pdf_urls", []).append(route)
                    known_urls.add(url)
            for key in strong_keys:
                identities[key] = match_index
            if title_key:
                title_identities[title_key] = match_index
    return merged


def apply_agent_selection(
    candidates: Iterable[dict[str, Any]],
    selection_path: Path,
    review_packet_path: Path,
) -> list[dict[str, Any]]:
    """Return candidates in the exact order approved by the host agent."""
    selection = read_json(selection_path)
    if selection.get("schema_version") != 1:
        raise ValueError("selection schema_version must be 1")
    if selection.get("reviewer") != "current-host-agent":
        raise ValueError("selection reviewer must be current-host-agent")
    selected_ids = selection.get("selected_candidate_ids")
    if not isinstance(selected_ids, list) or not selected_ids:
        raise ValueError("selection must contain selected_candidate_ids")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("selection contains duplicate candidate IDs")
    by_id = {item["candidate_id"]: item for item in candidates}
    reviewed_ids = {
        item["candidate_id"] for item in _read_jsonl(review_packet_path)
    }
    unreviewed = [item for item in selected_ids if item not in reviewed_ids]
    if unreviewed:
        raise ValueError(
            f"selection contains candidates outside the review packet: {unreviewed[:5]}"
        )
    unknown = [item for item in selected_ids if item not in by_id]
    if unknown:
        raise ValueError(f"selection contains unknown candidate IDs: {unknown[:5]}")
    return [by_id[item] for item in selected_ids]


def download_candidates(
    candidates: Iterable[dict[str, Any]],
    workspace: Path,
    *,
    target_papers: int,
    openalex_api_key: str | None,
    max_downloads: int = DEFAULT_MAX_DOWNLOADS,
    max_downloads_per_host: int = DEFAULT_MAX_DOWNLOADS_PER_HOST,
) -> list[dict[str, Any]]:
    return download_candidates_concurrently(
        candidates,
        workspace,
        target_papers=target_papers,
        openalex_api_key=openalex_api_key,
        max_downloads=max_downloads,
        max_downloads_per_host=max_downloads_per_host,
    )


def minimum_usable_papers(target_papers: int) -> int:
    """Return the one corpus-viability floor used by acquisition and init."""

    if target_papers < 1:
        raise ValueError("target_papers must be positive")
    return max(1, math.ceil(target_papers * 6 / 7))


def _attempt_dict(attempt: SearchAttempt) -> dict[str, Any]:
    return {
        "query_id": attempt.query_id,
        "provider": attempt.provider,
        "status": attempt.status,
        "returned": attempt.returned,
        "message": attempt.message,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a fresh local mini corpus using OpenAlex and the providers "
            "enabled by the confirmed research profile."
        )
    )
    parser.add_argument(
        "--profile",
        type=Path,
        required=True,
        help="Confirmed research-profile JSON stored in the user's domain workspace.",
    )
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument(
        "--registry",
        type=Path,
        default=default_registry_path(),
        help="Explicit registry for this Skill installation or isolated test run.",
    )
    parser.add_argument("--target-papers", type=int, default=70)
    parser.add_argument("--openalex-per-query", type=int, default=50)
    parser.add_argument("--arxiv-per-query", type=int, default=60)
    parser.add_argument(
        "--review-candidate-limit",
        type=int,
        help=(
            "Maximum candidates in the coverage-preserving host-review packet. "
            "Defaults to a target-scaled limit."
        ),
    )
    parser.add_argument(
        "--refresh-search",
        action="store_true",
        help="Ignore successful metadata caches and query providers again.",
    )
    parser.add_argument(
        "--download-workers",
        type=int,
        default=DEFAULT_MAX_DOWNLOADS,
        help="Maximum concurrent PDF downloads (default: 4).",
    )
    parser.add_argument(
        "--download-workers-per-host",
        type=int,
        default=DEFAULT_MAX_DOWNLOADS_PER_HOST,
        help="Maximum concurrent PDF downloads to one host (default: 2).",
    )
    parser.add_argument("--skip-arxiv", action="store_true")
    parser.add_argument("--skip-openalex", action="store_true")
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument(
        "--selection",
        type=Path,
        help="Agent-reviewed selection JSON. Reuses candidates.jsonl without searching again.",
    )
    parser.add_argument("--analyze", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not 1 <= args.target_papers <= 100:
        raise SystemExit("--target-papers must be between 1 and 100")
    if (
        args.review_candidate_limit is not None
        and args.review_candidate_limit < args.target_papers
    ):
        raise SystemExit("--review-candidate-limit cannot be smaller than --target-papers")
    if args.download_workers <= 0:
        raise SystemExit("--download-workers must be positive")
    if args.download_workers_per_host <= 0:
        raise SystemExit("--download-workers-per-host must be positive")
    if args.download_workers_per_host > args.download_workers:
        raise SystemExit(
            "--download-workers-per-host cannot exceed --download-workers"
        )
    if args.search_only == bool(args.selection):
        raise SystemExit(
            "Choose exactly one stage: --search-only, or --selection <agent-review.json>."
        )

    profile = read_json(args.profile)
    validate_profile(profile)
    queries = list(profile["search_queries"])
    scope = profile["retrieval_scope"]
    workspace = args.workspace.resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    timeline = RunTimeline(workspace / "run-timings.json")
    write_json(workspace / "research-profile.json", profile)
    DomainRegistry(args.registry).register(workspace)
    openalex_api_key = load_openalex_api_key()
    started = utc_now()

    attempts: list[dict[str, Any]] = []
    if args.search_only:
        providers = list(scope["providers"])
        active_providers: list[str] = []
        collectors = {}
        if "openalex" in providers and not args.skip_openalex:
            active_providers.append("openalex")
            collectors["openalex"] = lambda: collect_openalex_candidates(
                queries,
                api_key=openalex_api_key,
                cache_dir=workspace / "cache" / "openalex-search",
                scope=scope,
                max_results_per_query=args.openalex_per_query,
                refresh=args.refresh_search,
            )

        if (
            "arxiv" in providers
            and not args.skip_arxiv
            and profile.get("arxiv_search_queries")
        ):
            active_providers.append("arxiv")
            collectors["arxiv"] = lambda: collect_arxiv_candidates(
                profile["arxiv_search_queries"],
                max_results_per_query=args.arxiv_per_query,
                scope=scope,
                cache_dir=workspace / "cache" / "arxiv-search",
                refresh=args.refresh_search,
            )

        with timeline.phase(
            "discovery.total",
            details={
                "providers": active_providers,
                "openalex_query_count": len(queries) if "openalex" in active_providers else 0,
                "arxiv_query_count": len(profile.get("arxiv_search_queries") or [])
                if "arxiv" in active_providers
                else 0,
            },
        ):
            groups, attempt_groups, provider_timings = collect_provider_lanes(
                active_providers,
                collectors,
            )
            for provider, timing in provider_timings.items():
                timeline.record(
                    f"discovery.{provider}",
                    started_at=timing["started_at"],
                    finished_at=timing["finished_at"],
                    elapsed_seconds=timing["elapsed_seconds"],
                    status=timing["status"],
                    details={
                        "candidate_count": timing.get("candidate_count", 0),
                        "attempt_count": timing.get("attempt_count", 0),
                    },
                )
            candidates = merge_candidates(
                *(groups[provider] for provider in active_providers)
            )
            write_jsonl(workspace / "candidates.jsonl", candidates)
            attempts = [
                item
                for provider in active_providers
                for item in _attempt_dicts(attempt_groups[provider])
            ]
            write_json(workspace / "search-attempts.json", attempts)

        with timeline.phase("candidate_review.prepare"):
            review_packet, review_summary = build_candidate_review_packet(
                candidates,
                target_papers=args.target_papers,
                limit=args.review_candidate_limit,
            )
            write_jsonl(workspace / "candidate-review-packet.jsonl", review_packet)
            write_json(workspace / "candidate-review-summary.json", review_summary)
        selected_candidates = candidates
    else:
        candidate_path = workspace / "candidates.jsonl"
        if not candidate_path.exists():
            raise SystemExit("Run --search-only in this workspace before applying a selection.")
        candidates = _read_jsonl(candidate_path)
        selected_candidates = apply_agent_selection(
            candidates,
            args.selection,
            workspace / "candidate-review-packet.jsonl",
        )
        attempt_path = workspace / "search-attempts.json"
        attempts = read_json(attempt_path) if attempt_path.exists() else []

    download_results: list[dict[str, Any]] = []
    if not args.search_only:
        with timeline.phase(
            "download",
            details={
                "selected_candidate_count": len(selected_candidates),
                "target_papers": args.target_papers,
            },
        ):
            download_results = download_candidates(
                selected_candidates,
                workspace,
                target_papers=args.target_papers,
                openalex_api_key=openalex_api_key,
                max_downloads=args.download_workers,
                max_downloads_per_host=args.download_workers_per_host,
            )
    status_counts = Counter(item["status"] for item in download_results)
    successful = status_counts["downloaded"] + status_counts["existing"]
    minimum_usable = minimum_usable_papers(args.target_papers)
    corpus_is_usable = successful >= minimum_usable
    analysis: dict[str, Any] | None = None
    if args.analyze and corpus_is_usable:
        with timeline.phase(
            "analysis",
            details={"successful_pdf_count": successful},
        ):
            analysis = analyze_corpus(
                candidates,
                download_results,
                workspace,
                profile=profile,
            )
    summary = {
        "started_at": started,
        "finished_at": utc_now(),
        "profile_id": profile["profile_id"],
        "workspace": str(workspace),
        "openalex_candidates": sum(item.get("provider") == "openalex" for item in candidates),
        "arxiv_candidates": sum(item.get("provider") == "arxiv" for item in candidates),
        "merged_candidates": len(candidates),
        "agent_selected_candidates": len(selected_candidates) if args.selection else None,
        "target_papers": args.target_papers,
        "minimum_usable_papers": minimum_usable,
        "successful_pdfs": successful,
        "corpus_is_usable": corpus_is_usable,
        "target_shortfall": max(0, args.target_papers - successful),
        "download_statuses": dict(status_counts),
        "search_statuses": dict(Counter(item["status"] for item in attempts)),
        "openalex_key_configured": bool(openalex_api_key),
        "used_openalex": any(item.get("provider") == "openalex" for item in candidates),
        "reused_existing_candidate_list": bool(args.selection),
        "timings": timeline.snapshot(),
        "analysis": analysis,
    }
    write_json(workspace / "cold-start-summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if args.search_only or corpus_is_usable else 2


def _attempt_dicts(attempts: Iterable[SearchAttempt]) -> list[dict[str, Any]]:
    return [_attempt_dict(attempt) for attempt in attempts]


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted; completed downloads were kept for resume.", file=sys.stderr)
        raise SystemExit(130)
