from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from continuous_state import ContinuousStore  # noqa: E402
from continuous_workflow import finalize  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "weekly-brief.json"


def load_fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def write_empty_terminology_assets(workspace: Path) -> None:
    analysis = workspace / "analysis"
    analysis.mkdir(parents=True, exist_ok=True)
    (analysis / "papers.jsonl").write_text("", encoding="utf-8")
    (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
    (analysis / "terminology-explanations.json").write_text("{}\n", encoding="utf-8")


def write_prepared_run(
    workspace: Path,
    run_name: str,
    payload: dict,
    *,
    packet_workspace: Path | None = None,
) -> tuple[Path, Path]:
    write_empty_terminology_assets(workspace)
    run_dir = workspace / "continuous" / "working" / run_name
    run_dir.mkdir(parents=True)
    output = run_dir / "brief-agent-output.json"
    output.write_text(json.dumps(payload), encoding="utf-8")
    packet = {
        "schema_version": 1,
        "run_id": run_name,
        "workspace": str((packet_workspace or workspace).resolve()),
        "agent_output_path": str(output.resolve()),
        "paper_items": [
            {
                "candidate_id": item["item_id"],
                "item_type": item["item_type"],
                "title": item["title"],
                "source_url": item["source_url"],
                "source_provenance": item.get("source_provenance"),
                "vocabulary_candidates": [
                    {"lemma": word["lemma"], "context": word["context"]}
                    for word in item["vocabulary"]
                ],
            }
            for item in payload["items"]
        ],
    }
    (run_dir / "agent-brief-input.json").write_text(
        json.dumps(packet), encoding="utf-8"
    )
    (run_dir / "paper.pdf").write_bytes(b"%PDF-test-fixture")
    (run_dir / "paper.txt").write_text("temporary full text", encoding="utf-8")
    return run_dir, output


class ContinuousStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        write_empty_terminology_assets(self.workspace)
        self.store = ContinuousStore(
            self.workspace, domain_id="alpha", display_name="Domain Alpha"
        )
        self.brief = load_fixture()
        self.store.import_brief(self.brief)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_preheat_uses_one_global_word_record_and_fsrs_queue(self) -> None:
        paper_id = "fixture-paper-alpha"
        started = self.store.start_preheat(paper_id)
        self.assertTrue(started["preheat_started"])
        self.assertEqual(self.store.summary()["learning_count"], 3)
        self.assertEqual(self.store.summary()["due_count"], 3)

        self.store.start_preheat(paper_id)
        self.assertEqual(self.store.summary()["learning_count"], 3)

        first = self.store.due_words(limit=1)[0]
        self.store.review(first.item_id, "good")
        self.assertEqual(self.store.summary()["due_count"], 2)

        second = self.store.due_words(limit=1)[0]
        self.store.mark_item_mastered(second.item_id)
        summary = self.store.summary()
        self.assertEqual(summary["mastered_count"], 1)
        self.assertEqual(summary["due_count"], 1)

    def test_idempotent_brief_import_preserves_preheat_sources(self) -> None:
        self.store.start_preheat("fixture-paper-alpha")
        before = [word.__dict__ for word in self.store.due_words()]
        imported = self.store.import_brief(load_fixture())
        after = [word.__dict__ for word in self.store.due_words()]
        self.assertEqual(imported["brief_id"], self.brief["brief_id"])
        self.assertEqual(after, before)
        self.assertTrue(all(item["context"] and item["source_title"] for item in after))

    def test_schedule_round_trip_and_handoff_is_domain_specific(self) -> None:
        saved = self.store.save_settings(
            {
                "weekly_brief": {"enabled": True, "weekday": 5, "time": "10:30"},
                "daily_review": {"enabled": True, "time": "19:45"},
            }
        )
        self.assertEqual(saved["weekly_brief"]["weekday"], 5)
        handoff = self.store.automation_handoff()
        self.assertEqual(handoff["domain_id"], "alpha")
        self.assertEqual(handoff["workspace"], str(self.workspace.resolve()))
        self.assertEqual(
            handoff["weekly_brief"]["automation_key"],
            "researchramp:alpha:weekly-brief",
        )
        self.assertIn("Domain Alpha", handoff["daily_review"]["name"])
        self.assertTrue(handoff["daily_review"]["only_when_due"])

    def test_weekly_and_daily_schedules_are_saved_and_handed_off_independently(self) -> None:
        defaults = self.store.get_settings()
        self.assertFalse(defaults["weekly_brief"]["enabled"])
        self.assertFalse(defaults["daily_review"]["enabled"])

        weekly = self.store.save_setting(
            "weekly_brief", {"enabled": True, "weekday": 2, "time": "08:15"}
        )
        self.assertTrue(weekly["weekly_brief"]["enabled"])
        self.assertFalse(weekly["daily_review"]["enabled"])
        weekly_handoff = self.store.automation_handoff("weekly_brief")
        self.assertEqual(weekly_handoff["section"], "weekly_brief")
        self.assertEqual(
            weekly_handoff["automation"]["automation_key"],
            "researchramp:alpha:weekly-brief",
        )
        self.assertNotIn("daily_discovery", weekly_handoff)
        self.assertNotIn("daily_review", weekly_handoff)

        daily = self.store.save_setting(
            "daily_review", {"enabled": True, "time": "19:20"}
        )
        self.assertEqual(daily["weekly_brief"]["time"], "08:15")
        self.assertTrue(daily["daily_review"]["enabled"])
        daily_handoff = self.store.automation_handoff("daily_review")
        self.assertEqual(daily_handoff["section"], "daily_review")
        self.assertEqual(
            daily_handoff["automation"]["automation_key"],
            "researchramp:alpha:daily-review",
        )
        self.assertNotIn("weekly_brief", daily_handoff)

    def test_unrecommended_candidates_remain_until_matching_brief_import(self) -> None:
        candidates = [
            {"candidate_id": "paper-a", "title": "A", "doi": "10.1/a"},
            {"candidate_id": "paper-b", "title": "B", "doi": "10.1/b"},
        ]
        self.assertEqual(self.store.record_discoveries(candidates), 2)
        self.assertEqual(
            {item["candidate_id"] for item in self.store.unrecommended_discoveries()},
            {"paper-a", "paper-b"},
        )
        brief = load_fixture()
        brief["brief_id"] = "fixture-recommend-paper-a"
        brief["items"][0]["item_id"] = "paper-a"
        brief["items"][1]["item_id"] = "fixture-guide-recommend-paper-a"
        self.store.import_brief(brief)
        self.assertEqual(
            [item["candidate_id"] for item in self.store.unrecommended_discoveries()],
            ["paper-b"],
        )

    def test_finalize_imports_before_removing_only_its_run(self) -> None:
        workspace = self.workspace / "fresh-finalize"
        ContinuousStore(workspace)
        sibling = workspace / "continuous" / "working" / "failed-sibling"
        sibling.mkdir(parents=True)
        (sibling / "diagnostic.txt").write_text("keep", encoding="utf-8")
        payload = load_fixture()
        run_dir, output = write_prepared_run(workspace, "successful-run", payload)

        result = finalize(workspace, output)
        self.assertTrue(result["temporary_files_deleted"])
        self.assertFalse(run_dir.exists())
        self.assertTrue(sibling.is_dir())
        self.assertEqual(len(ContinuousStore(workspace).list_briefs()), 1)
        self.assertTrue(
            (
                workspace
                / "continuous"
                / "briefs"
                / "fixture-2026-W35-domain-alpha.json"
            ).is_file()
        )

    def test_finalize_cleanup_failure_can_retry_without_losing_run(self) -> None:
        workspace = self.workspace / "retry-finalize"
        ContinuousStore(workspace)
        payload = load_fixture()
        payload.pop("created_at", None)
        run_dir, output = write_prepared_run(workspace, "retry-run", payload)

        with patch("continuous_workflow.shutil.rmtree", side_effect=OSError("busy")):
            with self.assertRaisesRegex(OSError, "busy"):
                finalize(workspace, output)
        self.assertTrue(run_dir.is_dir())

        result = finalize(workspace, output)
        self.assertTrue(result["temporary_files_deleted"])
        self.assertFalse(run_dir.exists())
        self.assertEqual(len(ContinuousStore(workspace).list_briefs()), 1)

    def test_finalize_rejects_crossed_paper_link_and_preserves_run(self) -> None:
        workspace = self.workspace / "crossed-source"
        ContinuousStore(workspace)
        packet_payload = load_fixture()
        run_dir, output = write_prepared_run(workspace, "bad-source", packet_payload)
        changed = load_fixture()
        changed["items"][0]["source_url"] = "https://example.invalid/different-paper"
        output.write_text(json.dumps(changed), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "changed its source source_url"):
            finalize(workspace, output)
        self.assertTrue(run_dir.is_dir())

    def test_finalize_rejects_controller_metadata_changes(self) -> None:
        workspace = self.workspace / "changed-controller-metadata"
        ContinuousStore(workspace)
        payload = load_fixture()
        run_dir, output = write_prepared_run(workspace, "metadata-run", payload)
        packet_path = run_dir / "agent-brief-input.json"
        packet = json.loads(packet_path.read_text(encoding="utf-8"))
        packet["brief_id"] = "controller-assigned-id"
        packet["period_start"] = payload["period_start"]
        packet["period_end"] = payload["period_end"]
        packet_path.write_text(json.dumps(packet), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "controller-assigned brief_id"):
            finalize(workspace, output)
        self.assertTrue(run_dir.is_dir())

    def test_finalize_rejects_packet_from_another_workspace(self) -> None:
        workspace = self.workspace / "domain-a"
        other = self.workspace / "domain-b"
        ContinuousStore(workspace)
        other.mkdir()
        payload = load_fixture()
        run_dir, output = write_prepared_run(
            workspace, "foreign-packet", payload, packet_workspace=other
        )
        with self.assertRaisesRegex(ValueError, "different ResearchRamp workspace"):
            finalize(workspace, output)
        self.assertTrue(run_dir.is_dir())

    def test_unsafe_brief_id_is_rejected_before_archive(self) -> None:
        payload = load_fixture()
        payload["brief_id"] = "../../analysis/vocabulary-calibration-result"
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.store.import_brief(payload)


if __name__ == "__main__":
    unittest.main()
