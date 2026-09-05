from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from generate_brief import BriefGenerationController  # noqa: E402
from continuous_state import ContinuousStore  # noqa: E402
from vocabulary_cards import card_id  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "weekly-brief.json"


class BriefGenerationControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name).resolve()
        (self.workspace / "continuous" / "discovery").mkdir(parents=True)
        analysis = self.workspace / "analysis"
        analysis.mkdir()
        (analysis / "papers.jsonl").write_text("", encoding="utf-8")
        (analysis / "first-terminology-map.jsonl").write_text("", encoding="utf-8")
        (analysis / "terminology-explanations.json").write_text("{}\n", encoding="utf-8")
        cards = [
            ("alpha", "A synthetic term used in the Alpha test domain.", "测试领域 Alpha 中使用的合成术语"),
            ("isolation", "The separation of distinct data spaces.", "不同数据空间彼此分离"),
            ("provenance", "A verifiable link between content and its source.", "内容及其来源之间可验证的对应关系"),
        ]
        (analysis / "vocabulary-card-catalog.jsonl").write_text(
            "".join(
                json.dumps(
                    {
                        "card_id": card_id(lemma, "noun"),
                        "sense_key": card_id(lemma, "noun"),
                        "lemma": lemma,
                        "part_of_speech": "noun",
                        "meaning_en": meaning_en,
                        "meaning_zh": meaning_zh,
                        "meaning_origin": "agent",
                        "source_paper_id": "W1",
                        "source_title": "Synthetic source",
                        "source_url": "https://example.invalid/synthetic-source",
                        "context": f"Synthetic context for {lemma}.",
                        "total_count": 10,
                        "document_count": 2,
                        "document_share": 1.0,
                    }
                ) + "\n"
                for lemma, meaning_en, meaning_zh in cards
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def fake_discover(self, *args, **kwargs) -> dict:
        discovery = self.workspace / "continuous" / "discovery"
        review = discovery / "host-review-input.json"
        candidates = discovery / "candidates.jsonl"
        review.write_text('{"schema_version":2}\n', encoding="utf-8")
        candidates.write_text(
            '{"candidate_id":"paper-a"}\n{"candidate_id":"paper-b"}\n',
            encoding="utf-8",
        )
        return {"review_input": str(review), "candidates": str(candidates)}

    def fake_prepare(self, workspace: Path, selection: Path) -> dict:
        del selection
        run_dir = workspace / "continuous" / "working" / "fixture-run"
        run_dir.mkdir(parents=True)
        packet = run_dir / "agent-brief-input.json"
        output = run_dir / "brief-agent-output.json"
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        packet.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "run_id": "fixture-run",
                    "workspace": str(workspace),
                    "period_start": "2026-08-19",
                    "period_end": "2026-09-01",
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
                        for item in fixture["items"]
                    ],
                    "agent_output_path": str(output),
                    "agent_requirements": [],
                }
            ),
            encoding="utf-8",
        )
        return {
            "status": "ready_for_agent",
            "agent_input": str(packet),
            "agent_output": str(output),
            "run_dir": str(run_dir),
        }

    @patch("generate_brief.validate_completed_workspace")
    @patch("generate_brief.prepare")
    @patch("generate_brief.discover")
    def test_one_operation_continues_until_one_imported_brief(
        self, discover_mock, prepare_mock, _validate_mock
    ) -> None:
        discover_mock.side_effect = self.fake_discover
        prepare_mock.side_effect = self.fake_prepare

        first = BriefGenerationController(self.workspace, "alpha").run()
        self.assertFalse(first["terminal"])
        self.assertEqual(first["checkpoint"], "source_selection_needed")

        selection_path = Path(first["next_action"]["output"])
        selection_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "period_start": "2026-08-19",
                    "period_end": "2026-09-01",
                    "selected_candidate_ids": ["paper-a", "paper-b"],
                }
            ),
            encoding="utf-8",
        )

        second = BriefGenerationController(self.workspace, "alpha").run()
        self.assertFalse(second["terminal"])
        self.assertEqual(second["checkpoint"], "brief_writing_needed")
        packet = json.loads(
            Path(second["next_action"]["inputs"]["prepared_brief"]).read_text(
                encoding="utf-8"
            )
        )
        self.assertRegex(packet["brief_id"], r"^brief-\d{8}T\d{6}Z-[0-9a-f]{8}$")

        output = json.loads(FIXTURE.read_text(encoding="utf-8"))
        output.pop("created_at", None)
        output["brief_id"] = packet["brief_id"]
        output["period_start"] = packet["period_start"]
        output["period_end"] = packet["period_end"]
        Path(second["next_action"]["output"]).write_text(
            json.dumps(output), encoding="utf-8"
        )
        third = BriefGenerationController(self.workspace, "alpha").run()
        self.assertTrue(third["terminal"])
        self.assertTrue(third["result"]["generated"])
        self.assertEqual(third["checkpoint"], "brief_ready")
        briefs = ContinuousStore(self.workspace).list_briefs()
        self.assertEqual(len(briefs), 1)
        self.assertEqual(briefs[0]["brief_id"], packet["brief_id"])

    @patch("generate_brief.validate_completed_workspace")
    @patch("generate_brief.prepare")
    @patch("generate_brief.discover")
    def test_insufficient_sources_is_terminal_and_saves_no_brief(
        self, discover_mock, prepare_mock, _validate_mock
    ) -> None:
        discover_mock.side_effect = self.fake_discover
        first = BriefGenerationController(self.workspace, "alpha").run()
        Path(first["next_action"]["output"]).write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "outcome": "insufficient",
                    "reason": "Only one reliable source was available",
                }
            ),
            encoding="utf-8",
        )
        result = BriefGenerationController(self.workspace, "alpha").run()
        self.assertTrue(result["terminal"])
        self.assertFalse(result["result"]["generated"])
        prepare_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
