#!/usr/bin/env python3
"""Drive ResearchRamp from a confirmed profile to a verified calibration service.

This is the sole lifecycle owner for the unattended part of first-time setup.
Discovery, review preparation, download, analysis, and finalization are internal
checkpoints.  None of them is a successful terminal result.  The operation may
hand work back to the host agent for contextual review, but it becomes terminal
only after the live library service proves that both vocabulary and terminology
are available for the selected registered domain.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows
    fcntl = None  # type: ignore[assignment]

from acquire_mini_corpus import minimum_usable_papers
from domain_registry import (
    DomainRegistry,
    default_registry_path,
    validate_initialized_workspace,
)
from vocabulary_cards import GLOSS_DATA_NAME, prepare_review_input
from open_workbench import (
    HOST,
    ensure_workbench,
    launchable_registry_domain_ids,
    probe_workbench,
    start_workbench,
)
from orthography_contract import orthography_summary_is_complete
from research_profile import validate_profile
from researchramp_license import enforce_business_license
from researchramp_core import read_json, utc_now, write_json
from terminology_assets import load_finalized_terminology


SCHEMA_VERSION = 1
APP_API_VERSION = 6
STATUS_NAME = "status.json"
LOCK_NAME = ".initialization.lock"


class InitializationError(RuntimeError):
    """A persistent or external condition prevented safe continuation."""


class ResumeScopeError(InitializationError):
    """The invocation does not belong to the persisted operation scope."""


def _read_json_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _json_get(port: int, path: str, *, domain_id: str | None = None) -> dict[str, Any]:
    connection = http.client.HTTPConnection(HOST, port, timeout=2.0)
    headers = {"Accept": "application/json", "Connection": "close"}
    if domain_id is not None:
        headers["X-ResearchRamp-Domain"] = domain_id
        headers["X-ResearchRamp-API-Version"] = str(APP_API_VERSION)
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        body = response.read(2_000_000)
    finally:
        connection.close()
    if response.status != 200:
        raise InitializationError(
            f"Workbench readiness probe failed: GET {path} returned {response.status}"
        )
    try:
        value = json.loads(body)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise InitializationError(
            f"Workbench readiness probe returned invalid JSON: {path}"
        ) from error
    if not isinstance(value, dict):
        raise InitializationError(
            f"Workbench readiness probe returned a non-object: {path}"
        )
    return value


class InitializationController:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.workspace = args.workspace.expanduser().resolve()
        self.profile_path = args.profile.expanduser().resolve()
        self.registry_path = args.registry.expanduser().resolve()
        self.status_path = self.workspace / STATUS_NAME
        self.operation_id = uuid.uuid4().hex
        self.revision = 0
        self.previous_status: dict[str, Any] | None = None
        self.profile_id = ""
        if self.profile_path.is_file():
            candidate_profile = _read_json_object(self.profile_path)
            self.profile_id = str(candidate_profile.get("profile_id") or "")
        if self.status_path.is_file():
            previous = _read_json_object(self.status_path)
            if (
                previous.get("schema_version") == SCHEMA_VERSION
                and previous.get("operation") == "prepare_calibration"
            ):
                self.previous_status = previous
                self.operation_id = str(previous.get("operation_id") or self.operation_id)
                raw_revision = previous.get("revision", 0)
                if isinstance(raw_revision, int) and not isinstance(raw_revision, bool):
                    self.revision = raw_revision

    def _status(
        self,
        status: str,
        *,
        terminal: bool,
        checkpoint: str,
        next_action: dict[str, Any] | None = None,
        service: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        self.revision += 1
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation": "prepare_calibration",
            "operation_id": self.operation_id,
            "revision": self.revision,
            "updated_at": utc_now(),
            "profile": str(self.profile_path),
            "profile_id": self.profile_id,
            "workspace": str(self.workspace),
            "registry": str(self.registry_path),
            "target_papers": self.args.target_papers,
            "minimum_usable_papers": minimum_usable_papers(self.args.target_papers),
            "status": status,
            "terminal": terminal,
            "checkpoint": checkpoint,
            "next_action": next_action,
        }
        if service is not None:
            payload["service"] = service
        if error is not None:
            payload["error"] = error
        write_json(self.status_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return payload

    def _host_action(
        self,
        action_type: str,
        *,
        input_path: Path | None = None,
        input_paths: dict[str, Path] | None = None,
        output_path: Path,
        checkpoint: str,
        instructions: str,
    ) -> dict[str, Any]:
        next_action: dict[str, Any] = {
            "actor": "current_host_agent",
            "type": action_type,
            "output": str(output_path),
            "instructions": instructions,
            "resume": self.resume_command(),
        }
        if input_path is not None:
            next_action["input"] = str(input_path)
        if input_paths is not None:
            next_action["inputs"] = {
                name: str(path) for name, path in input_paths.items()
            }
        return self._status(
            "host_action_required",
            terminal=False,
            checkpoint=checkpoint,
            next_action=next_action,
        )

    def resume_command(self) -> list[str]:
        return [
            str(Path(sys.executable).absolute()),
            str(Path(__file__).resolve()),
            "run",
            "--profile",
            str(self.profile_path),
            "--workspace",
            str(self.workspace),
            "--registry",
            str(self.registry_path),
            "--target-papers",
            str(self.args.target_papers),
            "--download-workers",
            str(self.args.download_workers),
            "--download-workers-per-host",
            str(self.args.download_workers_per_host),
            "--port",
            str(self.args.port),
        ]

    def _run_helper(
        self,
        command: list[str],
        checkpoint: str,
        *,
        accepted_codes: tuple[int, ...] = (0,),
    ) -> int:
        self._status("running", terminal=False, checkpoint=checkpoint)
        result = subprocess.run(command, cwd=Path(__file__).resolve().parents[1])
        if result.returncode not in accepted_codes:
            raise InitializationError(
                f"Internal checkpoint {checkpoint} failed with exit code "
                f"{result.returncode}; inspect the preserved workspace and resume."
            )
        return result.returncode

    def _acquire_command(self, *extra: str) -> list[str]:
        return [
            sys.executable,
            str(Path(__file__).with_name("acquire_mini_corpus.py")),
            "--profile",
            str(self.profile_path),
            "--workspace",
            str(self.workspace),
            "--registry",
            str(self.registry_path),
            "--target-papers",
            str(self.args.target_papers),
            "--download-workers",
            str(self.args.download_workers),
            "--download-workers-per-host",
            str(self.args.download_workers_per_host),
            *extra,
        ]

    def _prepare_assets(self) -> None:
        candidates = self.workspace / "candidates.jsonl"
        candidate_packet = self.workspace / "candidate-review-packet.jsonl"
        candidate_selection = self.workspace / "candidate-review-selection.json"
        if not candidates.is_file() or not candidate_packet.is_file():
            self._run_helper(
                self._acquire_command("--search-only"),
                "discovering_and_preparing_candidate_review",
            )
        if not any(candidate_packet.read_text(encoding="utf-8").splitlines()):
            raise InitializationError(
                "Discovery produced no candidates for host review; inspect provider "
                "outcomes before resuming the same operation."
            )
        if not candidate_selection.is_file():
            self._host_action(
                "review_candidates",
                input_path=candidate_packet,
                output_path=candidate_selection,
                checkpoint="candidate_review_needed",
                instructions=(
                    "Select an ordered, relevant set with enough backups for the target. "
                    "Write schema_version=1, reviewer=current-host-agent, "
                    "selected_candidate_ids, and review_summary; then immediately resume."
                ),
            )
            raise StopIteration

        orthography_input = self.workspace / "analysis" / "orthography-review-input.json"
        if not orthography_input.is_file():
            self._run_helper(
                self._acquire_command(
                    "--selection",
                    str(candidate_selection),
                    "--analyze",
                ),
                "downloading_and_analyzing_corpus",
                accepted_codes=(0, 2),
            )

        summary_path = self.workspace / "cold-start-summary.json"
        if not summary_path.is_file():
            raise InitializationError("Acquisition did not write cold-start-summary.json")
        acquisition = _read_json_object(summary_path)
        if acquisition.get("corpus_is_usable") is not True:
            self._host_action(
                "expand_candidate_selection",
                input_path=candidate_packet,
                output_path=candidate_selection,
                checkpoint="more_usable_papers_needed",
                instructions=(
                    "Revise the same selection with additional relevant backup candidates, "
                    "then immediately resume."
                ),
            )
            raise StopIteration

        terminology_input = self.workspace / "analysis" / "terminology-review-input.json"
        if not terminology_input.is_file():
            raise InitializationError("Analysis did not write terminology review input")

        orthography_selection = (
            self.workspace / "analysis" / "orthography-review-selection.json"
        )
        orthography_summary = (
            self.workspace / "analysis" / "orthography-review-summary.json"
        )
        orthography_is_complete = (
            orthography_summary.is_file()
            and orthography_summary_is_complete(_read_json_object(orthography_summary))
        )
        if not orthography_is_complete:
            if not orthography_selection.is_file():
                self._host_action(
                    "review_vocabulary_orthography",
                    input_path=orthography_input,
                    output_path=orthography_selection,
                    checkpoint="orthography_review_needed",
                    instructions=(
                        "Review every suspicious vocabulary lemma. Write one "
                        "schema_version=1 review with reviewer=current-host-agent, "
                        "lemma_keeps, lemma_replacements, lemma_drops, and "
                        "review_summary. Every candidate must appear in exactly one "
                        "of lemma_keeps, lemma_replacements, or lemma_drops; then "
                        "immediately resume."
                    ),
                )
                raise StopIteration
            self._run_helper(
                [
                    sys.executable,
                    str(Path(__file__).with_name("apply_orthography_review.py")),
                    "--workspace",
                    str(self.workspace),
                    "--selection",
                    str(orthography_selection),
                ],
                "finalizing_vocabulary_orthography",
            )

        card_review_input = self.workspace / "analysis" / "vocabulary-card-review-input.json"
        combined_selection = self.workspace / "analysis" / "domain-review-selection.json"
        assets_summary = self.workspace / "analysis" / "domain-assets-summary.json"
        if not assets_summary.is_file():
            prepare_review_input(
                self.workspace,
                Path(__file__).resolve().parents[1] / "app" / "data" / GLOSS_DATA_NAME,
            )

        if not assets_summary.is_file():
            if not combined_selection.is_file():
                self._host_action(
                    "review_vocabulary_cards_and_terminology",
                    input_paths={
                        "terminology": terminology_input,
                        "vocabulary_cards": card_review_input,
                    },
                    output_path=combined_selection,
                    checkpoint="learning_asset_review_needed",
                    instructions=(
                        "Review the vocabulary-card gloss candidates and terminology "
                        "candidates together. Write one schema_version=1 review with "
                        "reviewer=current-host-agent, terminology, "
                        "terminology_explanations, vocabulary_card_glosses, "
                        "and review_summary. vocabulary_card_glosses must provide the "
                        "contextual Chinese meaning and a stable semantic sense_key for every "
                        "vocabulary_cards candidate, keyed by its already finalized canonical "
                        "lemma; English is optional. Treat dictionary meanings as suggestions "
                        "and use corpus acronym expansions and representative sentences as "
                        "the authority. Then immediately resume."
                    ),
                )
                raise StopIteration
            self._run_helper(
                [
                    sys.executable,
                    str(Path(__file__).with_name("finalize_domain_assets.py")),
                    "--workspace",
                    str(self.workspace),
                    "--selection",
                    str(combined_selection),
                ],
                "finalizing_vocabulary_and_terminology",
            )
        summary = _read_json_object(assets_summary)
        if summary.get("ready_for_calibration") is not True:
            raise InitializationError(
                "Vocabulary and terminology finalization did not complete"
            )

    def _launch_and_verify(self, profile: dict[str, Any]) -> dict[str, Any]:
        registration = DomainRegistry(self.registry_path).register(self.workspace)
        domain_id = registration.domain_id
        registry = DomainRegistry(self.registry_path)
        domain_ids = launchable_registry_domain_ids(registry, domain_id)
        starter = lambda path, selected, view, port: start_workbench(
            path,
            selected,
            view,
            port,
            ready_calibration_domain=domain_id,
        )
        launch = ensure_workbench(
            self.registry_path,
            domain_id,
            "vocabulary",
            self.args.port,
            expected_domain_ids=domain_ids,
            starter=starter,
        )
        selected_port = int(launch["port"])
        identity_probe = probe_workbench(
            selected_port,
            self.registry_path,
            expected_domain_ids=domain_ids,
            timeout=2.0,
        )
        if identity_probe.kind.value != "match" or identity_probe.identity is None:
            raise InitializationError("The launched workbench failed its identity probe")

        query = urlencode({"domain_id": domain_id})
        app_state = _json_get(
            selected_port,
            f"/api/app-state?{query}",
            domain_id=domain_id,
        )
        terms_api = _json_get(
            selected_port,
            f"/api/terms?{query}",
            domain_id=domain_id,
        )
        terms, _explanations, summary = load_finalized_terminology(
            self.workspace,
            require_review_summary=True,
        )
        calibration = app_state.get("calibration")
        embedded_terms = app_state.get("terminology")
        expected_term_names = {
            str(item.get("term") or "").strip().casefold() for item in terms
        }
        embedded_term_names = {
            str(item.get("term") or "").strip().casefold()
            for item in (embedded_terms or {}).get("terms", [])
            if isinstance(item, dict)
        }
        api_term_names = {
            str(item.get("term") or "").strip().casefold()
            for item in terms_api.get("terms", [])
            if isinstance(item, dict)
        }
        if (
            app_state.get("domain_id") != domain_id
            or not isinstance(calibration, dict)
            or calibration.get("question_limit") != 30
            or (
                calibration.get("complete") is not True
                and not isinstance(calibration.get("word"), dict)
            )
            or not isinstance(embedded_terms, dict)
            or embedded_terms.get("count") != len(terms)
            or terms_api.get("count") != len(terms)
            or embedded_term_names != expected_term_names
            or api_term_names != expected_term_names
            or summary is None
            or summary.get("selected_terminology_count") != len(terms)
        ):
            raise InitializationError(
                "The live workbench did not expose the finalized vocabulary and "
                "terminology snapshot for the selected domain"
            )
        vocabulary_path = self.workspace / "analysis" / "vocabulary-map.tsv"
        import csv

        with vocabulary_path.open(encoding="utf-8", newline="") as handle:
            vocabulary_rows = list(csv.DictReader(handle, delimiter="\t"))
        vocabulary_lemmas = [
            str(item.get("lemma") or "").strip().casefold()
            for item in vocabulary_rows
        ]
        vocabulary_entry_count = len(vocabulary_lemmas)
        if (
            vocabulary_entry_count < 30
            or len(set(vocabulary_lemmas)) != vocabulary_entry_count
            or not all(vocabulary_lemmas)
        ):
            raise InitializationError(
                "The finalized vocabulary map must contain at least 30 unique lemmas"
            )
        return {
            **launch,
            "registry": str(self.registry_path),
            "profile_id": profile["profile_id"],
            "domain_ids": list(domain_ids),
            "vocabulary_ready": True,
            "vocabulary_entry_count": vocabulary_entry_count,
            "terminology_ready": True,
            "terminology_count": len(terms),
            "calibration_answered": calibration.get("answered"),
            "verified_at": utc_now(),
        }

    def run(self) -> dict[str, Any]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        lock_path = self.workspace / LOCK_NAME
        with lock_path.open("a+") as lock:
            try:
                if fcntl is not None:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:  # pragma: no cover - exercised on Windows
                    import msvcrt

                    lock.seek(0)
                    if not lock.read(1):
                        lock.write("0")
                        lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as error:
                raise InitializationError(
                    f"Another initialization controller is active for {self.workspace}"
                ) from error
            profile = _read_json_object(self.profile_path)
            validate_profile(profile)
            self.profile_id = str(profile["profile_id"])
            if self.previous_status is not None:
                expected_scope = {
                    "profile": str(self.profile_path),
                    "workspace": str(self.workspace),
                    "registry": str(self.registry_path),
                    "target_papers": self.args.target_papers,
                }
                mismatched = [
                    key
                    for key, expected in expected_scope.items()
                    if self.previous_status.get(key) != expected
                ]
                if mismatched:
                    raise ResumeScopeError(
                        "Existing initialization status belongs to a different "
                        f"operation scope: {', '.join(mismatched)}"
                    )
            existing_profile = self.workspace / "research-profile.json"
            if existing_profile.is_file() and _read_json_object(
                existing_profile
            ) != profile:
                raise ResumeScopeError(
                    "Workspace is already bound to a different confirmed profile"
                )
            self._status("running", terminal=False, checkpoint="preparing_calibration")
            try:
                self._prepare_assets()
            except StopIteration:
                return _read_json_object(self.status_path)
            validate_initialized_workspace(self.workspace)
            service = self._launch_and_verify(profile)
            return self._status(
                "awaiting_user_calibration",
                terminal=True,
                checkpoint="calibration_service_ready",
                next_action={
                    "actor": "user",
                    "type": "answer_vocabulary_calibration",
                    "url": service["url"],
                    "question_count": 30,
                },
                service=service,
            )

    def inspect(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            raise InitializationError(
                f"No initialization status exists in {self.workspace}"
            )
        payload = _read_json_object(self.status_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status"), nargs="?", default="run")
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    parser.add_argument("--target-papers", type=int, default=70)
    parser.add_argument("--download-workers", type=int, default=4)
    parser.add_argument("--download-workers-per-host", type=int, default=2)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.target_papers <= 100:
        parser.error("--target-papers must be between 1 and 100")
    if args.download_workers < 1:
        parser.error("--download-workers must be positive")
    if not 1 <= args.download_workers_per_host <= args.download_workers:
        parser.error("--download-workers-per-host must be between 1 and --download-workers")
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    return args


def main() -> int:
    args = parse_args()
    enforce_business_license("initialization")
    controller = InitializationController(args)
    try:
        if args.command == "status":
            controller.inspect()
        else:
            controller.run()
        return 0
    except ResumeScopeError as error:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "terminal": False,
                    "error": str(error),
                    "authoritative_status": str(controller.status_path),
                },
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except (InitializationError, FileNotFoundError, ValueError) as error:
        controller._status(
            "failed",
            terminal=False,
            checkpoint="blocked",
            error=str(error),
            next_action={
                "actor": "current_host_agent",
                "type": "diagnose_and_resume",
                "resume": controller.resume_command(),
            },
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
