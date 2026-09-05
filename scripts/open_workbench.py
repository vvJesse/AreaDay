#!/usr/bin/env python3
"""Reuse or start the local AreaDay workbench and print its exact URL."""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from http import HTTPStatus
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from domain_registry import (
    DomainRegistry,
    default_registry_path,
    validate_completed_workspace,
    validate_corpus_launch_workspace,
)
from remote_calibration import InvalidCalibrationData
from researchramp_license import enforce_business_license
from workbench_protocol import (
    DEFAULT_WORKBENCH_IDLE_TIMEOUT_SECONDS,
    WORKBENCH_IDENTITY_PATH,
    WORKBENCH_IDENTITY_VERSION,
    WORKBENCH_SERVICE,
    WORKBENCH_SHUTDOWN_PATH,
)


HOST = "127.0.0.1"
PORT = 8765
VIEWS = ("vocabulary", "briefs", "review")
PROBE_TIMEOUT_SECONDS = 0.4
FALLBACK_PORT_COUNT = 9
STARTUP_TIMEOUT_SECONDS = 12.0
EXIT_CONVERGENCE_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.1
LOG_TAIL_BYTES = 16_384
STALE_SHUTDOWN_TIMEOUT_SECONDS = 3.0


class ProbeKind(str, Enum):
    ABSENT = "absent"
    UNRESOLVED = "unresolved"
    MATCH = "match"
    OTHER_REGISTRY = "other_registry"
    STALE_RUNTIME = "stale_runtime"
    INCOMPATIBLE = "incompatible"
    OCCUPIED_UNKNOWN = "occupied_unknown"


@dataclass(frozen=True)
class ProbeResult:
    kind: ProbeKind
    identity: dict[str, Any] | None = None
    detail: str = ""


@dataclass(frozen=True)
class LaunchAttempt:
    process: subprocess.Popen[bytes]
    instance_id: str
    log_path: Path


class WorkbenchConflict(RuntimeError):
    """No candidate port can host or reuse a compatible live service."""


class WorkbenchAccessError(RuntimeError):
    """The launcher could not inspect the loopback workbench endpoint."""


class WorkbenchStartupError(RuntimeError):
    """A workbench created by this launcher did not become ready."""


class WorkbenchCleanupError(RuntimeError):
    """A launcher-owned child could not be confirmed stopped."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", help="Explicit registered domain ID.")
    parser.add_argument("--view", choices=VIEWS, default="vocabulary")
    parser.add_argument("--registry", type=Path)
    parser.add_argument(
        "--port",
        type=int,
        default=PORT,
        help="Preferred workbench port; nearby fallbacks are selected automatically.",
    )
    parser.add_argument(
        "--ready-calibration-domain",
        help=(
            "Include one registered domain whose vocabulary and terminology are "
            "ready while its 30-answer calibration is still incomplete."
        ),
    )
    parser.add_argument(
        "--idle-timeout-seconds",
        type=int,
        default=DEFAULT_WORKBENCH_IDLE_TIMEOUT_SECONDS,
        help="Stop after this many seconds without user activity; zero disables it.",
    )
    args = parser.parse_args()
    if args.idle_timeout_seconds < 0:
        parser.error("--idle-timeout-seconds must be zero or positive")
    return args


def _connection_was_refused(error: OSError) -> bool:
    return isinstance(error, ConnectionRefusedError) or error.errno in {
        errno.ECONNREFUSED,
        10061,  # WSAECONNREFUSED
    }


def _address_is_in_use(error: OSError) -> bool:
    return error.errno in {
        errno.EADDRINUSE,
        10048,  # WSAEADDRINUSE
    }


def _address_is_denied(error: OSError) -> bool:
    return error.errno in {
        errno.EACCES,
        errno.EPERM,
        10013,  # WSAEACCES (for example, an excluded Windows port)
    }


def _probe_bindability(port: int) -> ProbeResult | None:
    """Return a decisive bind result, or ``None`` when a live owner may exist."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind((HOST, port))
        return ProbeResult(ProbeKind.ABSENT)
    except OSError as error:
        if _address_is_in_use(error):
            return None
        if _address_is_denied(error):
            return ProbeResult(
                ProbeKind.OCCUPIED_UNKNOWN,
                detail=f"AreaDay cannot bind this port: {type(error).__name__}: {error}",
            )
        raise WorkbenchAccessError(
            f"AreaDay could not inspect loopback port {port}: {error}"
        ) from error
    finally:
        listener.close()


