from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from provider_discovery import collect_provider_lanes  # noqa: E402


class ProviderDiscoveryTests(unittest.TestCase):
    def test_lanes_overlap_but_results_follow_confirmed_provider_order(self) -> None:
        barrier = threading.Barrier(2)
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def collector(name: str):
            def run():
                nonlocal active, maximum_active
                with lock:
                    active += 1
                    maximum_active = max(maximum_active, active)
                barrier.wait(timeout=1)
                with lock:
                    active -= 1
                return [{"candidate_id": name}], [name]

            return run

        groups, attempts, timings = collect_provider_lanes(
            ["openalex", "arxiv"],
            {
                "openalex": collector("openalex"),
                "arxiv": collector("arxiv"),
            },
        )
        self.assertEqual(maximum_active, 2)
        self.assertEqual(list(groups), ["openalex", "arxiv"])
        self.assertEqual(list(attempts), ["openalex", "arxiv"])
        self.assertEqual(list(timings), ["openalex", "arxiv"])

    def test_single_lane_does_not_require_parallelism(self) -> None:
        groups, attempts, _ = collect_provider_lanes(
            ["openalex"],
            {"openalex": lambda: ([{"candidate_id": "one"}], ["ok"])},
        )
        self.assertEqual(groups["openalex"][0]["candidate_id"], "one")
        self.assertEqual(attempts["openalex"], ["ok"])

    def test_collector_failure_is_reraised(self) -> None:
        def fail():
            raise RuntimeError("synthetic provider failure")

        with self.assertRaisesRegex(RuntimeError, "synthetic"):
            collect_provider_lanes(["openalex"], {"openalex": fail})


if __name__ == "__main__":
    unittest.main()
