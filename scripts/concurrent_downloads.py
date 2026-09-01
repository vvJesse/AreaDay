"""Deterministic, bounded PDF acquisition for ResearchRamp candidates.

Only the coordinator mutates result records and writes the checkpoint. Worker
threads execute one route at a time, which keeps fallbacks for a candidate
strictly serial while allowing unrelated candidates to make progress.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from fulltext import (
    download_licensed_open_access_pdf,
    download_openalex_content_pdf,
    valid_pdf,
    validated_pdf_hostname,
)
from researchramp_core import write_jsonl


DEFAULT_MAX_DOWNLOADS = 4
DEFAULT_MAX_DOWNLOADS_PER_HOST = 2
DEFAULT_ROUTE_ATTEMPTS = 2
OPENALEX_CONTENT_HOST = "content.openalex.org"

DirectDownloader = Callable[[str, Path, Any], None]
OpenAlexDownloader = Callable[[str, str, Path, Any], None]
CheckpointWriter = Callable[[Path, Iterable[dict[str, Any]]], None]


@dataclass(frozen=True)
class _Route:
    kind: str
    provider: str
    host: str
    url: str | None = None
    work_id: str | None = None
    validation_error: str | None = None


@dataclass
class _CandidateState:
    index: int
    destination: Path
    record: dict[str, Any]
    routes: list[_Route]
    next_route: int = 0
    active: bool = False


@dataclass(frozen=True)
class _RouteOutcome:
    ok: bool
    errors: tuple[str, ...] = ()


class _ThreadSessions:
    """Create one reusable HTTP session per executor thread."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self._factory = factory
        self._local = threading.local()
        self._sessions: list[Any] = []
        self._lock = threading.Lock()

    def get(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._factory()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def close(self) -> None:
        for session in self._sessions:
            close = getattr(session, "close", None)
            if callable(close):
                close()


def _requests_session() -> Any:
    import requests

    return requests.Session()


def _default_direct_download(url: str, destination: Path, session: Any) -> None:
    download_licensed_open_access_pdf(url, destination, session=session)


def _default_openalex_download(
    work_id: str,
    api_key: str,
    destination: Path,
    session: Any,
) -> None:
    download_openalex_content_pdf(
        work_id,
        api_key,
        destination,
        session=session,
    )


def _safe_candidate_id(candidate_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", candidate_id)


def _part_path(destination: Path) -> Path:
    return destination.with_suffix(".pdf.part")


def _direct_route(provider: str, url: str) -> _Route:
    try:
        host = validated_pdf_hostname(url)
        error = None
    except ValueError as exc:
        host = "invalid-route"
        error = f"{type(exc).__name__}: {exc}"[:500]
    return _Route(
        kind="direct",
        provider=provider,
        host=host,
        url=url,
        validation_error=error,
    )


def _candidate_routes(
    candidate: dict[str, Any],
    openalex_api_key: str | None,
) -> list[_Route]:
    routes: list[_Route] = []
    seen_urls: set[str] = set()
    direct_routes: list[dict[str, Any]] = []
    if candidate.get("pdf_url"):
        direct_routes.append(
            {
                "provider": candidate.get("provider") or "unknown",
                "url": candidate["pdf_url"],
            }
        )
    direct_routes.extend(candidate.get("alternate_pdf_urls") or [])
    for item in direct_routes:
        url = str(item.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        routes.append(
            _direct_route(
                str(item.get("provider") or candidate.get("provider") or "unknown"),
                url,
            )
        )

    if (
        openalex_api_key
        and candidate.get("openalex_id")
        and candidate.get("has_openalex_pdf")
    ):
        routes.append(
            _Route(
                kind="openalex-content",
                provider="openalex-content",
                host=OPENALEX_CONTENT_HOST,
                work_id=str(candidate["openalex_id"]),
            )
        )
    return routes


def _new_record(candidate: dict[str, Any], destination: Path) -> dict[str, Any]:
    return {
        "candidate_id": candidate["candidate_id"],
        "title": candidate["title"],
        "provider": candidate["provider"],
        "pdf_url": candidate.get("pdf_url") or "",
        "path": str(destination),
        "status": "failed",
        "message": None,
        "attempts": [],
    }


def _run_route(
    route: _Route,
    destination: Path,
    *,
    openalex_api_key: str | None,
    sessions: _ThreadSessions,
    direct_downloader: DirectDownloader,
    openalex_downloader: OpenAlexDownloader,
    valid_pdf_fn: Callable[[Path], bool],
    max_attempts: int,
) -> _RouteOutcome:
    errors: list[str] = []
    for _attempt in range(max_attempts):
        try:
            session = sessions.get()
            if route.kind == "direct":
                assert route.url is not None
                direct_downloader(route.url, destination, session)
            else:
                assert route.work_id is not None
                assert openalex_api_key is not None
                openalex_downloader(
                    route.work_id,
                    openalex_api_key,
                    destination,
                    session,
                )
            if not valid_pdf_fn(destination):
                raise RuntimeError("download route did not produce a valid PDF")
            return _RouteOutcome(ok=True, errors=tuple(errors))
        except Exception as exc:
            destination.unlink(missing_ok=True)
            errors.append(f"{type(exc).__name__}: {exc}"[:500])
        finally:
            _part_path(destination).unlink(missing_ok=True)
    return _RouteOutcome(ok=False, errors=tuple(errors))


def download_candidates_concurrently(
    candidates: Iterable[dict[str, Any]],
    workspace: Path,
    *,
    target_papers: int,
    openalex_api_key: str | None,
    max_downloads: int = DEFAULT_MAX_DOWNLOADS,
    max_downloads_per_host: int = DEFAULT_MAX_DOWNLOADS_PER_HOST,
    max_attempts_per_route: int = DEFAULT_ROUTE_ATTEMPTS,
    direct_downloader: DirectDownloader = _default_direct_download,
    openalex_downloader: OpenAlexDownloader = _default_openalex_download,
    session_factory: Callable[[], Any] = _requests_session,
    valid_pdf_fn: Callable[[Path], bool] = valid_pdf,
    checkpoint_writer: CheckpointWriter = write_jsonl,
) -> list[dict[str, Any]]:
    """Download candidates with deterministic selection and bounded concurrency.

    At most ``target_papers - successful`` candidates are ever reserved. This
    makes it impossible for concurrent completions to download more successful
    PDFs than requested. Results and checkpoints are always ordered by the
    original candidate sequence, regardless of completion order.
    """
    if target_papers <= 0:
        return []
    if max_downloads <= 0:
        raise ValueError("max_downloads must be positive")
    if max_downloads_per_host <= 0:
        raise ValueError("max_downloads_per_host must be positive")
    if max_attempts_per_route <= 0:
        raise ValueError("max_attempts_per_route must be positive")

    candidate_list = list(candidates)
    papers_dir = workspace / "papers"
    papers_dir.mkdir(parents=True, exist_ok=True)
    checkpoint_path = workspace / "download-results.jsonl"

    destinations: list[Path] = []
    destination_owners: dict[Path, str] = {}
    for candidate in candidate_list:
        candidate_id = str(candidate["candidate_id"])
        destination = papers_dir / f"{_safe_candidate_id(candidate_id)}.pdf"
        owner = destination_owners.get(destination)
        if owner is not None:
            raise ValueError(
                "candidates map to the same PDF path: "
                f"{owner!r} and {candidate_id!r}"
            )
        destination_owners[destination] = candidate_id
        destinations.append(destination)

    results: dict[int, dict[str, Any]] = {}
    reserved: dict[int, _CandidateState] = {}
    active: dict[Future[_RouteOutcome], tuple[_CandidateState, _Route]] = {}
    host_activity: dict[str, int] = {}
    sessions = _ThreadSessions(session_factory)
    executor = ThreadPoolExecutor(
        max_workers=max_downloads,
        thread_name_prefix="researchramp-pdf",
    )
    next_index = 0
    successful = 0

    def ordered_results() -> list[dict[str, Any]]:
        return [results[index] for index in sorted(results)]

    def checkpoint(record: dict[str, Any], index: int) -> None:
        results[index] = record
        checkpoint_writer(checkpoint_path, ordered_results())

    def finalize_failed(state: _CandidateState) -> None:
        if state.record["attempts"]:
            state.record["message"] = "; ".join(
                f"{item['provider']}: {item.get('error', item['status'])}"
                for item in state.record["attempts"]
            )[:1000]
        checkpoint(state.record, state.index)
        reserved.pop(state.index, None)

    def admit_candidates() -> None:
        nonlocal next_index, successful
        while (
            successful + len(reserved) < target_papers
            and next_index < len(candidate_list)
        ):
            index = next_index
            next_index += 1
            candidate = candidate_list[index]
            destination = destinations[index]
            _part_path(destination).unlink(missing_ok=True)
            if destination.exists() and valid_pdf_fn(destination):
                record = _new_record(candidate, destination)
                record["status"] = "existing"
                checkpoint(record, index)
                successful += 1
                continue
            if destination.exists():
                destination.unlink(missing_ok=True)
            reserved[index] = _CandidateState(
                index=index,
                destination=destination,
                record=_new_record(candidate, destination),
                routes=_candidate_routes(candidate, openalex_api_key),
            )

    def consume_invalid_and_exhausted() -> bool:
        changed = False
        for state in list(reserved.values()):
            if state.active:
                continue
            while state.next_route < len(state.routes):
                route = state.routes[state.next_route]
                if route.validation_error is None:
                    break
                state.record["attempts"].append(
                    {
                        "provider": route.provider,
                        "status": "failed",
                        "error": route.validation_error,
                    }
                )
                state.next_route += 1
                changed = True
            if state.next_route >= len(state.routes):
                finalize_failed(state)
                changed = True
        return changed

    def submit_ready_routes() -> bool:
        submitted = False
        for state in list(reserved.values()):
            if len(active) >= max_downloads:
                break
            if state.active or state.next_route >= len(state.routes):
                continue
            route = state.routes[state.next_route]
            if host_activity.get(route.host, 0) >= max_downloads_per_host:
                continue
            future = executor.submit(
                _run_route,
                route,
                state.destination,
                openalex_api_key=openalex_api_key,
                sessions=sessions,
                direct_downloader=direct_downloader,
                openalex_downloader=openalex_downloader,
                valid_pdf_fn=valid_pdf_fn,
                max_attempts=max_attempts_per_route,
            )
            state.active = True
            active[future] = (state, route)
            host_activity[route.host] = host_activity.get(route.host, 0) + 1
            submitted = True
        return submitted

    try:
        admit_candidates()
        while reserved or active:
            while consume_invalid_and_exhausted():
                admit_candidates()
            submit_ready_routes()
            if not active:
                if reserved:
                    raise RuntimeError("download scheduler could not make progress")
                break

            completed, _ = wait(tuple(active), return_when=FIRST_COMPLETED)
            for future in sorted(completed, key=lambda item: active[item][0].index):
                state, route = active.pop(future)
                state.active = False
                host_activity[route.host] -= 1
                outcome = future.result()
                for error in outcome.errors:
                    state.record["attempts"].append(
                        {
                            "provider": route.provider,
                            "status": "failed",
                            "error": error,
                        }
                    )
                if outcome.ok:
                    state.record["status"] = "downloaded"
                    state.record["provider"] = route.provider
                    state.record["pdf_url"] = route.url
                    state.record["message"] = None
                    state.record["attempts"].append(
                        {"provider": route.provider, "status": "downloaded"}
                    )
                    checkpoint(state.record, state.index)
                    reserved.pop(state.index, None)
                    successful += 1
                else:
                    state.next_route += 1
            admit_candidates()
    except BaseException:
        for future in active:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)
        for destination in destinations:
            _part_path(destination).unlink(missing_ok=True)
            if destination.exists() and not valid_pdf_fn(destination):
                destination.unlink(missing_ok=True)
        sessions.close()
        raise
    else:
        executor.shutdown(wait=True)
        sessions.close()

    return ordered_results()
