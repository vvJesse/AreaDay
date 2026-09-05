"""Shared validation for a completed vocabulary orthography review."""

from __future__ import annotations

from typing import Any


def orthography_summary_is_complete(summary: Any) -> bool:
    if (
        not isinstance(summary, dict)
        or summary.get("schema_version") != 1
        or summary.get("reviewer") != "current-host-agent"
    ):
        return False
    names = (
        "reviewed_candidate_count",
        "replacement_count",
        "drop_count",
        "explicit_keep_count",
        "unchanged_candidate_count",
    )
    if any(
        isinstance(summary.get(name), bool)
        or not isinstance(summary.get(name), int)
        or summary[name] < 0
        for name in names
    ):
        return False
    return (
        summary["explicit_keep_count"] == summary["unchanged_candidate_count"]
        and summary["reviewed_candidate_count"]
        == summary["replacement_count"]
        + summary["drop_count"]
        + summary["explicit_keep_count"]
    )
