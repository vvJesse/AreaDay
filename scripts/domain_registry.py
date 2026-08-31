#!/usr/bin/env python3
"""Explicit local registry for isolated ResearchRamp research domains.

The registry stores labels and user-confirmed workspace paths only. It never
searches the filesystem for corpora and it never copies vocabulary or brief
records between workspaces.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
DOMAIN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")


def utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def default_registry_path() -> Path:
    """Return this Skill instance's registry without creating or reading it."""

    return Path(__file__).resolve().parents[1] / "researchramp-data" / "real-domains.json"


def _read_profile(workspace: Path) -> dict[str, Any]:
    for name in ("research-profile-input.json", "research-profile.json"):
        path = workspace / name
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid research profile: {path}") from error
        if isinstance(payload, dict):
            return payload
    raise FileNotFoundError(f"Research profile not found in workspace: {workspace}")


def _normalized_domain_id(value: str) -> str:
    candidate = value.strip().lower()
    if not DOMAIN_ID_PATTERN.fullmatch(candidate):
        raise ValueError(f"Invalid ResearchRamp domain ID: {candidate}")
    return candidate


def validate_registration_workspace(workspace: Path) -> dict[str, Any]:
    """Verify the confirmed identity needed to register a workspace path."""

    resolved = workspace.expanduser().resolve()
    if not resolved.is_dir():
        raise FileNotFoundError(f"ResearchRamp workspace not found: {resolved}")
    profile = _read_profile(resolved)
    if profile.get("confirmed") is not True:
        raise ValueError(f"ResearchRamp profile is not confirmed: {resolved}")
    if not str(profile.get("profile_id") or "").strip():
        raise ValueError(f"ResearchRamp profile ID is missing: {resolved}")
    return profile


def validate_initialized_workspace(workspace: Path) -> dict[str, Any]:
    """Verify the real corpus assets required to start or resume calibration."""

    resolved = workspace.expanduser().resolve()
    profile = validate_registration_workspace(resolved)
    vocabulary = resolved / "analysis" / "vocabulary-map.tsv"
    if not vocabulary.is_file():
        raise FileNotFoundError(
            "ResearchRamp initialization is incomplete; missing: " + str(vocabulary)
        )
    return profile


