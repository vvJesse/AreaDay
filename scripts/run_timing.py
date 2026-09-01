"""Persistent, process-safe-enough timing records for one corpus workspace.

The initial workflow spans multiple CLI invocations with a host-agent review in
between.  This recorder keeps phase durations beside the workspace so a later
invocation can extend the same diagnostic record without relying on file mtimes.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator

from researchramp_core import read_json, write_json


def _wall_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class RunTimeline:
    """Record named workflow phases and persist after every state transition."""

    def __init__(
        self,
        path: Path,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_now: Callable[[], str] = _wall_now,
    ) -> None:
        self.path = path
        self._monotonic = monotonic
        self._wall_now = wall_now
        self._lock = threading.RLock()
        if path.is_file():
            loaded = read_json(path)
            self._data = loaded if loaded.get("schema_version") == 1 else {}
        else:
            self._data = {}
        self._data.setdefault("schema_version", 1)
        self._data.setdefault("phases", {})

    def _persist(self) -> None:
        write_json(self.path, self._snapshot_unlocked())

    def _snapshot_unlocked(self) -> dict[str, Any]:
        phases = {
            name: dict(value)
            for name, value in self._data["phases"].items()
        }
        started_values = [
            value.get("started_at")
            for value in phases.values()
            if value.get("started_at")
        ]
        finished_values = [
            value.get("finished_at")
            for value in phases.values()
            if value.get("finished_at")
        ]
        workflow: dict[str, Any] = {}
        if started_values:
            workflow["started_at"] = min(started_values)
        if finished_values:
            workflow["finished_at"] = max(finished_values)
        if started_values and finished_values:
            started = datetime.fromisoformat(workflow["started_at"])
            finished = datetime.fromisoformat(workflow["finished_at"])
            workflow["wall_elapsed_seconds"] = round(
                max(0.0, (finished - started).total_seconds()),
                6,
            )
        return {
            "schema_version": self._data["schema_version"],
            "workflow": workflow,
            "phases": phases,
        }

    @contextmanager
    def phase(
        self,
        name: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> Iterator[None]:
        """Measure one phase, persisting both running and terminal states."""
        started = self._monotonic()
        with self._lock:
            previous = self._data["phases"].get(name) or {}
            phase = {
                "attempt": int(previous.get("attempt") or 0) + 1,
                "status": "running",
                "started_at": self._wall_now(),
            }
            if details:
                phase["details"] = details
            self._data["phases"][name] = phase
            self._persist()
        try:
            yield
        except BaseException as error:
            with self._lock:
                phase.update(
                    status="failed",
                    finished_at=self._wall_now(),
                    elapsed_seconds=round(self._monotonic() - started, 6),
                    error_type=type(error).__name__,
                )
                self._persist()
            raise
        else:
            with self._lock:
                phase.update(
                    status="ok",
                    finished_at=self._wall_now(),
                    elapsed_seconds=round(self._monotonic() - started, 6),
                )
                self._persist()

    def record(
        self,
        name: str,
        *,
        started_at: str,
        finished_at: str,
        elapsed_seconds: float,
        status: str = "ok",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a phase measured by an external coordinator."""
        with self._lock:
            previous = self._data["phases"].get(name) or {}
            phase: dict[str, Any] = {
                "attempt": int(previous.get("attempt") or 0) + 1,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "elapsed_seconds": round(float(elapsed_seconds), 6),
            }
            if details:
                phase["details"] = details
            self._data["phases"][name] = phase
            self._persist()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_unlocked()
