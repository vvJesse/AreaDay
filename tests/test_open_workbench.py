from __future__ import annotations

import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


SKILL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_ROOT / "scripts"
FIXTURE_SERVER = Path(__file__).with_name("workbench_identity_fixture.py")
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import open_workbench as launcher  # noqa: E402


def identity(
    registry: Path,
    instance_id: str,
    domain_ids: tuple[str, ...] = ("domain-a",),
) -> dict[str, object]:
    return {
        "service": "researchramp-workbench",
        "identity_version": launcher.WORKBENCH_IDENTITY_VERSION,
        "registry": str(registry.expanduser().resolve()),
        "instance_id": instance_id,
        "domain_ids": sorted(domain_ids),
    }


def match(registry: Path, instance_id: str) -> launcher.ProbeResult:
    return launcher.ProbeResult(
        launcher.ProbeKind.MATCH,
        identity=identity(registry, instance_id),
    )


def unused_port() -> int:
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            port = int(listener.getsockname()[1])
        if port != launcher.PORT:
            return port


def create_minimal_completed_workspace(
    root: Path,
    domain_id: str,
) -> Path:
    """Create only the persisted product contract needed by the real server."""

    workspace = root / domain_id
    analysis = workspace / "analysis"
    analysis.mkdir(parents=True)
    (workspace / "research-profile-input.json").write_text(
        json.dumps(
            {
                "version": 1,
                "profile_id": domain_id,
                "title": f"Domain {domain_id}",
                "confirmed": True,
            }
        ),
        encoding="utf-8",
    )
    lemmas = [f"researchword{index}" for index in range(30)]
    (analysis / "vocabulary-map.tsv").write_text(
        "lemma\tpart_of_speech\ttotal_count\tdocument_count\tdocument_share\n"
        + "".join(
            f"{lemma}\tnoun\t{40 - index}\t10\t0.5\n"
            for index, lemma in enumerate(lemmas)
        ),
        encoding="utf-8",
    )
    (analysis / "papers.jsonl").write_text("", encoding="utf-8")
    (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
    (analysis / "terminology-explanations.json").write_text("{}\n", encoding="utf-8")
    (analysis / "host-review-summary.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "reviewer": "current-host-agent",
                "review_passes": 1,
                "terminology_candidate_count": 0,
                "selected_terminology_count": 0,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (analysis / "vocabulary-calibration-session.json").write_text(
        json.dumps(
            {
                "answers": [
                    {"lemma": lemma, "response": "known"}
                    for lemma in lemmas
                ]
            }
        ),
        encoding="utf-8",
    )
    export_content = (
        "lemma\timportance_tier\tclassification\n"
        f"{lemmas[0]}\tA\timportant_boundary\n"
    )
    (analysis / "personalized-vocabulary.tsv").write_text(
        export_content,
        encoding="utf-8",
    )
    (analysis / "vocabulary-calibration-result.json").write_text(
        json.dumps(
            {
                "counts": {
                    "total": 1,
                    "important_boundary_protected": 1,
                },
                "threshold": {},
                "importance": {},
                "vocabulary_snapshot_sha256": "fixture-snapshot",
                "personalized_vocabulary_sha256": hashlib.sha256(
                    export_content.encode("utf-8")
                ).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return workspace


class ScriptedProbe:
    def __init__(self, *results: launcher.ProbeResult):
        if not results:
            raise ValueError("ScriptedProbe requires at least one result")
        self.results = deque(results)
        self.last = results[-1]
        self.calls: list[tuple[int, Path]] = []

    def __call__(self, port: int, registry: Path) -> launcher.ProbeResult:
        self.calls.append((port, registry))
        if self.results:
            self.last = self.results.popleft()
        return self.last


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeProcess:
    def __init__(self, return_code: int | None = None):
        self.return_code = return_code
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.return_code = -15

    def kill(self) -> None:
        self.kill_calls += 1
        self.return_code = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.return_code is None:
            self.return_code = 0
        return self.return_code


class UnstoppableFakeProcess(FakeProcess):
    pid = 4242

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        raise subprocess.TimeoutExpired("fake-child", timeout)


class PermissionDeniedFakeProcess(UnstoppableFakeProcess):
    pid = 4343

    def terminate(self) -> None:
        self.terminate_calls += 1
        raise PermissionError("terminate denied")

    def kill(self) -> None:
        self.kill_calls += 1
        raise PermissionError("kill denied")


class StateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.registry = self.root / "registry.json"
        self.registry.write_text("{}\n", encoding="utf-8")
        self.log_path = self.root / "attempt.log"
        self.log_path.write_text("", encoding="utf-8")

    def attempt(
        self,
        instance_id: str = "own-instance",
        *,
        return_code: int | None = None,
    ) -> tuple[launcher.LaunchAttempt, FakeProcess]:
        process = FakeProcess(return_code)
        attempt = launcher.LaunchAttempt(
            process=process,  # type: ignore[arg-type]
            instance_id=instance_id,
            log_path=self.log_path,
        )
        return attempt, process

    def run_ensure(
        self,
        probe: ScriptedProbe,
        attempt: launcher.LaunchAttempt,
        clock: FakeClock | None = None,
        *,
        timeout: float = 2.0,
    ) -> dict[str, object]:
        clock = clock or FakeClock()
        return launcher.ensure_workbench(
            self.registry,
            "domain-a",
            "vocabulary",
            43123,
            probe=probe,
            starter=lambda *_: attempt,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            startup_timeout=timeout,
        )

    def test_absent_service_starts_own_instance(self) -> None:
        attempt, process = self.attempt()
        probe = ScriptedProbe(
            launcher.ProbeResult(launcher.ProbeKind.ABSENT),
            match(self.registry, "own-instance"),
        )

        result = self.run_ensure(probe, attempt)

        self.assertEqual(result["status"], "started")
        self.assertEqual(result["instance_id"], "own-instance")
        self.assertEqual(process.terminate_calls, 0)

    def test_not_ready_during_startup_is_not_misclassified(self) -> None:
        attempt, process = self.attempt()
        absent = launcher.ProbeResult(launcher.ProbeKind.ABSENT)
        probe = ScriptedProbe(absent, absent, absent, absent, match(self.registry, "own-instance"))
        clock = FakeClock()

        result = self.run_ensure(probe, attempt, clock)

        self.assertEqual(result["status"], "started")
        self.assertGreaterEqual(len(clock.sleeps), 3)
        self.assertEqual(process.terminate_calls, 0)

    def test_bound_but_not_ready_child_can_converge_to_own_identity(self) -> None:
        attempt, process = self.attempt()
        probe = ScriptedProbe(
            launcher.ProbeResult(launcher.ProbeKind.ABSENT),
            launcher.ProbeResult(
                launcher.ProbeKind.OCCUPIED_UNKNOWN,
                detail="identity endpoint returned HTTP 503",
            ),
            match(self.registry, "own-instance"),
        )

        result = self.run_ensure(probe, attempt)

        self.assertEqual(result["status"], "started")
        self.assertEqual(process.terminate_calls, 0)

    def test_matching_live_service_is_reused_without_starting(self) -> None:
        starter = mock.Mock(side_effect=AssertionError("starter must not run"))
        result = launcher.ensure_workbench(
            self.registry,
            "domain-a",
            "review",
            43124,
            probe=ScriptedProbe(match(self.registry, "running-instance")),
            starter=starter,
        )

        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["instance_id"], "running-instance")
        self.assertTrue(str(result["url"]).endswith("?domain=domain-a#review"))
        starter.assert_not_called()

    def test_initial_unknown_service_conflicts_without_starting(self) -> None:
        starter = mock.Mock(side_effect=AssertionError("starter must not run"))
        probe = ScriptedProbe(
            launcher.ProbeResult(
                launcher.ProbeKind.OCCUPIED_UNKNOWN,
                detail="HTTP 404",
            )
        )

        with self.assertRaises(launcher.WorkbenchConflict):
            launcher.ensure_workbench(
                self.registry,
                "domain-a",
                "vocabulary",
                43125,
                probe=probe,
                starter=starter,
            )
        starter.assert_not_called()

    def test_initial_other_registry_conflicts_without_starting(self) -> None:
        starter = mock.Mock(side_effect=AssertionError("starter must not run"))
        probe = ScriptedProbe(
            launcher.ProbeResult(
                launcher.ProbeKind.OTHER_REGISTRY,
                identity=identity(self.root / "other.json", "other-instance"),
                detail=str(self.root / "other.json"),
            )
        )

        with self.assertRaisesRegex(launcher.WorkbenchConflict, "another registry"):
            launcher.ensure_workbench(
                self.registry,
                "domain-a",
                "vocabulary",
                43126,
                probe=probe,
                starter=starter,
            )
        starter.assert_not_called()

    def test_stale_runtime_conflicts_without_starting(self) -> None:
        starter = mock.Mock(side_effect=AssertionError("starter must not run"))
        probe = ScriptedProbe(
            launcher.ProbeResult(
                launcher.ProbeKind.STALE_RUNTIME,
                identity=identity(self.registry, "old-instance"),
                detail="loaded=['domain-a'], expected=['domain-a', 'domain-b']",
            )
        )

        with self.assertRaisesRegex(launcher.WorkbenchConflict, "domain set is stale"):
            launcher.ensure_workbench(
                self.registry,
                "domain-b",
                "vocabulary",
                43127,
                expected_domain_ids=("domain-a", "domain-b"),
                probe=probe,
                starter=starter,
            )
        starter.assert_not_called()

    def test_concurrent_winner_is_reused_and_own_child_is_reaped(self) -> None:
        attempt, process = self.attempt()
        probe = ScriptedProbe(
            launcher.ProbeResult(launcher.ProbeKind.ABSENT),
            match(self.registry, "concurrent-winner"),
        )

        result = self.run_ensure(probe, attempt)

        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["instance_id"], "concurrent-winner")
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)

    def test_other_registry_winner_stops_own_child_before_conflict(self) -> None:
        attempt, process = self.attempt()
        probe = ScriptedProbe(
            launcher.ProbeResult(launcher.ProbeKind.ABSENT),
            launcher.ProbeResult(
                launcher.ProbeKind.OTHER_REGISTRY,
                identity=identity(self.root / "other.json", "other-instance"),
                detail=str(self.root / "other.json"),
            ),
        )

        with self.assertRaises(launcher.WorkbenchConflict):
            self.run_ensure(probe, attempt)
        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)

    def test_child_exit_keeps_original_log_error_visible(self) -> None:
        sentinel = "sentinel-startup-error: database is read only"
        self.log_path.write_text(sentinel + "\n", encoding="utf-8")
        attempt, process = self.attempt(return_code=23)
        probe = ScriptedProbe(launcher.ProbeResult(launcher.ProbeKind.ABSENT))
        clock = FakeClock()

        with self.assertRaises(launcher.WorkbenchStartupError) as caught:
            self.run_ensure(probe, attempt, clock)

        message = str(caught.exception)
        self.assertIn("exit code 23", message)
        self.assertIn(sentinel, message)
        self.assertEqual(process.terminate_calls, 0)

    def test_child_exit_with_unknown_port_owner_is_a_conflict(self) -> None:
        sentinel = "bind failed because another server won"
        self.log_path.write_text(sentinel + "\n", encoding="utf-8")
        attempt, _ = self.attempt(return_code=98)
        probe = ScriptedProbe(
            launcher.ProbeResult(launcher.ProbeKind.ABSENT),
            launcher.ProbeResult(
                launcher.ProbeKind.OCCUPIED_UNKNOWN,
                detail="identity endpoint returned HTTP 404",
            ),
        )

        with self.assertRaises(launcher.WorkbenchConflict) as caught:
            self.run_ensure(probe, attempt)

        self.assertIn("cannot be identified", str(caught.exception))
        self.assertIn(sentinel, str(caught.exception))

    def test_post_spawn_probe_access_failure_cleans_own_child(self) -> None:
        attempt, process = self.attempt()
        probe = mock.Mock(
            side_effect=[
                launcher.ProbeResult(launcher.ProbeKind.ABSENT),
                launcher.WorkbenchAccessError("loopback access denied"),
            ]
        )

        with self.assertRaisesRegex(
            launcher.WorkbenchAccessError,
            "loopback access denied",
        ):
            launcher.ensure_workbench(
                self.registry,
                "domain-a",
                "vocabulary",
                43129,
                probe=probe,
                starter=lambda *_: attempt,
            )

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)

    def test_exited_concurrent_loser_can_still_reuse_winner(self) -> None:
        attempt, process = self.attempt(return_code=98)
        absent = launcher.ProbeResult(launcher.ProbeKind.ABSENT)
        probe = ScriptedProbe(absent, absent, match(self.registry, "winner-instance"))

        result = self.run_ensure(probe, attempt)

        self.assertEqual(result["status"], "reused")
        self.assertEqual(result["instance_id"], "winner-instance")
        self.assertEqual(process.terminate_calls, 0)

    def test_timeout_stops_and_reaps_own_child(self) -> None:
        attempt, process = self.attempt()
        probe = ScriptedProbe(launcher.ProbeResult(launcher.ProbeKind.ABSENT))
        clock = FakeClock()

        with self.assertRaisesRegex(launcher.WorkbenchStartupError, "did not become ready"):
            self.run_ensure(probe, attempt, clock, timeout=0.25)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.wait_calls, 1)

    def test_cleanup_never_hides_an_unstoppable_live_child(self) -> None:
        process = UnstoppableFakeProcess()
        attempt = launcher.LaunchAttempt(
            process=process,  # type: ignore[arg-type]
            instance_id="unstoppable-instance",
            log_path=self.log_path,
        )

        with self.assertRaisesRegex(
            launcher.WorkbenchCleanupError,
            "PID: 4242",
        ):
            launcher.stop_launch_attempt(attempt)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 2)

    def test_cleanup_reports_permission_denial_while_child_is_live(self) -> None:
        process = PermissionDeniedFakeProcess()
        attempt = launcher.LaunchAttempt(
            process=process,  # type: ignore[arg-type]
            instance_id="denied-instance",
            log_path=self.log_path,
        )

        with self.assertRaisesRegex(
            launcher.WorkbenchCleanupError,
            "terminate denied.*kill denied",
        ):
            launcher.stop_launch_attempt(attempt)

        self.assertEqual(process.terminate_calls, 1)
        self.assertEqual(process.kill_calls, 1)
        self.assertEqual(process.wait_calls, 2)


