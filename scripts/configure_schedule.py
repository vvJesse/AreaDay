#!/usr/bin/env python3
"""Save one ResearchRamp schedule preference and emit exactly one task handoff."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from continuous_state import ContinuousStore
from domain_registry import default_registry_path, validate_completed_workspace
from generate_brief import (
    BriefGenerationError,
    DomainSelectionRequired,
    resolve_workspace,
)
from researchramp_license import enforce_business_license


def valid_time(value: str) -> str:
    try:
        return datetime.strptime(value, "%H:%M").strftime("%H:%M")
    except ValueError as error:
        raise argparse.ArgumentTypeError("time must use HH:MM") from error


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=("weekly", "daily"))
    parser.add_argument("--registry", type=Path, default=default_registry_path())
    parser.add_argument("--domain")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--time", type=valid_time, required=True)
    parser.add_argument("--weekday", type=int, choices=range(1, 8))
    parser.add_argument("--disable", action="store_true")
    args = parser.parse_args()
    if args.section == "weekly" and args.weekday is None:
        parser.error("weekly scheduling requires --weekday 1 through 7")
    if args.section == "daily" and args.weekday is not None:
        parser.error("daily reminders do not accept --weekday")
    return args


def main() -> int:
    args = parse_args()
    enforce_business_license("scheduling")
    try:
        workspace, domain_id = resolve_workspace(
            args.registry.expanduser().resolve(), args.domain, args.workspace
        )
        validate_completed_workspace(workspace)
        store = ContinuousStore(workspace, domain_id=domain_id)
        if args.section == "weekly":
            section = "weekly_brief"
            settings = {
                "enabled": not args.disable,
                "weekday": args.weekday,
                "time": args.time,
            }
        else:
            section = "daily_review"
            settings = {
                "enabled": not args.disable,
                "time": args.time,
                "only_when_due": True,
            }
        saved = store.save_setting(section, settings)
        print(
            json.dumps(
                {
                    "status": "saved",
                    "settings": saved[section],
                    "automation_handoff": store.automation_handoff(section),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    except DomainSelectionRequired as error:
        print(json.dumps(error.payload(), ensure_ascii=False))
        return 1
    except (BriefGenerationError, FileNotFoundError, OSError, ValueError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
