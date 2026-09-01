from __future__ import annotations

import sys
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from candidate_review import (  # noqa: E402
    build_candidate_review_packet,
    default_review_limit,
)
from acquire_mini_corpus import merge_candidates  # noqa: E402


def candidate(index: int, provider: str, query_id: str, lane: str = "recent"):
    return {
        "candidate_id": f"{provider}:{index}",
        "provider": provider,
        "query_hits": [{"query_id": query_id, "date_lane": lane}],
    }


class CandidateReviewPacketTests(unittest.TestCase):
    def test_default_limit_matches_the_selection_ceiling_for_target_70(self) -> None:
        self.assertEqual(default_review_limit(70), 100)
        self.assertEqual(default_review_limit(5), 15)

    def test_cross_provider_merge_preserves_both_coverage_buckets(self) -> None:
        common = {
            "title": "Shared synthetic paper",
            "abstract": "Synthetic abstract",
            "doi": "10.1000/shared",
            "alternate_pdf_urls": [],
        }
        merged = merge_candidates(
            [
                {
                    **common,
                    "candidate_id": "OpenAlex:W1",
                    "provider": "openalex",
                    "query_hits": [
                        {
                            "provider": "openalex",
                            "query_id": "shared-id",
                            "date_lane": "all",
                        }
                    ],
                }
            ],
            [
                {
                    **common,
                    "candidate_id": "arXiv:2401.00001",
                    "provider": "arxiv",
                    "query_hits": [
                        {
                            "provider": "arxiv",
                            "query_id": "shared-id",
                            "date_lane": "recent",
                        }
                    ],
                }
            ],
        )
        packet, summary = build_candidate_review_packet(
            merged,
            target_papers=1,
            limit=1,
        )
        self.assertEqual(len(packet), 1)
        self.assertEqual(
            summary["coverage_counts"],
            {
                "openalex:shared-id:all": 1,
                "arxiv:shared-id:recent": 1,
            },
        )

    def test_packet_preserves_provider_query_and_date_lane_coverage(self) -> None:
        candidates = [
            *[candidate(index, "openalex", "broad", "all") for index in range(6)],
            candidate(10, "openalex", "narrow", "all"),
            candidate(20, "arxiv", "recent", "recent"),
            candidate(21, "arxiv", "foundation", "foundation"),
        ]
        packet, summary = build_candidate_review_packet(
            candidates,
            target_papers=4,
            limit=4,
        )
        keys = {
            (item["provider"], item["query_hits"][0]["query_id"], item["query_hits"][0]["date_lane"])
            for item in packet
        }
        self.assertEqual(
            keys,
            {
                ("openalex", "broad", "all"),
                ("openalex", "narrow", "all"),
                ("arxiv", "recent", "recent"),
                ("arxiv", "foundation", "foundation"),
            },
        )
        self.assertEqual(summary["review_packet_count"], 4)
        self.assertEqual(summary["deferred_candidate_count"], 5)

    def test_packet_is_deterministic_and_never_invents_candidates(self) -> None:
        candidates = [
            candidate(1, "openalex", "q1"),
            candidate(2, "openalex", "q1"),
            candidate(3, "arxiv", "q2"),
            candidate(4, "arxiv", "q2"),
        ]
        first, _ = build_candidate_review_packet(candidates, target_papers=2, limit=3)
        second, _ = build_candidate_review_packet(candidates, target_papers=2, limit=3)
        self.assertEqual(first, second)
        self.assertEqual(
            [item["candidate_id"] for item in first],
            ["openalex:1", "arxiv:3", "openalex:2"],
        )
        self.assertTrue(all(item in candidates for item in first))

    def test_limit_cannot_undercut_download_target(self) -> None:
        with self.assertRaisesRegex(ValueError, "smaller"):
            build_candidate_review_packet([], target_papers=3, limit=2)

    def test_explicit_limit_cannot_drop_a_coverage_bucket(self) -> None:
        candidates = [
            candidate(index, "openalex", f"q{index}") for index in range(4)
        ]
        with self.assertRaisesRegex(ValueError, "preserve.*coverage"):
            build_candidate_review_packet(
                candidates,
                target_papers=2,
                limit=2,
            )


if __name__ == "__main__":
    unittest.main()
