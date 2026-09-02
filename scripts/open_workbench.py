#!/usr/bin/env python3
"""Reuse or start the local AreaDay workbench and print its exact URL."""

from __future__ import annotations

import argparse
import errno
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from domain_registry import (
    DomainRegistry,
    default_registry_path,
    validate_completed_workspace,
    validate_initialized_workspace,
)
from researchramp_license import enforce_business_license
from workbench_protocol import (
    WORKBENCH_IDENTITY_PATH,
    WORKBENCH_IDENTITY_VERSION,
    WORKBENCH_SERVICE,
)


HOST = "127.0.0.1"
PORT = 8765
VIEWS = ("vocabulary", "briefs", "review")
PROBE_TIMEOUT_SECONDS = 0.4
STARTUP_TIMEOUT_SECONDS = 12.0
EXIT_CONVERGENCE_SECONDS = 0.5
POLL_INTERVAL_SECONDS = 0.1
LOG_TAIL_BYTES = 16_384


class ProbeKind(str, Enum):
    ABSENT = "absent"
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
    """The fixed workbench port is owned by an incompatible live service."""


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
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument(
        "--ready-calibration-domain",
        help=(
            "Include one registered domain whose vocabulary and terminology are "
            "ready while its 30-answer calibration is still incomplete."
        ),
    )
    return parser.parse_args()


def _connection_was_refused(error: OSError) -> bool:
    return isinstance(error, ConnectionRefusedError) or error.errno in {
        errno.ECONNREFUSED,
        10061,  # WSAECONNREFUSED
    }


def probe_workbench(
    port: int,
    registry_path: Path,
    *,
    expected_domain_ids: tuple[str, ...] | None = None,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Identify the live port owner; only explicit refusal means no service."""

    connection = http.client.HTTPConnection(HOST, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            WORKBENCH_IDENTITY_PATH,
            headers={"Accept": "application/json", "Connection": "close"},
        )
        response = connection.getresponse()
        body = response.read(65_537)
    except OSError as error:
        if _connection_was_refused(error):
            return ProbeResult(ProbeKind.ABSENT)
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
                validate_initialized_workspace(Path(item.workspace))
            else:
                validate_completed_workspace(Path(item.workspace))
        except FileNotFoundError:
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
) -> LaunchAttempt:
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
        "url": workbench_url(port, domain_id, view),
    }


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
) -> dict[str, Any]:
    """Converge on exactly one compatible live workbench for this registry."""

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
    registry_path = registry_path.expanduser().resolve()

    initial = probe(port, registry_path)
    if initial.kind is ProbeKind.MATCH:
        assert initial.identity is not None
        return _result("reused", domain_id, view, port, initial.identity)
    if initial.kind is not ProbeKind.ABSENT:
        raise _conflict(port, initial)

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
    completed_domain_ids = launchable_registry_domain_ids(
        registry,
        ready_calibration_domain,
    )
    domain_id = select_registry_domain(
        registry,
        args.domain,
        completed_domain_ids,
    )
    starter = None
    if ready_calibration_domain is not None:
        starter = lambda path, domain, view, port: start_workbench(
            path,
            domain,
            view,
            port,
            ready_calibration_domain=ready_calibration_domain,
        )
    ensure_kwargs: dict[str, Any] = {
        "expected_domain_ids": completed_domain_ids,
    }
    if starter is not None:
        ensure_kwargs["starter"] = starter
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
