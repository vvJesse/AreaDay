#!/usr/bin/env python3
"""Deterministic stages for ResearchRamp's continuing weekly workflow.

The host agent remains responsible for relevance/value review, paper selection,
and all shadow-preview writing. This script performs metadata
discovery, temporary full-text preparation, output validation, local import,
and cleanup.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
from collections import Counter
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from academic_text import clean_academic_text, extract_pdf_text
from acquire_mini_corpus import (
    collect_arxiv_candidates,
    collect_openalex_candidates,
    download_candidates,
    merge_candidates,
)
from continuous_state import ContinuousStore, discovery_keys, utc_iso, validate_brief
from research_profile import validate_profile
from researchramp_core import (
    load_openalex_api_key,
    read_json,
    stable_hash,
    write_json,
    write_jsonl,
)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _profile_path(workspace: Path) -> Path:
    for name in ("research-profile-input.json", "research-profile.json"):
        path = workspace / name
        if path.is_file():
            return path
    raise FileNotFoundError("没有找到已确认的 ResearchRamp 研究配置，请先运行 init")


def _continuous_root(workspace: Path) -> Path:
    return workspace / "continuous"


def _discovery_state_path(workspace: Path) -> Path:
    return _continuous_root(workspace) / "discovery-state.json"


def _candidate_date(candidate: dict[str, Any]) -> datetime | None:
    raw = str(candidate.get("publication_date") or "").strip()
    if raw:
        try:
            return datetime.fromisoformat(raw).replace(tzinfo=UTC)
        except ValueError:
            pass
    year = candidate.get("year")
    if isinstance(year, int):
        return datetime(year, 1, 1, tzinfo=UTC)
    return None


ACADEMIC_ITEM_TYPES = {
    "new_paper",
    "recent_paper",
    "classic_paper",
    "backlog_paper",
}
SUPPLEMENTAL_ITEM_TYPES = {"public_report", "research_update"}
FRESHNESS_ORDER = {"new": 4, "recent": 3, "classic": 2, "undated": 1, "supplemental": 0}


def _classify_candidate(
    candidate: dict[str, Any],
    *,
    now: datetime,
    fresh_days: int,
    recent_days: int,
) -> dict[str, Any]:
    copy = dict(candidate)
    if str(copy.get("recommended_item_type") or "") in SUPPLEMENTAL_ITEM_TYPES:
        copy["freshness_tier"] = "supplemental"
        return copy
    published = _candidate_date(copy)
    if published is None:
        tier, item_type = "undated", "backlog_paper"
    elif published >= now - timedelta(days=fresh_days):
        tier, item_type = "new", "new_paper"
    elif published >= now - timedelta(days=recent_days):
        tier, item_type = "recent", "recent_paper"
    else:
        tier, item_type = "classic", "classic_paper"
    copy["freshness_tier"] = tier
    copy["recommended_item_type"] = item_type
    return copy


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, str, int]:
    return (
        FRESHNESS_ORDER.get(str(candidate.get("freshness_tier") or ""), -1),
        str(candidate.get("publication_date") or ""),
        int(candidate.get("citation_count") or 0),
    )


def _collect_academic_candidates(
    *,
    scope: dict[str, Any],
    providers: list[str],
    queries: list[dict[str, Any]],
    arxiv_queries: list[dict[str, Any]],
    per_query: int,
    cache_dir: Path,
    openalex_api_key: str | None,
) -> tuple[list[dict[str, Any]], list[Any]]:
    candidates: list[dict[str, Any]] = []
    attempts: list[Any] = []
    if "openalex" in providers:
        group, group_attempts = collect_openalex_candidates(
            queries,
            api_key=openalex_api_key,
            cache_dir=cache_dir / "openalex",
            scope=scope,
            max_results_per_query=per_query,
        )
        candidates.extend(group)
        attempts.extend(group_attempts)
    if "arxiv" in providers and arxiv_queries:
        group, group_attempts = collect_arxiv_candidates(
            arxiv_queries,
            max_results_per_query=per_query,
            scope=scope,
        )
        candidates.extend(group)
        attempts.extend(group_attempts)
    return candidates, attempts


def discover(
    workspace: Path,
    fresh_days: int,
    recent_days: int,
    per_query: int,
    query_limit: int | None = None,
    include_classics: bool = False,
) -> dict[str, Any]:
    openalex_api_key = load_openalex_api_key()
    profile = read_json(_profile_path(workspace))
    validate_profile(profile)
    store = ContinuousStore(workspace)
    state_path = _discovery_state_path(workspace)
    now = datetime.now(UTC)
    fresh_days = max(1, fresh_days)
    recent_days = max(fresh_days, recent_days)
    recent_cutoff = now - timedelta(days=recent_days)

    base_scope = dict(profile["retrieval_scope"])
    providers = list(base_scope["providers"])
    queries = profile["search_queries"]
    if query_limit is not None:
        queries = queries[: max(1, query_limit)]
    discovery_dir = _continuous_root(workspace) / "discovery"
    recent_scope = dict(base_scope)
    recent_scope["recent_from_year"] = recent_cutoff.year
    recent_scope["foundation_from_year"] = recent_cutoff.year
    recent_scope["publication_from_date"] = recent_cutoff.date().isoformat()
    recent_scope["publication_to_date"] = now.date().isoformat()
    recent_arxiv_queries = [
        query
        for query in (profile.get("arxiv_search_queries") or [])
        if query.get("date_lane", "recent") == "recent"
    ]
    candidates, attempts = _collect_academic_candidates(
        scope=recent_scope,
        providers=list(providers),
        queries=list(queries),
        arxiv_queries=recent_arxiv_queries,
        per_query=per_query,
        cache_dir=discovery_dir / "cache" / "recent",
        openalex_api_key=openalex_api_key,
    )

    if include_classics:
        classic_candidates, classic_attempts = _collect_academic_candidates(
            scope=base_scope,
            providers=list(providers),
            queries=list(queries),
            arxiv_queries=list(profile.get("arxiv_search_queries") or []),
            per_query=min(per_query, 10),
            cache_dir=discovery_dir / "cache" / "classic",
            openalex_api_key=openalex_api_key,
        )
        candidates.extend(
            candidate
            for candidate in classic_candidates
            if (published := _candidate_date(candidate)) is None
            or published < recent_cutoff
        )
        attempts.extend(classic_attempts)

    attempt_payload = [attempt.__dict__ for attempt in attempts]
    write_json(discovery_dir / "search-attempts.json", attempt_payload)
    if not any(attempt.status == "ok" for attempt in attempts):
        raise RuntimeError(
            "All configured discovery providers failed; the previous successful scan "
            "time and candidate pool were left unchanged."
        )

    merged = merge_candidates(candidates)
    discovery_scope_id = stable_hash(
        {
            "profile_id": profile["profile_id"],
            "providers": providers,
            "openalex_primary_filter": base_scope.get("openalex_primary_filter"),
            "arxiv_categories": base_scope.get("arxiv_categories") or [],
            "search_queries": queries,
            "arxiv_search_queries": recent_arxiv_queries,
            "publication_from_date": recent_scope["publication_from_date"],
        },
        24,
    )
    classified = [
        {
            **_classify_candidate(
                candidate,
                now=now,
                fresh_days=fresh_days,
                recent_days=recent_days,
            ),
            "discovery_scope_id": discovery_scope_id,
        }
        for candidate in merged
        if include_classics
        or (candidate_date := _candidate_date(candidate)) is None
        or candidate_date >= recent_cutoff
    ]
    seen = store.seen_discovery_keys(discovery_scope_id=discovery_scope_id)
    fresh = [candidate for candidate in classified if not (discovery_keys(candidate) & seen)]
    fresh.sort(key=_candidate_sort_key, reverse=True)
    newly_recorded = store.record_discoveries(fresh)
    candidate_pool = [
        _classify_candidate(
            candidate,
            now=now,
            fresh_days=fresh_days,
            recent_days=recent_days,
        )
        for candidate in store.unrecommended_discoveries(
            discovery_scope_id=discovery_scope_id
        )
    ]
    candidate_pool.sort(key=_candidate_sort_key, reverse=True)
    tier_counts = Counter(
        str(candidate.get("freshness_tier") or "undated")
        for candidate in candidate_pool
    )
    write_jsonl(discovery_dir / "candidates.jsonl", candidate_pool)
    write_json(
        discovery_dir / "host-review-input.json",
        {
            "schema_version": 2,
            "research_summary": profile["research_summary"],
            "period_start": (now - timedelta(days=fresh_days)).date().isoformat(),
            "period_end": now.date().isoformat(),
            "new_candidate_count": newly_recorded,
            "candidate_pool_count": len(candidate_pool),
            "candidate_tiers": dict(tier_counts),
            "fresh_days": fresh_days,
            "recent_days": recent_days,
            "classic_lane_included": include_classics,
            "discovery_scope_id": discovery_scope_id,
            "provider_route": providers,
            "openalex_primary_filter": base_scope.get("openalex_primary_filter"),
            "arxiv_categories": base_scope.get("arxiv_categories") or [],
            "candidate_file": str(discovery_dir / "candidates.jsonl"),
            "required_total_items": {"minimum": 2, "target": 3, "maximum": 5},
            "agent_instructions": [
                "Review candidates from title and abstract for research value and relevance.",
                "Judge research relevance and value first. Use recency only to break ties between similarly useful papers.",
                "If fewer than two strong papers remain and classic_lane_included is false, rerun discover with --include-classics and review again.",
                "Use older classic papers only after the new and recent lanes remain insufficient.",
                "If all paper lanes still provide fewer than two strong items, search the public web for a directly readable report or research update and add it as a supplemental item. Do not invent a source or use inaccessible content.",
                "Select 2–5 strong items in total and preserve each candidate's recommended_item_type.",
                "Write a selection JSON using references/continuous-workflow.md.",
            ],
        },
    )
    write_json(
        state_path,
        {
            "schema_version": 2,
            "last_successful_scan_at": now.isoformat(),
            "last_period_start": (now - timedelta(days=fresh_days)).date().isoformat(),
            "new_candidate_count": newly_recorded,
            "candidate_pool_count": len(candidate_pool),
            "candidate_tiers": dict(tier_counts),
            "classic_lane_included": include_classics,
            "discovery_scope_id": discovery_scope_id,
            "attempts": attempt_payload,
        },
    )
    return {
        "status": "ok",
        "new_candidate_count": newly_recorded,
        "candidate_pool_count": len(candidate_pool),
        "candidate_tiers": dict(tier_counts),
        "classic_lane_included": include_classics,
        "review_input": str(discovery_dir / "host-review-input.json"),
        "candidates": str(discovery_dir / "candidates.jsonl"),
    }


def _personal_unfamiliar(workspace: Path) -> set[str]:
    path = workspace / "analysis" / "personalized-vocabulary.tsv"
    if not path.is_file():
        raise FileNotFoundError("缺少个人领域词表，请先完成 init 的30题校准")
    result: set[str] = set()
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("classification") != "likely_known":
                lemma = str(row.get("lemma") or "").strip().lower()
                if lemma:
                    result.add(lemma)
    return result


def _candidate_source_url(candidate: dict[str, Any]) -> str:
    source_url = str(candidate.get("source_url") or "").strip()
    if source_url:
        return source_url
    doi = str(candidate.get("doi") or "").strip()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    if doi:
        return f"https://doi.org/{doi}"
    arxiv_id = str(candidate.get("arxiv_id") or "").strip()
    if arxiv_id:
        return f"https://arxiv.org/abs/{arxiv_id}"
    openalex_id = str(candidate.get("openalex_id") or "").strip()
    if openalex_id:
        return f"https://openalex.org/{openalex_id}"
    return str(candidate.get("pdf_url") or "").strip()


def _normalized_identity_words(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _verify_pdf_identity(candidate: dict[str, Any], raw_text: str) -> dict[str, Any]:
    """Conservatively bind downloaded PDF text to the selected metadata record."""

    head = raw_text[:12000]
    head_folded = head.casefold()
    title_tokens = _normalized_identity_words(str(candidate.get("title") or ""))
    head_tokens = _normalized_identity_words(head)
    normalized_title = " ".join(title_tokens)
    normalized_head = " ".join(head_tokens)

    doi = re.sub(
        r"^https?://(?:dx\.)?doi\.org/",
        "",
        str(candidate.get("doi") or "").strip().casefold(),
    )
    head_dois = {
        match.rstrip(".,;)").casefold()
        for match in re.findall(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", head_folded)
    }
    if doi and head_dois and doi not in head_dois:
        return {
            "verified": False,
            "reason": "downloaded PDF exposes a different DOI near its front matter",
            "candidate_doi": doi,
            "observed_dois": sorted(head_dois)[:10],
        }

    identifier_match = False
    evidence = ""
    if doi and doi in head_folded:
        identifier_match = True
        evidence = "doi"
    arxiv_id = str(candidate.get("arxiv_id") or "").strip().casefold()
    if arxiv_id and arxiv_id in head_folded:
        identifier_match = True
        evidence = evidence or "arxiv_id"

    exact_title = bool(normalized_title and normalized_title in normalized_head)
    head_token_set = set(head_tokens)
    title_coverage = (
        sum(token in head_token_set for token in title_tokens) / len(title_tokens)
        if title_tokens
        else 0.0
    )
    position = 0
    ordered_matches = 0
    for token in title_tokens:
        try:
            position = head_tokens.index(token, position) + 1
            ordered_matches += 1
        except ValueError:
            continue
    ordered_coverage = ordered_matches / len(title_tokens) if title_tokens else 0.0
    title_match = exact_title or (
        len(title_tokens) >= 4 and title_coverage >= 0.90 and ordered_coverage >= 0.80
    )
    verified = identifier_match or title_match
    return {
        "verified": verified,
        "reason": evidence or ("title" if title_match else "title/identifier mismatch"),
        "title_token_coverage": round(title_coverage, 4),
        "ordered_title_coverage": round(ordered_coverage, 4),
        "candidate_doi": doi or None,
        "observed_dois": sorted(head_dois)[:10],
    }


def _context_window(sentence: str, start: int, end: int, limit: int = 500) -> str:
    """Return a compact source span that always contains the matched token."""

    if len(sentence) <= limit:
        return re.sub(r"\s+", " ", sentence.strip())
    center = (start + end) // 2
    left = max(0, center - limit // 2)
    right = min(len(sentence), left + limit)
    left = max(0, right - limit)
    if left:
        next_space = sentence.find(" ", left, min(start, left + 80))
        if next_space != -1:
            left = next_space + 1
    if right < len(sentence):
        previous_space = sentence.rfind(" ", max(end, right - 80), right)
        if previous_space > end:
            right = previous_space
    excerpt = re.sub(r"\s+", " ", sentence[left:right].strip())
    if left:
        excerpt = "… " + excerpt
    if right < len(sentence):
        excerpt += " …"
    return excerpt


def _contexts_for_words(text: str, unfamiliar: set[str], limit: int = 80) -> list[dict[str, Any]]:
    import spacy

    model = spacy.load("en_core_web_sm", disable=["ner", "parser"])
    sentences = re.split(r"(?<=[.!?])\s+", text)
    counts: Counter[str] = Counter()
    contexts: dict[str, str] = {}
    pos: dict[str, str] = {}
    for sentence in sentences:
        if not sentence.strip():
            continue
        doc = model(sentence[:5000])
        for token in doc:
            lemma = token.lemma_.lower().strip()
            if lemma not in unfamiliar or not token.is_alpha:
                continue
            counts[lemma] += 1
            contexts.setdefault(
                lemma,
                _context_window(sentence, token.idx, token.idx + len(token.text)),
            )
            pos.setdefault(lemma, token.pos_.lower())
    return [
        {"lemma": lemma, "count": count, "part_of_speech": pos.get(lemma, ""), "context": contexts.get(lemma, "")}
        for lemma, count in counts.most_common(limit)
    ]


class _ReadableHTML(HTMLParser):
    BLOCKS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "article"}
    IGNORED = {"script", "style", "noscript", "svg", "form", "nav", "footer"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.IGNORED:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.IGNORED and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth and data.strip():
            self.parts.append(data)

    def text(self) -> str:
        lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(self.parts).splitlines()]
        return "\n\n".join(line for line in lines if line)


def _normalize_supplemental_items(raw_items: Any) -> list[dict[str, Any]]:
    if raw_items is None:
        return []
    if not isinstance(raw_items, list):
        raise ValueError("selection.supplemental_items must be a list")
    result: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    allowed = {
        "item_type",
        "title",
        "source_url",
        "content_url",
        "publication_date",
        "venue",
        "abstract",
    }
    for index, raw in enumerate(raw_items, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"selection.supplemental_items[{index}] must be an object")
        unexpected = sorted(set(raw) - allowed)
        if unexpected:
            raise ValueError(
                f"selection.supplemental_items[{index}] contains unsupported fields: {unexpected}"
            )
        item_type = str(raw.get("item_type") or "").strip()
        if item_type not in SUPPLEMENTAL_ITEM_TYPES:
            raise ValueError(
                f"selection.supplemental_items[{index}].item_type must be public_report or research_update"
            )
        required = {
            key: str(raw.get(key) or "").strip()
            for key in ("title", "source_url", "publication_date", "venue")
        }
        if any(not value for value in required.values()):
            raise ValueError(
                f"selection.supplemental_items[{index}] requires title, source_url, publication_date, and venue"
            )
        for key in ("source_url", "content_url"):
            value = str(raw.get(key) or "").strip()
            if not value:
                continue
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    f"selection.supplemental_items[{index}].{key} must be a public http(s) URL"
                )
        try:
            published = datetime.fromisoformat(required["publication_date"])
        except ValueError as error:
            raise ValueError(
                f"selection.supplemental_items[{index}].publication_date must be ISO format"
            ) from error
        normalized_url = required["source_url"].rstrip("/").casefold()
        if normalized_url in seen_urls:
            raise ValueError("selection contains duplicate supplemental source URLs")
        seen_urls.add(normalized_url)
        result.append(
            {
                "candidate_id": f"Web:{stable_hash(normalized_url, 24)}",
                "provider": "web-supplemental",
                "title": required["title"],
                "abstract": str(raw.get("abstract") or "").strip(),
                "year": published.year,
                "publication_date": required["publication_date"],
                "venue": required["venue"],
                "source_url": required["source_url"],
                "content_url": str(raw.get("content_url") or required["source_url"]).strip(),
                "pdf_url": "",
                "alternate_pdf_urls": [],
                "citation_count": 0,
                "query_hits": [],
                "recommended_item_type": item_type,
                "freshness_tier": "supplemental",
            }
        )
    return result


def _fetch_supplemental_source(candidate: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    content_url = str(candidate.get("content_url") or candidate.get("source_url") or "").strip()
    response = requests.get(
        content_url,
        headers={"User-Agent": "ResearchRamp/1.0 (+local research brief)"},
        timeout=45,
    )
    response.raise_for_status()
    body = response.content
    if not body or len(body) > 50 * 1024 * 1024:
        raise ValueError(f"supplemental source has an unsupported size: {candidate['candidate_id']}")
    source_dir = run_dir / "sources"
    text_dir = run_dir / "text"
    source_dir.mkdir(parents=True, exist_ok=True)
    text_dir.mkdir(parents=True, exist_ok=True)
    source_stem = stable_hash(candidate["candidate_id"])
    content_type = str(response.headers.get("Content-Type") or "").casefold()
    if body.startswith(b"%PDF") or "application/pdf" in content_type:
        source_path = source_dir / f"{source_stem}.pdf"
        source_path.write_bytes(body)
        raw_text, page_count = extract_pdf_text(source_path)
        clean_text, diagnostics = clean_academic_text(raw_text)
        body_word_count = int(diagnostics["body_word_count"])
    else:
        encoding = response.encoding or "utf-8"
        decoded = body.decode(encoding, errors="replace")
        if "html" in content_type or "<html" in decoded[:1000].casefold():
            parser = _ReadableHTML()
            parser.feed(decoded)
            clean_text = parser.text()
            source_path = source_dir / f"{source_stem}.html"
            source_path.write_bytes(body)
        else:
            clean_text = re.sub(r"[ \t]+", " ", decoded).strip()
            source_path = source_dir / f"{source_stem}.txt"
            source_path.write_bytes(body)
        page_count = 0
        body_word_count = len(re.findall(r"\b[A-Za-z][A-Za-z'-]*\b", clean_text))
    if body_word_count < 200:
        raise ValueError(
            f"supplemental source did not expose enough readable text: {candidate['candidate_id']}"
        )
    identity_check = _verify_pdf_identity(candidate, clean_text)
    if not identity_check["verified"]:
        raise ValueError(
            f"supplemental source title does not match its readable content: {candidate['candidate_id']}"
        )
    text_path = text_dir / f"{source_stem}.txt"
    text_path.write_text(clean_text, encoding="utf-8")
    return {
        "clean_text": clean_text,
        "text_path": text_path,
        "page_count": page_count,
        "body_word_count": body_word_count,
        "actual_download_url": str(response.url),
        "content_sha256": hashlib.sha256(body).hexdigest(),
        "identity_check": identity_check,
    }


def prepare(workspace: Path, selection_path: Path) -> dict[str, Any]:
    selection = read_json(selection_path)
    if selection.get("schema_version") != 1:
        raise ValueError("selection.schema_version must be 1")
    selected_ids = selection.get("selected_candidate_ids") or []
    if not isinstance(selected_ids, list) or not all(
        isinstance(candidate_id, str) and candidate_id.strip() for candidate_id in selected_ids
    ):
        raise ValueError("selection.selected_candidate_ids must be a list of candidate IDs")
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("selection contains duplicate candidate IDs")
    supplemental = _normalize_supplemental_items(selection.get("supplemental_items"))
    if not 2 <= len(selected_ids) + len(supplemental) <= 5:
        raise ValueError("selection must contain 2 to 5 papers or supplemental materials in total")
    allowed_selection_keys = {
        "schema_version",
        "period_start",
        "period_end",
        "selected_candidate_ids",
        "supplemental_items",
    }
    unexpected_keys = sorted(set(selection) - allowed_selection_keys)
    if unexpected_keys:
        raise ValueError(f"selection contains unsupported fields: {unexpected_keys}")
    for key in ("period_start", "period_end"):
        if not str(selection.get(key) or "").strip():
            raise ValueError(f"selection.{key} is required")

    discovery_dir = _continuous_root(workspace) / "discovery"
    by_id = {item["candidate_id"]: item for item in _read_jsonl(discovery_dir / "candidates.jsonl")}
    missing = [candidate_id for candidate_id in selected_ids if candidate_id not in by_id]
    if missing:
        raise ValueError(f"selection contains unknown candidate IDs: {missing}")
    selected = [dict(by_id[candidate_id]) for candidate_id in selected_ids]
    seen_selection_keys: set[str] = set()
    for candidate in [*selected, *supplemental]:
        candidate_keys = discovery_keys(candidate)
        if candidate_keys & seen_selection_keys:
            raise ValueError("selection contains the same source more than once")
        seen_selection_keys.update(candidate_keys)
    store = ContinuousStore(workspace)
    historical_keys = store.seen_discovery_keys()
    unrecommended_ids = {
        str(candidate.get("candidate_id") or "")
        for candidate in store.unrecommended_discoveries()
    }
    for candidate in supplemental:
        if (
            discovery_keys(candidate) & historical_keys
            and candidate["candidate_id"] not in unrecommended_ids
        ):
            raise ValueError(
                f"supplemental source was already discovered or recommended: {candidate['title']}"
            )
    store.record_discoveries(supplemental)
    selected.extend(supplemental)

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = _continuous_root(workspace) / "working" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    academic_selected = [
        candidate
        for candidate in selected
        if str(candidate.get("recommended_item_type") or "") not in SUPPLEMENTAL_ITEM_TYPES
    ]
    download_results = (
        download_candidates(
            academic_selected,
            run_dir,
            target_papers=len(academic_selected),
            openalex_api_key=load_openalex_api_key(),
        )
        if academic_selected
        else []
    )
    results_by_id = {
        str(item.get("candidate_id") or ""): item for item in download_results
    }
    failed_ids = [
        candidate["candidate_id"]
        for candidate in academic_selected
        if results_by_id.get(candidate["candidate_id"], {}).get("status")
        not in {"downloaded", "existing"}
    ]
    if failed_ids:
        raise RuntimeError(
            "selected paper PDFs were not downloaded; weekly preparation stopped: "
            + ", ".join(failed_ids)
        )
    unfamiliar = _personal_unfamiliar(workspace)
    prepared_items: list[dict[str, Any]] = []
    for candidate in selected:
        item_type = str(candidate.get("recommended_item_type") or "").strip()
        if item_type in SUPPLEMENTAL_ITEM_TYPES:
            source = _fetch_supplemental_source(candidate, run_dir)
            clean_text = str(source["clean_text"])
            text_path = Path(source["text_path"])
            page_count = int(source["page_count"])
            body_word_count = int(source["body_word_count"])
            source_provenance = {
                "candidate_id": candidate["candidate_id"],
                "source_url": candidate["source_url"],
                "actual_download_url": source["actual_download_url"],
                "content_sha256": source["content_sha256"],
                "identity_check": source["identity_check"],
            }
        else:
            if item_type not in ACADEMIC_ITEM_TYPES:
                candidate = _classify_candidate(
                    candidate,
                    now=datetime.now(UTC),
                    fresh_days=14,
                    recent_days=90,
                )
                item_type = str(candidate["recommended_item_type"])
            result = results_by_id[candidate["candidate_id"]]
            pdf_path = Path(result["path"])
            raw_text, page_count = extract_pdf_text(pdf_path)
            identity_check = _verify_pdf_identity(candidate, raw_text)
            if not identity_check["verified"]:
                raise RuntimeError(
                    "downloaded PDF does not match the selected paper; weekly preparation "
                    f"stopped: {candidate['candidate_id']} ({identity_check['reason']})"
                )
            clean_text, diagnostics = clean_academic_text(raw_text)
            body_word_count = int(diagnostics["body_word_count"])
            text_path = run_dir / "text" / f"{stable_hash(candidate['candidate_id'])}.txt"
            text_path.parent.mkdir(parents=True, exist_ok=True)
            text_path.write_text(clean_text, encoding="utf-8")
            source_provenance = {
                "candidate_id": candidate["candidate_id"],
                "doi": candidate.get("doi"),
                "arxiv_id": candidate.get("arxiv_id"),
                "openalex_id": candidate.get("openalex_id"),
                "semantic_scholar_id": candidate.get("semantic_scholar_id"),
                "actual_download_url": result.get("pdf_url"),
                "pdf_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                "identity_check": identity_check,
            }
        words = _contexts_for_words(clean_text, unfamiliar)
        source_url = _candidate_source_url(candidate)
        if not re.match(r"^https?://", source_url):
            raise ValueError(
                f"selected paper has no canonical source URL: {candidate['candidate_id']}"
            )
        prepared_items.append(
            {
                **candidate,
                "item_type": item_type,
                "source_url": source_url,
                "source_provenance": source_provenance,
                "temporary_text_path": str(text_path),
                "page_count": page_count,
                "body_word_count": body_word_count,
                "estimated_minutes": max(1, round(body_word_count / 180)),
                "vocabulary_candidates": words,
            }
        )

    packet = {
        "schema_version": 1,
        "run_id": run_id,
        "workspace": str(workspace),
        "period_start": selection.get("period_start"),
        "period_end": selection.get("period_end"),
        "research_summary": read_json(_profile_path(workspace))["research_summary"],
        "paper_items": prepared_items,
        "agent_output_path": str(run_dir / "brief-agent-output.json"),
        "agent_requirements": [
            "Produce exactly the prepared recommendations and preserve their item_type labels.",
            "Generate all value explanations, weekly summary text, vocabulary meanings, and shadow previews as the current Codex/Work Buddy agent; do not call an external model API.",
            "Use the temporary full text for source-level reading and vocabulary context, not title/abstract alone.",
            "Preserve each item's source_provenance object exactly in the finished item.",
            "Each shadow preview must naturally cover its supplied vocabulary and terminology, splitting into several short paragraphs when needed.",
            "Do not copy long passages from the source. Save only derived writing and short representative contexts.",
            "Write the final JSON using references/continuous-workflow.md, then run finalize.",
        ],
    }
    write_json(run_dir / "agent-brief-input.json", packet)
    return {
        "status": "ready_for_agent",
        "run_dir": str(run_dir),
        "agent_input": str(run_dir / "agent-brief-input.json"),
        "agent_output": str(run_dir / "brief-agent-output.json"),
        "item_count": len(prepared_items),
        "paper_items": sum(
            1 for item in prepared_items if item["item_type"] in ACADEMIC_ITEM_TYPES
        ),
        "supplemental_items": sum(
            1 for item in prepared_items if item["item_type"] in SUPPLEMENTAL_ITEM_TYPES
        ),
    }


def validate_output_provenance(payload: dict[str, Any], packet: dict[str, Any]) -> None:
    """Reject title/link/context combinations not grounded in the prepared source."""

    expected_brief_id = str(packet.get("brief_id") or "").strip()
    if expected_brief_id and str(payload.get("brief_id") or "").strip() != expected_brief_id:
        raise ValueError("finished brief changed its controller-assigned brief_id")
    for key in ("period_start", "period_end"):
        expected_period = str(packet.get(key) or "").strip()
        if expected_period and str(payload.get(key) or "").strip() != expected_period:
            raise ValueError(f"finished brief changed its prepared {key}")

    expected: dict[str, dict[str, Any]] = {}
    for raw in packet.get("paper_items") or []:
        item = dict(raw)
        item_id = str(item.get("candidate_id") or "").strip()
        if item_id:
            if item_id in expected:
                raise ValueError(f"duplicate prepared source item ID: {item_id}")
            expected[item_id] = {
                "title": str(item.get("title") or "").strip(),
                "source_url": str(item.get("source_url") or "").strip(),
                "item_type": str(item.get("item_type") or "new_paper").strip(),
                "source_provenance": item.get("source_provenance"),
                "contexts": {
                    str(word.get("lemma") or "").strip().lower(): str(
                        word.get("context") or ""
                    ).strip()
                    for word in item.get("vocabulary_candidates") or []
                },
            }
    output_items = payload.get("items") or []
    output_ids = [str(item.get("item_id") or "").strip() for item in output_items]
    if len(output_ids) != len(expected) or set(output_ids) != set(expected):
        raise ValueError(
            "finished brief must contain exactly the papers in its prepared packet"
        )

    for item in output_items:
        item_id = str(item.get("item_id") or "").strip()
        source = expected.get(item_id)
        if source is None:
            raise ValueError(f"brief item is not present in the prepared packet: {item_id}")
        for key in ("title", "source_url", "item_type"):
            if str(item.get(key) or "").strip() != source[key]:
                raise ValueError(f"brief item {item_id} changed its source {key}")
        if item.get("source_provenance") != source["source_provenance"]:
            raise ValueError(f"brief item {item_id} changed its source provenance")
        contexts = source["contexts"]
        for word in item.get("vocabulary") or []:
            lemma = str(word.get("lemma") or "").strip().lower()
            if lemma not in contexts:
                raise ValueError(f"vocabulary is not grounded in its prepared source: {item_id}/{lemma}")
            if str(word.get("context") or "").strip() != contexts[lemma]:
                raise ValueError(f"source context changed after preparation: {item_id}/{lemma}")


def finalize(workspace: Path, output_path: Path) -> dict[str, Any]:
    output_path = output_path.resolve()
    working_root = (_continuous_root(workspace) / "working").resolve()
    run_dir = output_path.parent
    if run_dir.parent != working_root or output_path.name != "brief-agent-output.json":
        raise ValueError(
            "agent output must be the brief-agent-output.json inside one isolated run directory"
        )
    payload = validate_brief(read_json(output_path))
    packet_path = output_path.parent / "agent-brief-input.json"
    if not packet_path.is_file():
        raise ValueError("the prepared agent input is missing; source alignment cannot be verified")
    packet = read_json(packet_path)
    if Path(str(packet.get("workspace") or "")).resolve() != workspace.resolve():
        raise ValueError("prepared packet belongs to a different ResearchRamp workspace")
    if str(packet.get("run_id") or "") != run_dir.name:
        raise ValueError("prepared packet run ID does not match its isolated run directory")
    if Path(str(packet.get("agent_output_path") or "")).resolve() != output_path:
        raise ValueError("prepared packet points to a different agent output path")
    validate_output_provenance(payload, packet)
    store = ContinuousStore(workspace)
    brief = store.import_brief(payload)
    archive_root = (_continuous_root(workspace) / "briefs").resolve()
    archived = (archive_root / f"{brief['brief_id']}.json").resolve()
    if archived.parent != archive_root:
        raise ValueError("brief archive path escaped its workspace")
    write_json(archived, brief)
    shutil.rmtree(run_dir)
    return {
        "status": "finalized",
        "brief_id": brief["brief_id"],
        "item_count": len(brief["items"]),
        "archive": str(archived),
        "temporary_files_deleted": not run_dir.exists(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    subparsers = parser.add_subparsers(dest="command", required=True)
    discover_parser = subparsers.add_parser("discover")
    discover_parser.add_argument(
        "--lookback-days",
        type=int,
        default=14,
        help="Publication-age boundary for the new-paper lane.",
    )
    discover_parser.add_argument(
        "--recent-days",
        type=int,
        default=1460,
        help="Rolling publication window for the main paper lane (default: four years).",
    )
    discover_parser.add_argument(
        "--include-classics",
        action="store_true",
        help="Also search the confirmed older date range for high-value classic papers.",
    )
    discover_parser.add_argument("--per-query", type=int, default=25)
    discover_parser.add_argument("--query-limit", type=int)
    prepare_parser = subparsers.add_parser("prepare")
    prepare_parser.add_argument("--selection", type=Path, required=True)
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("--agent-output", type=Path, required=True)
    import_parser = subparsers.add_parser("import-brief")
    import_parser.add_argument("--input", type=Path, required=True)
    handoff_parser = subparsers.add_parser("schedule-handoff")
    handoff_parser.add_argument(
        "--section", choices=("weekly_brief", "daily_review")
    )
    subparsers.add_parser("due-count")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workspace = args.workspace.resolve()
    if args.command == "discover":
        result = discover(
            workspace,
            max(1, args.lookback_days),
            max(1, args.recent_days),
            max(1, min(args.per_query, 100)),
            args.query_limit,
            args.include_classics,
        )
    elif args.command == "prepare":
        result = prepare(workspace, args.selection.resolve())
    elif args.command == "finalize":
        result = finalize(workspace, args.agent_output.resolve())
    elif args.command == "import-brief":
        result = ContinuousStore(workspace).import_brief(read_json(args.input.resolve()))
    elif args.command == "schedule-handoff":
        result = ContinuousStore(workspace).automation_handoff(args.section)
    elif args.command == "due-count":
        result = {"due_count": len(ContinuousStore(workspace).due_words())}
    else:
        raise AssertionError(args.command)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
