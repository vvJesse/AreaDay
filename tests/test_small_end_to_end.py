from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


SKILL_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from acquire_mini_corpus import SearchAttempt, main  # noqa: E402
from researchramp_core import read_json  # noqa: E402
from tests.test_initial_pipeline import valid_test_profile  # noqa: E402


def synthetic_candidate(index: int) -> dict[str, object]:
    query_index = index % 3 + 1
    return {
        "candidate_id": f"Synthetic:{index}",
        "provider": "openalex",
        "title": f"Synthetic candidate {index}",
        "abstract": "Synthetic metadata used only for an offline workflow test.",
        "pdf_url": f"https://host{index % 2}.example/paper-{index}.pdf",
        "alternate_pdf_urls": [],
        "query_hits": [
            {
                "query_id": f"q0{query_index}",
                "label": f"Example {query_index}",
                "date_lane": "recent",
            }
        ],
    }


class SmallEndToEndTests(unittest.TestCase):
    def test_search_review_download_and_analysis_with_two_synthetic_papers(self) -> None:
        candidates = [synthetic_candidate(index) for index in range(6)]
        attempts = [
            SearchAttempt(f"q0{index}", "openalex", "ok", 2)
            for index in range(1, 4)
        ]

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            profile_path = root / "profile.json"
            profile_path.write_text(
                json.dumps(valid_test_profile()),
                encoding="utf-8",
            )
            registry = MagicMock()

            search_argv = [
                "acquire_mini_corpus.py",
                "--profile",
                str(profile_path),
                "--workspace",
                str(workspace),
                "--target-papers",
                "2",
                "--review-candidate-limit",
                "4",
                "--search-only",
            ]
            with (
                patch.object(sys, "argv", search_argv),
                patch("acquire_mini_corpus.DomainRegistry", return_value=registry),
                patch("acquire_mini_corpus.load_openalex_api_key", return_value=None),
                patch(
                    "acquire_mini_corpus.collect_openalex_candidates",
                    return_value=(candidates, attempts),
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)

            full_candidates = [
                json.loads(line)
                for line in (workspace / "candidates.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            review_packet = [
                json.loads(line)
                for line in (workspace / "candidate-review-packet.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(full_candidates), 6)
            self.assertEqual(len(review_packet), 4)

            # Simulate the host-agent review step without involving the user.
            selection_path = workspace / "agent-candidate-selection.json"
            selection_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "reviewer": "current-host-agent",
                        "selected_candidate_ids": [
                            item["candidate_id"] for item in review_packet[:2]
                        ],
                        "review_summary": "Offline synthetic selection.",
                    }
                ),
                encoding="utf-8",
            )

            def fake_pdf_download(
                url: str,
                destination: Path,
                *,
                session: object | None = None,
            ) -> None:
                del url, session
                time.sleep(0.01)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"%PDF-1.4\nsynthetic PDF\n")

            run_argv = [
                "acquire_mini_corpus.py",
                "--profile",
                str(profile_path),
                "--workspace",
                str(workspace),
                "--target-papers",
                "2",
                "--selection",
                str(selection_path),
                "--analyze",
            ]
            with (
                patch.object(sys, "argv", run_argv),
                patch("acquire_mini_corpus.DomainRegistry", return_value=registry),
                patch("acquire_mini_corpus.load_openalex_api_key", return_value=None),
                patch(
                    "concurrent_downloads.download_licensed_open_access_pdf",
                    side_effect=fake_pdf_download,
                ),
                patch(
                    "acquire_mini_corpus.analyze_corpus",
                    return_value={"analyzed_papers": 2, "synthetic": True},
                ),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(main(), 0)

            summary = read_json(workspace / "cold-start-summary.json")
            timings = read_json(workspace / "run-timings.json")["phases"]
            self.assertEqual(summary["successful_pdfs"], 2)
            self.assertEqual(summary["analysis"]["analyzed_papers"], 2)
            self.assertEqual(
                {item["status"] for item in summary["timings"]["phases"].values()},
                {"ok"},
            )
            self.assertIn("discovery.total", timings)
            self.assertIn("candidate_review.prepare", timings)
            self.assertIn("download", timings)
            self.assertIn("analysis", timings)
            self.assertEqual(len(list((workspace / "papers").glob("*.pdf"))), 2)
            self.assertGreaterEqual(registry.register.call_count, 2)


if __name__ == "__main__":
    unittest.main()
