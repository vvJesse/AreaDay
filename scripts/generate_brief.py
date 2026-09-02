#!/usr/bin/env python3
"""Run one ResearchRamp brief generation operation to a terminal result.

Discovery, source preparation, host-agent writing, validation, and import are
one operation.  Host-agent work is exposed through ``next_action`` and must be
completed before immediately resuming this controller.  A prepared packet is
not a successful result; only an imported brief or an explicit insufficient-
sources result is terminal.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from continuous_workflow import discover, finalize, prepare
from domain_registry import (
    DomainRegistry,
    default_registry_path,
    validate_completed_workspace,
)
from researchramp_core import read_json, write_json
from researchramp_license import enforce_business_license


SCHEMA_VERSION = 1
STATUS_NAME = "brief-generation-status.json"


class BriefGenerationError(RuntimeError):
    """The current generation operation cannot safely continue."""


class DomainSelectionRequired(BriefGenerationError):
    """The requested operation cannot choose one registered domain safely."""

    def __init__(
        self,
        domains: list[dict[str, str]],
        *,
        requested_domain: str | None = None,
    ) -> None:
        self.domains = domains
        self.requested_domain = requested_domain
        if requested_domain is None:
            message = "Multiple ResearchRamp domains are available; ask the user to choose one"
        else:
            message = (
                f"ResearchRamp domain {requested_domain!r} is not registered; "
                "ask the user to choose one"
            )
        super().__init__(message)

    def payload(self) -> dict[str, Any]:
        return {
            "status": "domain_selection_required",
            "error": str(self),
            "requested_domain": self.requested_domain,
            "domains": self.domains,
        }


def _read_object(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def resolve_workspace(
    registry_path: Path,
    domain_id: str | None,
    explicit_workspace: Path | None,
) -> tuple[Path, str | None]:
    if explicit_workspace is not None:
        if domain_id is not None:
            raise BriefGenerationError("Use either --workspace or --domain, not both")
        return explicit_workspace.expanduser().resolve(), None

    registry = DomainRegistry(registry_path)
    if not registry.domains:
        raise BriefGenerationError("No initialized ResearchRamp domain is registered")
    choices = [
        {"domain_id": item.domain_id, "display_name": item.display_name}
        for item in registry.domains
    ]
    if domain_id is None:
        if len(registry.domains) != 1:
            raise DomainSelectionRequired(choices)
        registration = registry.domains[0]
    else:
        try:
            registration = registry.get(domain_id)
        except (KeyError, ValueError):
            raise DomainSelectionRequired(choices, requested_domain=domain_id)
    return Path(registration.workspace).resolve(), registration.domain_id


class BriefGenerationController:
    def __init__(self, workspace: Path, domain_id: str | None = None) -> None:
        self.workspace = workspace.resolve()
        self.domain_id = domain_id
        self.root = self.workspace / "continuous" / "generation"
        self.status_path = self.workspace / "continuous" / STATUS_NAME
        self.previous: dict[str, Any] | None = None
        if self.status_path.is_file():
            candidate = _read_object(self.status_path)
            if (
                candidate.get("schema_version") == SCHEMA_VERSION
                and candidate.get("operation") == "generate_brief"
                and candidate.get("terminal") is False
            ):
                self.previous = candidate
        if self.previous is None:
            self.operation_id = uuid.uuid4().hex
            self.revision = 0
        else:
            self.operation_id = str(self.previous["operation_id"])
            self.revision = int(self.previous.get("revision") or 0)
            if self.domain_id is None:
                previous_domain = str(self.previous.get("domain_id") or "").strip()
                self.domain_id = previous_domain or None
        self.operation_dir = self.root / self.operation_id
        self.selection_path = self.operation_dir / "selection.json"
        self.prepared_path = self.operation_dir / "prepared.json"

    def _status(
        self,
        status: str,
        *,
        terminal: bool,
        checkpoint: str,
        next_action: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> dict[str, Any]:
        self.revision += 1
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "operation": "generate_brief",
            "operation_id": self.operation_id,
            "revision": self.revision,
            "workspace": str(self.workspace),
            "domain_id": self.domain_id,
            "status": status,
            "terminal": terminal,
            "checkpoint": checkpoint,
            "next_action": next_action,
        }
        if result is not None:
            payload["result"] = result
        if error is not None:
            payload["error"] = error
        write_json(self.status_path, payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)
        return payload

    def resume_command(self) -> list[str]:
        command = [
            str(Path(sys.executable).absolute()),
            str(Path(__file__).resolve()),
            "run",
            "--workspace",
            str(self.workspace),
        ]
        return command

    def _host_action(
        self,
        action_type: str,
        *,
        inputs: dict[str, Path],
        output: Path,
        checkpoint: str,
        instructions: str,
    ) -> dict[str, Any]:
        return self._status(
            "host_action_required",
            terminal=False,
            checkpoint=checkpoint,
            next_action={
                "actor": "current_host_agent",
                "type": action_type,
                "inputs": {name: str(path) for name, path in inputs.items()},
                "output": str(output),
                "instructions": instructions,
                "resume": self.resume_command(),
            },
        )

    def _discover_once(self) -> None:
        review_input = self.operation_dir / "host-review-input.json"
        candidates = self.operation_dir / "candidates.jsonl"
        if review_input.is_file() and candidates.is_file():
            return
        self._status("running", terminal=False, checkpoint="discovering_sources")
        result = discover(
            self.workspace,
            fresh_days=14,
            recent_days=1460,
            per_query=25,
            query_limit=None,
            include_classics=True,
        )
        self.operation_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(Path(result["review_input"]), review_input)
        shutil.copyfile(Path(result["candidates"]), candidates)

    def _request_selection(self) -> dict[str, Any]:
        review_input = self.operation_dir / "host-review-input.json"
        candidates = self.operation_dir / "candidates.jsonl"
        return self._host_action(
            "select_brief_sources",
            inputs={"review": review_input, "candidates": candidates},
            output=self.selection_path,
            checkpoint="source_selection_needed",
            instructions=(
                "Select 2–5 strong, directly readable sources for this research domain. "
                "Write the normal schema_version=1 selection JSON with period_start, "
                "period_end, selected_candidate_ids, and optional supplemental_items. "
                "If fewer than two reliable sources exist after reviewing all supplied "
                "lanes and any verified public sources you can find, instead write "
                '{"schema_version":1,"outcome":"insufficient","reason":"..."}. '
                "Do not stop after writing the file; immediately run the supplied resume command."
            ),
        )

    def _prepare_once(self) -> dict[str, Any]:
        if self.prepared_path.is_file():
            return _read_object(self.prepared_path)
        self._status("running", terminal=False, checkpoint="preparing_sources")
        prepared = prepare(self.workspace, self.selection_path)
        packet_path = Path(str(prepared["agent_input"])).resolve()
        packet = _read_object(packet_path)
        brief_id = (
            "brief-"
            + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            + "-"
            + self.operation_id[:8]
        )
        packet["brief_id"] = brief_id
        packet["agent_requirements"] = [
            *list(packet.get("agent_requirements") or []),
            f"Use the exact brief_id {brief_id}.",
            "Use the exact period_start and period_end from this packet.",
            "After writing agent_output_path, immediately resume the generation controller.",
        ]
        write_json(packet_path, packet)
        prepared = {**prepared, "brief_id": brief_id}
        self.operation_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.prepared_path, prepared)
        return prepared

    def _request_writing(self, prepared: dict[str, Any]) -> dict[str, Any]:
        return self._host_action(
            "write_research_brief",
            inputs={"prepared_brief": Path(str(prepared["agent_input"]))},
            output=Path(str(prepared["agent_output"])),
            checkpoint="brief_writing_needed",
            instructions=(
                "Read the prepared packet and its temporary source texts. Write the "
                "source-grounded brief JSON exactly as required by "
                "references/continuous-workflow.md, including the packet's exact "
                "brief_id and period. Do not stop after writing the file; immediately "
                "run the supplied resume command."
            ),
        )

    def run(self) -> dict[str, Any]:
        validate_completed_workspace(self.workspace)
        if self.previous is not None and self.previous.get("workspace") != str(self.workspace):
            raise BriefGenerationError("Existing generation status belongs to another workspace")
        self.operation_dir.mkdir(parents=True, exist_ok=True)
        self._discover_once()
        if not self.selection_path.is_file():
            return self._request_selection()
        selection = _read_object(self.selection_path)
        if selection.get("outcome") == "insufficient":
            reason = str(selection.get("reason") or "No reliable brief sources were available")
            return self._status(
                "no_brief_generated",
                terminal=True,
                checkpoint="insufficient_sources",
                result={"generated": False, "reason": reason},
            )
        prepared = self._prepare_once()
        output_path = Path(str(prepared["agent_output"])).resolve()
        if not output_path.is_file():
            return self._request_writing(prepared)
        self._status("running", terminal=False, checkpoint="finalizing_brief")
        result = finalize(self.workspace, output_path)
        return self._status(
            "brief_generated",
            terminal=True,
            checkpoint="brief_ready",
            result={"generated": True, **result},
        )

    def inspect(self) -> dict[str, Any]:
        if not self.status_path.is_file():
            raise BriefGenerationError(f"No brief generation status exists in {self.workspace}")
        payload = _read_object(self.status_path)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status"), nargs="?", default="run")
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    parser.add_argument("--domain")
    parser.add_argument("--workspace", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    enforce_business_license("brief_generation")
    try:
        workspace, domain_id = resolve_workspace(
            args.registry.expanduser().resolve(), args.domain, args.workspace
        )
        controller = BriefGenerationController(workspace, domain_id)
        if args.command == "status":
            controller.inspect()
        else:
            controller.run()
        return 0
    except DomainSelectionRequired as error:
        print(json.dumps(error.payload(), ensure_ascii=False))
        return 1
    except (BriefGenerationError, FileNotFoundError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
