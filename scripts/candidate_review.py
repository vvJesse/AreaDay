"""Build a compact, coverage-preserving packet for host-agent review."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Iterable


def _coverage_keys(candidate: dict[str, Any]) -> list[tuple[str, str, str]]:
    candidate_provider = str(candidate.get("provider") or "unknown")
    keys: list[tuple[str, str, str]] = []
    for hit in candidate.get("query_hits") or []:
        if not isinstance(hit, dict):
            continue
        provider = str(hit.get("provider") or candidate_provider)
        query_id = str(hit.get("query_id") or "unattributed")
        lane = str(hit.get("date_lane") or "all")
        key = (provider, query_id, lane)
        if key not in keys:
            keys.append(key)
    return keys or [(candidate_provider, "unattributed", "all")]


def default_review_limit(target_papers: int) -> int:
    """Keep enough reviewed backups without exposing an unbounded candidate list."""
    if target_papers < 1:
        raise ValueError("target_papers must be positive")
    return min(100, max(target_papers * 2, target_papers + 10))


def build_candidate_review_packet(
    candidates: Iterable[dict[str, Any]],
    *,
    target_papers: int,
    limit: int | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Select an ordered review tranche while retaining every query/provider lane.

    Provider search order remains the relevance signal inside each coverage bucket.
    Round-robin selection prevents one broad query from consuming the packet before
    narrower confirmed angles receive review slots.
    """
    candidate_list = list(candidates)
    requested_limit = default_review_limit(target_papers) if limit is None else limit
    if requested_limit < target_papers:
        raise ValueError("review packet limit cannot be smaller than target_papers")

    buckets: OrderedDict[tuple[str, str, str], list[int]] = OrderedDict()
    for index, candidate in enumerate(candidate_list):
        for key in _coverage_keys(candidate):
            buckets.setdefault(key, []).append(index)

    selected_indexes: list[int] = []
    selected_set: set[int] = set()
    for indexes in buckets.values():
        if any(index in selected_set for index in indexes):
            continue
        index = indexes[0]
        selected_indexes.append(index)
        selected_set.add(index)

    minimum_coverage_candidates = len(selected_indexes)
    if limit is not None and limit < minimum_coverage_candidates:
        raise ValueError(
            "review packet limit is too small to preserve provider/query/date coverage; "
            f"at least {minimum_coverage_candidates} candidates are required"
        )
    packet_limit = min(
        len(candidate_list),
        max(requested_limit, minimum_coverage_candidates),
    )

    offsets = {key: 0 for key in buckets}
    while len(selected_indexes) < packet_limit:
        added_this_round = False
        for key, indexes in buckets.items():
            offset = offsets[key]
            while offset < len(indexes) and indexes[offset] in selected_set:
                offset += 1
            offsets[key] = offset
            if offset >= len(indexes):
                continue
            index = indexes[offset]
            offsets[key] += 1
            selected_set.add(index)
            selected_indexes.append(index)
            added_this_round = True
            if len(selected_indexes) >= packet_limit:
                break
        if not added_this_round:
            break

    if len(selected_indexes) < packet_limit:
        for index in range(len(candidate_list)):
            if index in selected_set:
                continue
            selected_indexes.append(index)
            if len(selected_indexes) >= packet_limit:
                break

    packet = [candidate_list[index] for index in selected_indexes]
    covered = OrderedDict()
    for candidate in packet:
        for provider, query_id, lane in _coverage_keys(candidate):
            label = f"{provider}:{query_id}:{lane}"
            covered[label] = int(covered.get(label, 0)) + 1
    summary = {
        "schema_version": 1,
        "candidate_count": len(candidate_list),
        "review_packet_count": len(packet),
        "deferred_candidate_count": len(candidate_list) - len(packet),
        "target_papers": target_papers,
        "coverage_bucket_count": len(buckets),
        "minimum_coverage_candidate_count": minimum_coverage_candidates,
        "coverage_counts": dict(covered),
    }
    return packet, summary
