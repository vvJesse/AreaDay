from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL_DIR / "scripts"))

from researchramp_core import read_json  # noqa: E402
from run_timing import RunTimeline  # noqa: E402


class FakeClock:
    def __init__(self) -> None:
        self.monotonic_values = iter([10.0, 12.5, 20.0, 21.25])
        self.wall_values = iter(
            [
                "2026-01-01T00:00:00.000+00:00",
                "2026-01-01T00:00:02.500+00:00",
                "2026-01-01T00:01:00.000+00:00",
                "2026-01-01T00:01:01.250+00:00",
            ]
        )

    def monotonic(self) -> float:
        return next(self.monotonic_values)

    def wall(self) -> str:
        return next(self.wall_values)


class RunTimelineTests(unittest.TestCase):
    def test_phases_persist_across_cli_like_instances(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run-timings.json"
            clock = FakeClock()
            first = RunTimeline(path, monotonic=clock.monotonic, wall_now=clock.wall)
            with first.phase("search", details={"queries": 3}):
                pass

            second = RunTimeline(path, monotonic=clock.monotonic, wall_now=clock.wall)
            with second.phase("download"):
                pass

            phases = second.snapshot()["phases"]
            workflow = second.snapshot()["workflow"]
            self.assertEqual(list(phases), ["search", "download"])
            self.assertEqual(phases["search"]["elapsed_seconds"], 2.5)
            self.assertEqual(phases["search"]["details"], {"queries": 3})
            self.assertEqual(phases["download"]["elapsed_seconds"], 1.25)
            self.assertEqual(workflow["wall_elapsed_seconds"], 61.25)
            self.assertEqual(
                read_json(path)["workflow"]["wall_elapsed_seconds"],
                61.25,
            )

    def test_failed_phase_is_diagnostic_and_reraised(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "run-timings.json"
            clock = FakeClock()
            timeline = RunTimeline(path, monotonic=clock.monotonic, wall_now=clock.wall)
            with self.assertRaisesRegex(ValueError, "synthetic"):
                with timeline.phase("analysis"):
                    raise ValueError("synthetic")
            phase = timeline.snapshot()["phases"]["analysis"]
            self.assertEqual(phase["status"], "failed")
            self.assertEqual(phase["error_type"], "ValueError")


if __name__ == "__main__":
    unittest.main()
