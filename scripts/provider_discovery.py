"""Run independent scholarly-provider discovery lanes with bounded overlap."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any, Callable, Iterable


Collector = Callable[[], tuple[list[dict[str, Any]], list[Any]]]


def _wall_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def collect_provider_lanes(
    provider_order: Iterable[str],
    collectors: dict[str, Collector],
    *,
    max_workers: int = 2,
    monotonic: Callable[[], float] = time.monotonic,
    wall_now: Callable[[], str] = _wall_now,
) -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, list[Any]],
    dict[str, dict[str, Any]],
]:
    """Run provider collectors concurrently while returning deterministic order."""
    ordered = [provider for provider in provider_order if provider in collectors]
    if not ordered:
        return {}, {}, {}

    def measured(provider: str):
        started_at = wall_now()
        started = monotonic()
        try:
            candidates, attempts = collectors[provider]()
        except BaseException as error:
            timing = {
                "status": "failed",
                "started_at": started_at,
                "finished_at": wall_now(),
                "elapsed_seconds": round(monotonic() - started, 6),
                "error_type": type(error).__name__,
            }
            return provider, None, None, timing, error
        timing = {
            "status": "ok",
            "started_at": started_at,
            "finished_at": wall_now(),
            "elapsed_seconds": round(monotonic() - started, 6),
            "candidate_count": len(candidates),
            "attempt_count": len(attempts),
        }
        return provider, candidates, attempts, timing, None

    completed: dict[str, tuple[Any, ...]] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(ordered))) as executor:
        futures = {provider: executor.submit(measured, provider) for provider in ordered}
        for provider in ordered:
            completed[provider] = futures[provider].result()

    candidate_groups: dict[str, list[dict[str, Any]]] = {}
    attempt_groups: dict[str, list[Any]] = {}
    timings: dict[str, dict[str, Any]] = {}
    for provider in ordered:
        _, candidates, attempts, timing, error = completed[provider]
        timings[provider] = timing
        if error is not None:
            raise error
        candidate_groups[provider] = candidates
        attempt_groups[provider] = attempts
    return candidate_groups, attempt_groups, timings