def probe_workbench(
    port: int,
    registry_path: Path,
    *,
    expected_domain_ids: tuple[str, ...] | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Identify a live owner, using bindability as the availability authority."""

    bind_result = _probe_bindability(port)
    if bind_result is not None:
        return bind_result

    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.connect()
    except OSError as error:
        connection.close()
        if _connection_was_refused(error):
            return ProbeResult(ProbeKind.ABSENT)
        if isinstance(error, TimeoutError):
            return ProbeResult(
                ProbeKind.UNRESOLVED,
                detail=f"TCP connection timed out: {error}",
            )
        if isinstance(error, PermissionError) or error.errno in {
            errno.EACCES,
            errno.EPERM,
        }:
            raise WorkbenchAccessError(
                "AreaDay could not access its loopback identity endpoint at "
                f"http://{HOST}:{port}{WORKBENCH_IDENTITY_PATH}: {error}. "
                "No service was accepted or left running."
            ) from error
        return ProbeResult(
            ProbeKind.UNRESOLVED,
            detail=f"TCP connection failed: {type(error).__name__}: {error}",
        )

    try:
        connection.request(
            "GET",
            WORKBENCH_IDENTITY_PATH,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(65_537)
    except OSError as error:
        if isinstance(error, PermissionError) or error.errno in {
            errno.EACCES,
            errno.EPERM,
        }:
            raise WorkbenchAccessError(
                "AreaDay could not access its loopback identity endpoint at "
                f"http://{HOST}:{port}{WORKBENCH_IDENTITY_PATH}: {error}. "
                "No service was accepted or left running."
            ) from error
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail=f"{type(error).__name__}: {error}",
        )
    except http.client.HTTPException as error:
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail=f"{type(error).__name__}: {error}",
        )
    finally:
        connection.close()

    if response.status != 200:
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail=f"identity endpoint returned HTTP {response.status}",
        )
    if len(body) > 65_536:
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail="identity response is too large",
        )
    try:
        identity = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail=f"identity response is not valid JSON: {error}",
        )
    if not isinstance(identity, dict):
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            detail="identity response is not an object",
        )

    service = identity.get("service")
    if service != WORKBENCH_SERVICE:
        return ProbeResult(
            ProbeKind.OCCUPIED_UNKNOWN,
            identity=identity,
            detail="service identity does not match AreaDay",
        )
    identity_version = identity.get("identity_version")
    if (
        type(identity_version) is not int
        or identity_version != WORKBENCH_IDENTITY_VERSION
    ):
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail="AreaDay workbench identity version is incompatible",
        )

    actual_registry = identity.get("registry")
    instance_id = identity.get("instance_id")
    if actual_registry is None:
        return ProbeResult(
            ProbeKind.OTHER_REGISTRY,
            identity=identity,
            detail="AreaDay is running in standalone mode",
        )
    if not isinstance(actual_registry, str) or not actual_registry.strip():
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail="AreaDay identity has no valid registry path",
        )
    if not isinstance(instance_id, str) or not instance_id.strip():
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail="AreaDay identity has no valid instance ID",
        )

    registry_identity = Path(actual_registry).expanduser()
    if not registry_identity.is_absolute():
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail="AreaDay registry identity is not an absolute path",
        )

    expected_registry = registry_path.expanduser().resolve()
    try:
        resolved_registry = registry_identity.resolve()
    except (OSError, RuntimeError) as error:
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail=f"AreaDay registry identity is invalid: {error}",
        )
    if resolved_registry != expected_registry:
        return ProbeResult(
            ProbeKind.OTHER_REGISTRY,
            identity=identity,
            detail=str(resolved_registry),
        )

    actual_domain_ids = identity.get("domain_ids")
    if (
        not isinstance(actual_domain_ids, list)
        or not actual_domain_ids
        or any(
            not isinstance(domain_id, str) or not domain_id.strip()
            for domain_id in actual_domain_ids
        )
        or actual_domain_ids != sorted(set(actual_domain_ids))
    ):
        return ProbeResult(
            ProbeKind.INCOMPATIBLE,
            identity=identity,
            detail="AreaDay identity has no valid sorted domain ID list",
        )
    if expected_domain_ids is not None:
        expected = tuple(sorted(set(expected_domain_ids)))
        if tuple(actual_domain_ids) != expected:
            return ProbeResult(
                ProbeKind.STALE_RUNTIME,
                identity=identity,
                detail=(
                    f"loaded={actual_domain_ids!r}, expected={list(expected)!r}"
                ),
            )
    return ProbeResult(ProbeKind.MATCH, identity=identity)


def launchable_registry_domain_ids(
    registry: DomainRegistry,
    ready_calibration_domain: str | None = None,
) -> tuple[str, ...]:
    if not registry.domains:
        raise RuntimeError(
            "No AreaDay domain is registered; run initialization first"
        )
    completed: list[str] = []
    for item in registry.domains:
        try:
            if item.domain_id == ready_calibration_domain:
                validate_corpus_launch_workspace(Path(item.workspace))
            else:
                validate_completed_workspace(Path(item.workspace))
        except (FileNotFoundError, InvalidCalibrationData):
            continue
        completed.append(item.domain_id)
    return tuple(completed)


def completed_registry_domain_ids(registry: DomainRegistry) -> tuple[str, ...]:
    return launchable_registry_domain_ids(registry)


def select_registry_domain(
    registry: DomainRegistry,
    requested: str | None,
    completed: tuple[str, ...] | None = None,
) -> str:
    completed = completed or completed_registry_domain_ids(registry)
    if requested is not None:
        registry.get(requested)
        if requested not in completed:
            raise RuntimeError(
                f"AreaDay domain is registered but initialization is incomplete: {requested}"
            )
        return requested
    if not completed:
        raise RuntimeError(
            "AreaDay domains are registered, but none has completed initialization"
        )
    if registry.active_domain_id in completed:
        return str(registry.active_domain_id)
    return completed[0]


def workbench_url(port: int, domain_id: str, view: str) -> str:
    encoded = quote(domain_id, safe="._-")
    return f"http://{HOST}:{port}/?domain={encoded}#{view}"


def _launch_log_path(port: int, instance_id: str) -> Path:
    user_id = str(os.getuid()) if hasattr(os, "getuid") else "user"
    return (
        Path(tempfile.gettempdir())
        / f"researchramp-workbench-{user_id}-{port}-{instance_id}.log"
    )


def start_workbench(
    registry_path: Path,
    domain_id: str,
    view: str,
    port: int,
    *,
    ready_calibration_domain: str | None = None,
    idle_timeout_seconds: int = DEFAULT_WORKBENCH_IDLE_TIMEOUT_SECONDS,
) -> LaunchAttempt:
    if isinstance(idle_timeout_seconds, bool) or idle_timeout_seconds < 0:
        raise ValueError("Workbench idle timeout must be zero or positive")
    skill_root = Path(__file__).resolve().parents[1]
    server = skill_root / "app" / "server.py"
    instance_id = uuid.uuid4().hex
    log_path = _launch_log_path(port, instance_id)
    command = [
        sys.executable,
        str(server),
        "--library",
        str(registry_path.expanduser().resolve()),
        "--domain",
        domain_id,
        "--mode",
        view,
        "--host",
        HOST,
        "--port",
        str(port),
        "--instance-id",
        instance_id,
        "--no-browser",
        "--idle-timeout-seconds",
        str(idle_timeout_seconds),
    ]
    if ready_calibration_domain is not None:
        command.extend(
            ["--ready-calibration-domain", ready_calibration_domain]
        )
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    process: subprocess.Popen[bytes] | None = None
    try:
        with log_path.open("xb") as log:
            process = subprocess.Popen(
                command,
                cwd=skill_root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=flags,
            )
        return LaunchAttempt(
            process=process,
            instance_id=instance_id,
            log_path=log_path,
        )
    except BaseException as error:
        if process is not None:
            try:
                stop_launch_attempt(
                    LaunchAttempt(process, instance_id, log_path)
                )
            except WorkbenchCleanupError as cleanup_error:
                raise cleanup_error from error
        if isinstance(error, (OSError, ValueError)):
            raise WorkbenchStartupError(
                "AreaDay could not create its workbench process. "
                f"Log: {log_path}. Cause: {error}"
            ) from error
        raise


def stop_launch_attempt(attempt: LaunchAttempt) -> None:
    """Stop and reap only the child process created by this launcher."""

    process = attempt.process
    if process.poll() is not None:
        return

    failures: list[str] = []
    try:
        process.terminate()
    except ProcessLookupError:
        pass
    except OSError as error:
        failures.append(f"terminate failed: {error}")

    try:
        process.wait(timeout=2.0)
        return
    except (subprocess.TimeoutExpired, ChildProcessError, OSError) as error:
        failures.append(f"wait after terminate failed: {error}")
    if process.poll() is not None:
        return

    try:
        process.kill()
    except ProcessLookupError:
        pass
    except OSError as error:
        failures.append(f"kill failed: {error}")

    try:
        process.wait(timeout=2.0)
        return
    except (subprocess.TimeoutExpired, ChildProcessError, OSError) as error:
        failures.append(f"wait after kill failed: {error}")
    if process.poll() is not None:
        return

    process_id = getattr(process, "pid", "unknown")
    detail = "; ".join(failures) or "process remains live"
    raise WorkbenchCleanupError(
        "AreaDay could not confirm that its launcher-owned child stopped. "
        f"PID: {process_id}. Log: {attempt.log_path}. Cause: {detail}"
    )


def startup_log_excerpt(log_path: Path) -> str:
    try:
        with log_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - LOG_TAIL_BYTES))
            payload = stream.read(LOG_TAIL_BYTES)
    except OSError:
        return ""
    return payload[-LOG_TAIL_BYTES:].decode("utf-8", errors="replace").strip()


def retire_stale_workbench(
    port: int,
    result: ProbeResult,
    *,
    timeout: float = STALE_SHUTDOWN_TIMEOUT_SECONDS,
) -> bool:
    """Ask one verified same-registry stale instance to release its port."""

    identity = result.identity
    if result.kind is not ProbeKind.STALE_RUNTIME or not isinstance(identity, dict):
        return False
    instance_id = identity.get("instance_id")
    registry = identity.get("registry")
    if not isinstance(instance_id, str) or not instance_id:
        return False
    if not isinstance(registry, str) or not registry:
        return False
    encoded = json.dumps({"instance_id": instance_id}).encode("utf-8")
    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request(
            "POST",
            WORKBENCH_SHUTDOWN_PATH,
            body=encoded,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Content-Length": str(len(encoded)),
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        body = response.read(4_097)
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    if response.status != HTTPStatus.OK or len(body) > 4_096:
        return False
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError):
        return False
    if not isinstance(payload, dict) or payload.get("status") != "shutting_down":
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        availability = probe_workbench(
            port,
            Path(registry),
            timeout=min(PROBE_TIMEOUT_SECONDS, max(0.05, deadline - time.monotonic())),
        )
        if availability.kind is ProbeKind.ABSENT:
            return True
        time.sleep(POLL_INTERVAL_SECONDS)
    return probe_workbench(
        port,
        Path(registry),
        timeout=PROBE_TIMEOUT_SECONDS,
    ).kind is ProbeKind.ABSENT


def _conflict(port: int, result: ProbeResult) -> WorkbenchConflict:
    if result.kind is ProbeKind.OTHER_REGISTRY:
        return WorkbenchConflict(
            f"Port {port} is running AreaDay for another registry: {result.detail}"
        )
    if result.kind is ProbeKind.INCOMPATIBLE:
        return WorkbenchConflict(
            f"Port {port} is running an incompatible AreaDay service: {result.detail}"
        )
    if result.kind is ProbeKind.STALE_RUNTIME:
        return WorkbenchConflict(
            f"Port {port} is running AreaDay for this registry, but its "
            "loaded domain set is stale. Stop that workbench and open it again. "
            f"Details: {result.detail}"
        )
    return WorkbenchConflict(
        f"Port {port} is occupied by a service that cannot be identified as "
        f"AreaDay: {result.detail or 'unknown response'}"
    )


def _result(
    status: str,
    domain_id: str,
    view: str,
    port: int,
    identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": status,
        "instance_id": identity["instance_id"],
        "domain_id": domain_id,
        "port": port,
        "url": workbench_url(port, domain_id, view),
    }


def candidate_ports(preferred_port: int, fallback_port_count: int) -> tuple[int, ...]:
    if not 1 <= preferred_port <= 65_535:
        raise ValueError("Workbench port must be between 1 and 65535")
    if fallback_port_count < 0:
        raise ValueError("fallback_port_count must be non-negative")
    final_port = min(65_535, preferred_port + fallback_port_count)
    return tuple(range(preferred_port, final_port + 1))


def _candidate_conflicts(
    ports: tuple[int, ...],
    results: dict[int, ProbeResult],
) -> WorkbenchConflict:
    details = "; ".join(
        f"{candidate}: {results[candidate].kind.value}"
        + (f" ({results[candidate].detail})" if results[candidate].detail else "")
        for candidate in ports
    )
    return WorkbenchConflict(
        "No compatible AreaDay workbench port is available in the candidate "
        f"range {ports[0]}-{ports[-1]}. Details: {details}"
    )


def _start_on_candidate(
    registry_path: Path,
    domain_id: str,
    view: str,
    port: int,
    initial: ProbeResult,
    *,
    probe: Callable[[int, Path], ProbeResult],
    starter: Callable[[Path, str, str, int], LaunchAttempt],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    startup_timeout: float,
) -> dict[str, Any]:
    """Start one candidate and converge on the process that actually bound it."""

    attempt = starter(registry_path, domain_id, view, port)
    keep_child = False
    try:
        deadline = monotonic() + startup_timeout
        exit_grace_deadline: float | None = None
        last_probe = initial
        while True:
            current = probe(port, registry_path)
            last_probe = current
            if current.kind is ProbeKind.MATCH:
                assert current.identity is not None
                if current.identity["instance_id"] == attempt.instance_id:
                    keep_child = True
                    return _result("started", domain_id, view, port, current.identity)
                return _result("reused", domain_id, view, port, current.identity)
            if current.kind in {
                ProbeKind.OTHER_REGISTRY,
                ProbeKind.STALE_RUNTIME,
                ProbeKind.INCOMPATIBLE,
            }:
                raise _conflict(port, current)

            now = monotonic()
            return_code = attempt.process.poll()
            if return_code is not None:
                if exit_grace_deadline is None:
                    exit_grace_deadline = min(
                        deadline, now + EXIT_CONVERGENCE_SECONDS
                    )
                if now >= exit_grace_deadline:
                    excerpt = startup_log_excerpt(attempt.log_path)
                    if last_probe.kind is ProbeKind.OCCUPIED_UNKNOWN:
                        conflict = _conflict(port, last_probe)
                        if excerpt:
                            conflict = WorkbenchConflict(
                                f"{conflict}\nLauncher child log: {attempt.log_path}\n"
                                f"{excerpt}"
                            )
                        raise conflict
                    if last_probe.kind is ProbeKind.UNRESOLVED:
                        raise _conflict(port, last_probe)
                    message = (
                        "The AreaDay workbench stopped during startup "
                        f"with exit code {return_code}. Last port state: "
                        f"{last_probe.kind.value}"
                    )
                    if last_probe.detail:
                        message += f" ({last_probe.detail})"
                    message += f". Log: {attempt.log_path}"
                    if excerpt:
                        message += f"\n{excerpt}"
                    raise WorkbenchStartupError(message)

            if now >= deadline:
                final_probe = probe(port, registry_path)
                last_probe = final_probe
                if final_probe.kind is ProbeKind.MATCH:
                    assert final_probe.identity is not None
                    if final_probe.identity["instance_id"] == attempt.instance_id:
                        keep_child = True
                        return _result(
                            "started", domain_id, view, port, final_probe.identity
                        )
                    return _result(
                        "reused", domain_id, view, port, final_probe.identity
                    )
                if final_probe.kind in {
                    ProbeKind.OTHER_REGISTRY,
                    ProbeKind.STALE_RUNTIME,
                    ProbeKind.INCOMPATIBLE,
                    ProbeKind.OCCUPIED_UNKNOWN,
                    ProbeKind.UNRESOLVED,
                }:
                    raise _conflict(port, final_probe)
                excerpt = startup_log_excerpt(attempt.log_path)
                message = (
                    "The AreaDay workbench did not become ready in time. "
                    f"Last port state: {last_probe.kind.value}. Log: {attempt.log_path}"
                )
                if excerpt:
                    message += f"\n{excerpt}"
                raise WorkbenchStartupError(message)
            sleep(POLL_INTERVAL_SECONDS)
    finally:
        if not keep_child:
            stop_launch_attempt(attempt)


def ensure_workbench(
    registry_path: Path,
    domain_id: str,
    view: str,
    port: int,
    *,
    expected_domain_ids: tuple[str, ...] | None = None,
    probe: Callable[[int, Path], ProbeResult] | None = None,
    starter: Callable[[Path, str, str, int], LaunchAttempt] | None = None,
    monotonic: Callable[[], float] | None = None,
    sleep: Callable[[float], None] | None = None,
    startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    fallback_port_count: int = FALLBACK_PORT_COUNT,
    retire_stale: Callable[[int, ProbeResult], bool] | None = None,
) -> dict[str, Any]:
    """Reuse or start one workbench, preferring ``port`` over fallbacks."""

    expected_domain_ids = tuple(
        sorted(set(expected_domain_ids or (domain_id,)))
    )
    if domain_id not in expected_domain_ids:
        raise ValueError(
            f"Selected AreaDay domain is not in the completed set: {domain_id}"
        )
    if probe is None:
        def probe_live(candidate_port: int, candidate_registry: Path) -> ProbeResult:
            return probe_workbench(
                candidate_port,
                candidate_registry,
                expected_domain_ids=expected_domain_ids,
            )

        probe = probe_live
    starter = starter or start_workbench
    monotonic = monotonic or time.monotonic
    sleep = sleep or time.sleep
    retire_stale = retire_stale or retire_stale_workbench
    registry_path = registry_path.expanduser().resolve()
    ports = candidate_ports(port, fallback_port_count)

    initial_results: dict[int, ProbeResult] = {}
    matching_port: int | None = None
    for candidate_port in ports:
        initial = probe(candidate_port, registry_path)
        initial_results[candidate_port] = initial
        if initial.kind is ProbeKind.MATCH and matching_port is None:
            matching_port = candidate_port

    for candidate_port, initial in initial_results.items():
        if initial.kind is not ProbeKind.STALE_RUNTIME:
            continue
        if retire_stale(candidate_port, initial):
            initial_results[candidate_port] = probe(candidate_port, registry_path)

    if matching_port is not None:
        matching = initial_results[matching_port]
        assert matching.identity is not None
        return _result("reused", domain_id, view, matching_port, matching.identity)

    conflicts: dict[int, ProbeResult] = {}
    for candidate_port in ports:
        initial = initial_results[candidate_port]
        if initial.kind not in {ProbeKind.ABSENT, ProbeKind.UNRESOLVED}:
            conflicts[candidate_port] = initial
            continue
        try:
            return _start_on_candidate(
                registry_path,
                domain_id,
                view,
                candidate_port,
                initial,
                probe=probe,
                starter=starter,
                monotonic=monotonic,
                sleep=sleep,
                startup_timeout=startup_timeout,
            )
        except WorkbenchConflict:
            latest = probe(candidate_port, registry_path)
            if latest.kind is ProbeKind.MATCH:
                assert latest.identity is not None
                return _result(
                    "reused",
                    domain_id,
                    view,
                    candidate_port,
                    latest.identity,
                )
            conflicts[candidate_port] = latest
            if len(ports) == 1:
                raise

    if len(ports) == 1:
        raise _conflict(port, initial_results[port])
    raise _candidate_conflicts(ports, {**initial_results, **conflicts})


def main() -> None:
    args = parse_args()
    enforce_business_license("workbench")
    registry_path = (
        args.registry.expanduser().resolve()
        if args.registry is not None
        else default_registry_path()
    )
    registry = DomainRegistry(registry_path)
    ready_calibration_domain = getattr(
        args, "ready_calibration_domain", None
    )
    if (
        ready_calibration_domain is not None
        and args.domain != ready_calibration_domain
    ):
        raise RuntimeError(
            "--ready-calibration-domain must equal the explicitly selected --domain"
        )
    calibration_domain = ready_calibration_domain or args.domain
    idle_timeout_seconds = getattr(
        args,
        "idle_timeout_seconds",
        DEFAULT_WORKBENCH_IDLE_TIMEOUT_SECONDS,
    )
    completed_domain_ids = launchable_registry_domain_ids(
        registry,
        calibration_domain,
    )
    if not completed_domain_ids and calibration_domain is None:
        calibration_domain = registry.active_domain_id
        completed_domain_ids = launchable_registry_domain_ids(
            registry,
            calibration_domain,
        )
    domain_id = select_registry_domain(
        registry,
        args.domain,
        completed_domain_ids,
    )
    starter = lambda path, domain, view, port: start_workbench(
        path,
        domain,
        view,
        port,
        ready_calibration_domain=calibration_domain,
        idle_timeout_seconds=idle_timeout_seconds,
    )
    ensure_kwargs: dict[str, Any] = {
        "expected_domain_ids": completed_domain_ids,
        "starter": starter,
    }
    result = ensure_workbench(
        registry_path,
        domain_id,
        args.view,
        args.port,
        **ensure_kwargs,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