def validate_completed_workspace(workspace: Path) -> dict[str, Any]:
    """Verify that a workspace can safely appear in the unified application."""

    resolved = workspace.expanduser().resolve()
    profile = validate_initialized_workspace(resolved)
    required = (
        resolved / "analysis" / "vocabulary-map.tsv",
        resolved / "analysis" / "vocabulary-calibration-session.json",
        resolved / "analysis" / "vocabulary-calibration-result.json",
        resolved / "analysis" / "personalized-vocabulary.tsv",
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(
            "ResearchRamp initialization is incomplete; missing: " + ", ".join(missing)
        )
    try:
        session = json.loads(required[1].read_text(encoding="utf-8"))
        result = json.loads(required[2].read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid completed calibration in workspace: {resolved}") from error
    answers = session.get("answers")
    counts = result.get("counts")
    if not isinstance(answers, list) or len(answers) != 30 or not isinstance(counts, dict):
        raise ValueError(f"ResearchRamp calibration is not complete: {resolved}")
    if not isinstance(result.get("threshold"), dict) or not isinstance(
        result.get("importance"), dict
    ):
        raise ValueError(f"ResearchRamp calibration result is incomplete: {resolved}")
    if not str(result.get("vocabulary_snapshot_sha256") or ""):
        raise ValueError(f"ResearchRamp calibration snapshot is missing: {resolved}")
    export_bytes = required[3].read_bytes()
    expected_export_hash = str(result.get("personalized_vocabulary_sha256") or "")
    if not expected_export_hash or hashlib.sha256(export_bytes).hexdigest() != expected_export_hash:
        raise ValueError(f"ResearchRamp personalized vocabulary is inconsistent: {resolved}")
    export_rows = max(0, export_bytes.count(b"\n") - 1)
    if export_rows != int(counts.get("total") or 0):
        raise ValueError(f"ResearchRamp personalized vocabulary row count is inconsistent: {resolved}")
    return profile


@dataclass(frozen=True)
class DomainRegistration:
    domain_id: str
    display_name: str
    workspace: str
    registered_at: str

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "DomainRegistration":
        domain_id = _normalized_domain_id(str(payload.get("domain_id") or ""))
        display_name = str(payload.get("display_name") or "").strip()
        workspace = str(payload.get("workspace") or "").strip()
        registered_at = str(payload.get("registered_at") or "").strip()
        if not display_name or not workspace or not registered_at:
            raise ValueError(f"Invalid domain registration: {domain_id}")
        return cls(
            domain_id=domain_id,
            display_name=display_name,
            workspace=str(Path(workspace).expanduser().resolve()),
            registered_at=registered_at,
        )


class DomainRegistry:
    """A manifest of explicitly registered, mutually isolated workspaces."""

    def __init__(self, path: Path):
        self.path = path.expanduser().resolve()
        self._lock_path = self.path.with_suffix(self.path.suffix + ".tmp")
        self._domains: list[DomainRegistration] = []
        self.active_domain_id: str | None = None
        self._load()

    @property
    def domains(self) -> tuple[DomainRegistration, ...]:
        return tuple(self._domains)

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"Invalid ResearchRamp domain registry: {self.path}") from error
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"Unsupported ResearchRamp domain registry: {self.path}")
        raw_domains = payload.get("domains")
        if not isinstance(raw_domains, list):
            raise ValueError("ResearchRamp domain registry requires a domains list")
        domains = [DomainRegistration.from_payload(item) for item in raw_domains]
        ids = [item.domain_id for item in domains]
        paths = [item.workspace for item in domains]
        if len(ids) != len(set(ids)):
            raise ValueError("ResearchRamp domain registry contains duplicate domain IDs")
        if len(paths) != len(set(paths)):
            raise ValueError("ResearchRamp domain registry contains duplicate workspace paths")
        active = payload.get("active_domain_id")
        if active is not None and active not in set(ids):
            raise ValueError("ResearchRamp registry active domain is not registered")
        self._domains = domains
        self.active_domain_id = str(active) if active is not None else (ids[0] if ids else None)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "active_domain_id": self.active_domain_id,
            "domains": [asdict(item) for item in self._domains],
        }
        self._lock_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        os.replace(self._lock_path, self.path)

    def get(self, domain_id: str) -> DomainRegistration:
        normalized = _normalized_domain_id(domain_id)
        for item in self._domains:
            if item.domain_id == normalized:
                return item
        raise KeyError(f"Unknown ResearchRamp domain: {normalized}")

    def register(
        self,
        workspace: Path,
        *,
        display_name: str | None = None,
        domain_id: str | None = None,
        make_active: bool = True,
    ) -> DomainRegistration:
        profile = validate_registration_workspace(workspace)
        resolved = workspace.expanduser().resolve()
        requested_label = (
            display_name.strip()
            if display_name is not None and display_name.strip()
            else None
        )
        if domain_id is None:
            for existing in self._domains:
                if existing.workspace != str(resolved):
                    continue
                updated = DomainRegistration(
                    domain_id=existing.domain_id,
                    display_name=requested_label or existing.display_name,
                    workspace=existing.workspace,
                    registered_at=existing.registered_at,
                )
                self._domains = [
                    updated if item.domain_id == existing.domain_id else item
                    for item in self._domains
                ]
                if make_active:
                    self.active_domain_id = existing.domain_id
                self._save()
                return updated
            domain_id = str(profile.get("profile_id") or "")
        normalized_id = _normalized_domain_id(domain_id)

        for existing in self._domains:
            if existing.workspace == str(resolved) and existing.domain_id != normalized_id:
                raise ValueError(
                    f"Workspace is already registered as another domain: {existing.domain_id}"
                )
            if existing.domain_id == normalized_id:
                if existing.workspace != str(resolved):
                    raise ValueError(
                        f"Domain ID is already registered to another workspace: {normalized_id}"
                    )
                updated = DomainRegistration(
                    domain_id=normalized_id,
                    display_name=requested_label or existing.display_name,
                    workspace=str(resolved),
                    registered_at=existing.registered_at,
                )
                self._domains = [updated if item.domain_id == normalized_id else item for item in self._domains]
                if make_active:
                    self.active_domain_id = normalized_id
                self._save()
                return updated

        label = requested_label or str(profile.get("title") or "").strip() or normalized_id
        registration = DomainRegistration(
            domain_id=normalized_id,
            display_name=label,
            workspace=str(resolved),
            registered_at=utc_iso(),
        )
        self._domains.append(registration)
        if make_active or self.active_domain_id is None:
            self.active_domain_id = normalized_id
        self._save()
        return registration

    def set_active(self, domain_id: str) -> DomainRegistration:
        registration = self.get(domain_id)
        self.active_domain_id = registration.domain_id
        self._save()
        return registration


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    subparsers = parser.add_subparsers(dest="command", required=True)

    register_parser = subparsers.add_parser("register")
    register_parser.add_argument("--workspace", type=Path, required=True)
    register_parser.add_argument("--domain-id")
    register_parser.add_argument("--label")

    active_parser = subparsers.add_parser("set-active")
    active_parser.add_argument("--domain-id", required=True)
    subparsers.add_parser("list")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    registry = DomainRegistry(args.registry)
    if args.command == "register":
        item = registry.register(
            args.workspace,
            display_name=args.label,
            domain_id=args.domain_id,
        )
        payload: Any = {"status": "registered", **asdict(item), "registry": str(registry.path)}
    elif args.command == "set-active":
        item = registry.set_active(args.domain_id)
        payload = {"status": "active", **asdict(item), "registry": str(registry.path)}
    elif args.command == "list":
        payload = {
            "schema_version": SCHEMA_VERSION,
            "registry": str(registry.path),
            "active_domain_id": registry.active_domain_id,
            "domains": [asdict(item) for item in registry.domains],
        }
    else:
        raise AssertionError(args.command)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