@contextmanager
def http_fixture(
    payload: object,
    *,
    status: int = 200,
    delay: float = 0.0,
):
    body = (
        payload
        if isinstance(payload, bytes)
        else json.dumps(payload).encode("utf-8")
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *values: object) -> None:
            return

        def do_GET(self) -> None:
            if delay:
                time.sleep(delay)
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = int(server.server_address[1])
    if port == launcher.PORT:
        server.server_close()
        with http_fixture(payload, status=status, delay=delay) as replacement:
            yield replacement
        return
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)


class ProbeContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.registry = self.root / "real" / "registry.json"
        self.registry.parent.mkdir()
        self.registry.write_text("{}\n", encoding="utf-8")

    def test_connection_refused_is_the_only_absent_state(self) -> None:
        result = launcher.probe_workbench(unused_port(), self.registry, timeout=0.1)
        self.assertIs(result.kind, launcher.ProbeKind.ABSENT)

    def test_matching_identity_uses_canonical_registry_and_ignores_pid(self) -> None:
        unresolved = self.registry.parent / ".." / "real" / "registry.json"
        payload = identity(self.registry, "instance-a")
        payload["pid"] = 999999
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(port, unresolved, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.MATCH)
        self.assertEqual(result.identity["instance_id"], "instance-a")  # type: ignore[index]

    def test_valid_researchramp_for_other_registry_is_distinct_conflict(self) -> None:
        other = self.root / "other" / "registry.json"
        with http_fixture(identity(other, "instance-b")) as port:
            result = launcher.probe_workbench(port, self.registry, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.OTHER_REGISTRY)

    def test_identity_version_mismatch_is_incompatible(self) -> None:
        payload = identity(self.registry, "instance-a")
        payload["identity_version"] = launcher.WORKBENCH_IDENTITY_VERSION + 1
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(port, self.registry, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.INCOMPATIBLE)

    def test_404_non_json_and_timeout_are_never_absent(self) -> None:
        cases = [
            ({"error": "missing"}, 404, 0.0),
            (b"not-json", 200, 0.0),
            ({"service": "slow"}, 200, 0.15),
        ]
        for payload, status, delay in cases:
            with self.subTest(status=status, payload=payload, delay=delay):
                with http_fixture(payload, status=status, delay=delay) as port:
                    result = launcher.probe_workbench(port, self.registry, timeout=0.03)
                self.assertIs(result.kind, launcher.ProbeKind.OCCUPIED_UNKNOWN)

    def test_missing_instance_id_is_incompatible(self) -> None:
        payload = identity(self.registry, "instance-a")
        del payload["instance_id"]
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(port, self.registry, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.INCOMPATIBLE)

    def test_loaded_domain_set_must_match_completed_registry_set(self) -> None:
        payload = identity(self.registry, "instance-a", ("domain-a",))
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(
                port,
                self.registry,
                expected_domain_ids=("domain-a", "domain-b"),
                timeout=0.2,
            )

        self.assertIs(result.kind, launcher.ProbeKind.STALE_RUNTIME)
        self.assertIn("domain-b", result.detail)

    def test_probe_permission_failure_is_not_reported_as_port_conflict(self) -> None:
        connection = mock.Mock()
        connection.request.side_effect = PermissionError("operation not permitted")
        with mock.patch.object(
            launcher.http.client,
            "HTTPConnection",
            return_value=connection,
        ):
            with self.assertRaisesRegex(
                launcher.WorkbenchAccessError,
                "No service was accepted or left running",
            ):
                launcher.probe_workbench(43128, self.registry, timeout=0.2)

        connection.close.assert_called_once_with()

    def test_identity_version_requires_an_integer(self) -> None:
        for invalid_version in (True, 1.0):
            with self.subTest(invalid_version=invalid_version):
                payload = identity(self.registry, "instance-a")
                payload["identity_version"] = invalid_version
                with http_fixture(payload) as port:
                    result = launcher.probe_workbench(
                        port,
                        self.registry,
                        timeout=0.2,
                    )

                self.assertIs(result.kind, launcher.ProbeKind.INCOMPATIBLE)

    def test_domain_ids_are_a_nonempty_sorted_unique_string_list(self) -> None:
        invalid_values = (
            None,
            [],
            "domain-a",
            [1],
            ["domain-b", "domain-a"],
            ["domain-a", "domain-a"],
        )
        for invalid in invalid_values:
            with self.subTest(domain_ids=invalid):
                payload = identity(self.registry, "instance-a")
                payload["domain_ids"] = invalid
                with http_fixture(payload) as port:
                    result = launcher.probe_workbench(
                        port,
                        self.registry,
                        timeout=0.2,
                    )
                self.assertIs(result.kind, launcher.ProbeKind.INCOMPATIBLE)

    def test_valid_multi_domain_identity_matches_normalized_expectation(self) -> None:
        payload = identity(
            self.registry,
            "instance-a",
            ("domain-beta", "domain-alpha"),
        )
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(
                port,
                self.registry,
                expected_domain_ids=(
                    "domain-beta",
                    "domain-alpha",
                    "domain-alpha",
                ),
                timeout=0.2,
            )

        self.assertIs(result.kind, launcher.ProbeKind.MATCH)

    def test_standalone_researchramp_is_not_a_registry_match(self) -> None:
        payload = identity(self.registry, "instance-a")
        payload["registry"] = None
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(port, self.registry, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.OTHER_REGISTRY)

    def test_registry_identity_must_be_absolute(self) -> None:
        payload = identity(self.registry, "instance-a")
        payload["registry"] = "relative/registry.json"
        with http_fixture(payload) as port:
            result = launcher.probe_workbench(port, self.registry, timeout=0.2)

        self.assertIs(result.kind, launcher.ProbeKind.INCOMPATIBLE)


class RealLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.registry = self.root / "registry.json"
        self.registry.write_text("{}\n", encoding="utf-8")
        self.processes: list[subprocess.Popen[bytes]] = []
        self.process_lock = threading.Lock()
        self.addCleanup(self.stop_processes)

    def stop_processes(self) -> None:
        with self.process_lock:
            processes = list(self.processes)
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2.0)

    def starter(
        self,
        *,
        delay: float = 0.0,
        fail_message: str | None = None,
        exit_code: int = 23,
    ):
        def start(
            registry: Path,
            domain: str,
            view: str,
            port: int,
        ) -> launcher.LaunchAttempt:
            del domain, view
            instance_id = uuid.uuid4().hex
            log_path = self.root / f"{instance_id}.log"
            command = [
                sys.executable,
                str(FIXTURE_SERVER),
                "--port",
                str(port),
                "--registry",
                str(registry),
                "--instance-id",
                instance_id,
                "--delay",
                str(delay),
                "--exit-code",
                str(exit_code),
            ]
            if fail_message is not None:
                command.extend(["--fail-message", fail_message])
            with log_path.open("xb") as log:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            with self.process_lock:
                self.processes.append(process)
            return launcher.LaunchAttempt(process, instance_id, log_path)

        return start

    @staticmethod
    def probe(port: int, registry: Path) -> launcher.ProbeResult:
        return launcher.probe_workbench(port, registry, timeout=0.05)

    def ensure(self, port: int, starter, *, timeout: float = 3.0):
        return launcher.ensure_workbench(
            self.registry,
            "domain-a",
            "vocabulary",
            port,
            probe=self.probe,
            starter=starter,
            startup_timeout=timeout,
        )

    def test_real_delayed_child_becomes_ready_without_false_conflict(self) -> None:
        port = unused_port()
        with mock.patch.object(launcher, "POLL_INTERVAL_SECONDS", 0.02):
            result = self.ensure(port, self.starter(delay=0.2))

        self.assertEqual(result["status"], "started")
        self.assertNotEqual(port, launcher.PORT)
        live = [process for process in self.processes if process.poll() is None]
        self.assertEqual(len(live), 1)

    def test_second_real_call_reuses_running_child(self) -> None:
        port = unused_port()
        starter = self.starter()
        with mock.patch.object(launcher, "POLL_INTERVAL_SECONDS", 0.02):
            first = self.ensure(port, starter)
        process_count = len(self.processes)
        forbidden_starter = mock.Mock(side_effect=AssertionError("must reuse"))

        second = self.ensure(port, forbidden_starter)

        self.assertEqual(first["status"], "started")
        self.assertEqual(second["status"], "reused")
        self.assertEqual(first["instance_id"], second["instance_id"])
        self.assertEqual(len(self.processes), process_count)
        forbidden_starter.assert_not_called()

    def test_real_foreign_service_blocks_spawn(self) -> None:
        forbidden_starter = mock.Mock(side_effect=AssertionError("must not spawn"))
        with http_fixture({"service": "something-else"}) as port:
            with self.assertRaises(launcher.WorkbenchConflict):
                self.ensure(port, forbidden_starter)
        forbidden_starter.assert_not_called()

    def test_default_probe_rejects_a_stale_loaded_domain_set(self) -> None:
        forbidden_starter = mock.Mock(side_effect=AssertionError("must not spawn"))
        with http_fixture(identity(self.registry, "old", ("domain-a",))) as port:
            with self.assertRaisesRegex(
                launcher.WorkbenchConflict,
                "domain set is stale",
            ):
                launcher.ensure_workbench(
                    self.registry,
                    "domain-b",
                    "vocabulary",
                    port,
                    expected_domain_ids=("domain-a", "domain-b"),
                    starter=forbidden_starter,
                )
        forbidden_starter.assert_not_called()

    def test_real_other_registry_blocks_spawn(self) -> None:
        forbidden_starter = mock.Mock(side_effect=AssertionError("must not spawn"))
        other_registry = self.root / "other-registry.json"
        with http_fixture(identity(other_registry, "other-instance")) as port:
            with self.assertRaisesRegex(launcher.WorkbenchConflict, "another registry"):
                self.ensure(port, forbidden_starter)
        forbidden_starter.assert_not_called()

    def test_real_child_failure_surfaces_original_log(self) -> None:
        port = unused_port()
        sentinel = "sentinel-startup-error-from-child"
        with (
            mock.patch.object(launcher, "POLL_INTERVAL_SECONDS", 0.01),
            mock.patch.object(launcher, "EXIT_CONVERGENCE_SECONDS", 0.03),
        ):
            with self.assertRaises(launcher.WorkbenchStartupError) as caught:
                self.ensure(
                    port,
                    self.starter(fail_message=sentinel, exit_code=23),
                    timeout=1.0,
                )

        self.assertIn("exit code 23", str(caught.exception))
        self.assertIn(sentinel, str(caught.exception))

    def test_two_concurrent_launchers_converge_on_one_instance(self) -> None:
        port = unused_port()
        base_start = self.starter(delay=0.25)
        start_barrier = threading.Barrier(2)

        def synchronized_start(
            registry: Path,
            domain: str,
            view: str,
            candidate_port: int,
        ) -> launcher.LaunchAttempt:
            start_barrier.wait(timeout=2.0)
            return base_start(registry, domain, view, candidate_port)

        def open_concurrently() -> dict[str, object]:
            return self.ensure(port, synchronized_start, timeout=3.0)

        with (
            mock.patch.object(launcher, "POLL_INTERVAL_SECONDS", 0.02),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(open_concurrently) for _ in range(2)]
            results = [future.result(timeout=5.0) for future in futures]

        self.assertEqual(sorted(result["status"] for result in results), ["reused", "started"])
        self.assertEqual(len({result["instance_id"] for result in results}), 1)
        self.assertEqual(len(self.processes), 2)
        live = [process for process in self.processes if process.poll() is None]
        self.assertEqual(len(live), 1)
        self.assertEqual(
            len([process for process in self.processes if process.poll() is not None]),
            1,
        )

        forbidden_starter = mock.Mock(side_effect=AssertionError("must reuse winner"))
        third = self.ensure(port, forbidden_starter)
        self.assertEqual(third["status"], "reused")
        self.assertEqual(third["instance_id"], results[0]["instance_id"])
        forbidden_starter.assert_not_called()


class ProductionHandshakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        alpha = create_minimal_completed_workspace(self.root, "alpha")
        beta = create_minimal_completed_workspace(self.root, "beta")
        self.registry_path = self.root / "library" / "domains.json"
        registry = launcher.DomainRegistry(self.registry_path)
        registry.register(alpha, domain_id="alpha", make_active=False)
        registry.register(beta, domain_id="beta", make_active=True)
        self.attempts: list[launcher.LaunchAttempt] = []
        self.addCleanup(self.stop_attempts)

    def stop_attempts(self) -> None:
        for attempt in self.attempts:
            try:
                launcher.stop_launch_attempt(attempt)
            finally:
                attempt.log_path.unlink(missing_ok=True)

    def recording_starter(
        self,
        registry: Path,
        domain: str,
        view: str,
        port: int,
    ) -> launcher.LaunchAttempt:
        attempt = launcher.start_workbench(registry, domain, view, port)
        self.attempts.append(attempt)
        return attempt

    def test_production_launcher_and_server_complete_the_real_handshake(self) -> None:
        port = unused_port()
        with mock.patch.object(launcher, "POLL_INTERVAL_SECONDS", 0.02):
            result = launcher.ensure_workbench(
                self.registry_path,
                "alpha",
                "vocabulary",
                port,
                expected_domain_ids=("alpha", "beta"),
                starter=self.recording_starter,
                startup_timeout=8.0,
            )

        self.assertEqual(result["status"], "started")
        self.assertEqual(len(self.attempts), 1)
        attempt = self.attempts[0]
        command = list(attempt.process.args)
        self.assertEqual(command[command.index("--instance-id") + 1], attempt.instance_id)
        self.assertEqual(
            Path(command[command.index("--library") + 1]),
            self.registry_path.resolve(),
        )
        probe = launcher.probe_workbench(
            port,
            self.registry_path,
            expected_domain_ids=("alpha", "beta"),
            timeout=0.2,
        )
        self.assertIs(probe.kind, launcher.ProbeKind.MATCH)
        self.assertEqual(probe.identity["domain_ids"], ["alpha", "beta"])  # type: ignore[index]

    def test_main_passes_the_full_completed_domain_set_to_ensure(self) -> None:
        args = SimpleNamespace(
            domain=None,
            view="briefs",
            registry=self.registry_path,
            port=43130,
        )
        expected_result = {
            "status": "reused",
            "instance_id": "existing",
            "domain_id": "beta",
            "url": "http://127.0.0.1:43130/?domain=beta#briefs",
        }
        with (
            mock.patch.object(launcher, "parse_args", return_value=args),
            mock.patch.object(launcher, "enforce_business_license"),
            mock.patch.object(
                launcher,
                "ensure_workbench",
                return_value=expected_result,
            ) as ensure,
            mock.patch("builtins.print"),
        ):
            launcher.main()

        ensure.assert_called_once_with(
            self.registry_path.resolve(),
            "beta",
            "briefs",
            43130,
            expected_domain_ids=("alpha", "beta"),
        )


if __name__ == "__main__":
    unittest.main()
