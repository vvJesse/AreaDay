from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import configure_schedule  # noqa: E402
import generate_brief  # noqa: E402
from generate_brief import DomainSelectionRequired, resolve_workspace  # noqa: E402


class DomainResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.alpha = self.root / "alpha-workspace"
        self.beta = self.root / "beta-workspace"
        self.alpha.mkdir()
        self.beta.mkdir()
        self.registry = self.root / "registry.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_registry(self, domains: list[tuple[str, str, Path]], active: str) -> None:
        self.registry.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "active_domain_id": active,
                    "domains": [
                        {
                            "domain_id": domain_id,
                            "display_name": display_name,
                            "workspace": str(workspace),
                            "registered_at": "2026-09-01T00:00:00+00:00",
                        }
                        for domain_id, display_name, workspace in domains
                    ],
                }
            ),
            encoding="utf-8",
        )

    def test_one_registered_domain_is_selected_without_asking(self) -> None:
        self.write_registry([("alpha", "Alpha Research", self.alpha)], "alpha")
        workspace, domain_id = resolve_workspace(self.registry, None, None)
        self.assertEqual(workspace, self.alpha)
        self.assertEqual(domain_id, "alpha")

    def test_multiple_domains_ignore_active_domain_and_require_a_choice(self) -> None:
        self.write_registry(
            [
                ("alpha", "Alpha Research", self.alpha),
                ("beta", "Beta Research", self.beta),
            ],
            "beta",
        )
        with self.assertRaises(DomainSelectionRequired) as raised:
            resolve_workspace(self.registry, None, None)
        payload = raised.exception.payload()
        self.assertEqual(payload["status"], "domain_selection_required")
        self.assertIsNone(payload["requested_domain"])
        self.assertEqual(
            payload["domains"],
            [
                {"domain_id": "alpha", "display_name": "Alpha Research"},
                {"domain_id": "beta", "display_name": "Beta Research"},
            ],
        )

    def test_unknown_named_domain_returns_registered_choices_without_fallback(self) -> None:
        self.write_registry(
            [
                ("alpha", "Alpha Research", self.alpha),
                ("beta", "Beta Research", self.beta),
            ],
            "beta",
        )
        with self.assertRaises(DomainSelectionRequired) as raised:
            resolve_workspace(self.registry, "gamma", None)
        payload = raised.exception.payload()
        self.assertEqual(payload["requested_domain"], "gamma")
        self.assertEqual(
            [item["domain_id"] for item in payload["domains"]], ["alpha", "beta"]
        )

    def test_generation_and_weekly_schedule_make_no_changes_while_domain_is_uncertain(self) -> None:
        self.write_registry(
            [
                ("alpha", "Alpha Research", self.alpha),
                ("beta", "Beta Research", self.beta),
            ],
            "beta",
        )
        generated_output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                ["generate_brief.py", "run", "--registry", str(self.registry)],
            ),
            patch.object(generate_brief, "enforce_business_license"),
            contextlib.redirect_stdout(generated_output),
        ):
            self.assertEqual(generate_brief.main(), 1)
        self.assertEqual(
            json.loads(generated_output.getvalue())["status"],
            "domain_selection_required",
        )

        schedule_output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "configure_schedule.py",
                    "weekly",
                    "--registry",
                    str(self.registry),
                    "--weekday",
                    "2",
                    "--time",
                    "08:15",
                ],
            ),
            patch.object(configure_schedule, "enforce_business_license"),
            contextlib.redirect_stdout(schedule_output),
        ):
            self.assertEqual(configure_schedule.main(), 1)
        self.assertEqual(
            json.loads(schedule_output.getvalue())["status"],
            "domain_selection_required",
        )
        self.assertFalse((self.alpha / "continuous").exists())
        self.assertFalse((self.beta / "continuous").exists())

    def test_weekly_schedule_binds_the_explicit_domain_and_workspace(self) -> None:
        self.write_registry(
            [
                ("alpha", "Alpha Research", self.alpha),
                ("beta", "Beta Research", self.beta),
            ],
            "alpha",
        )
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "configure_schedule.py",
                    "weekly",
                    "--registry",
                    str(self.registry),
                    "--domain",
                    "beta",
                    "--weekday",
                    "2",
                    "--time",
                    "08:15",
                ],
            ),
            patch.object(configure_schedule, "enforce_business_license"),
            patch("configure_schedule.validate_completed_workspace"),
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(configure_schedule.main(), 0)
        payload = json.loads(output.getvalue())
        handoff = payload["automation_handoff"]
        self.assertEqual(handoff["domain_id"], "beta")
        self.assertEqual(handoff["workspace"], str(self.beta))
        self.assertEqual(handoff["automation"]["weekday"], 2)
        self.assertEqual(handoff["automation"]["time"], "08:15")
        self.assertFalse((self.alpha / "continuous").exists())
        self.assertTrue((self.beta / "continuous" / "schedule.json").is_file())


if __name__ == "__main__":
    unittest.main()
