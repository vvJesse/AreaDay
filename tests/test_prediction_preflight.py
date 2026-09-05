from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from prediction_preflight import prediction_preflight_status  # noqa: E402
from remote_calibration import CalibrationServiceError  # noqa: E402
from researchramp_license import LicenseError  # noqa: E402


class FakeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    def request(self, action: str, payload: dict) -> dict:
        self.calls.append((action, payload))
        if self.error is not None:
            raise self.error
        return {"status": "calibration_authorized"}


class PredictionPreflightTests(unittest.TestCase):
    def test_missing_local_license_stops_before_contacting_the_server(self) -> None:
        verifier = Mock()
        verifier.status.side_effect = LicenseError(
            "license_missing", "No AreaDay license is installed."
        )
        client = FakeClient()

        result = prediction_preflight_status(
            verifier=verifier,
            license_path=Path("/missing/license.rrlicense"),
            device_id="RRD1-MAC-test",
            client=client,
        )

        self.assertEqual(result["status"], "license_required")
        self.assertEqual(result["code"], "license_missing")
        self.assertEqual(client.calls, [])

    def test_ready_preflight_sends_only_the_installed_license_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            license_path = Path(temporary) / "license.rrlicense"
            envelope = {
                "format": "researchramp-license-envelope-v1",
                "key_id": "test",
                "payload": "payload",
                "signature": "signature",
            }
            license_path.write_text(json.dumps(envelope), encoding="utf-8")
            verifier = Mock()
            client = FakeClient()

            result = prediction_preflight_status(
                verifier=verifier,
                license_path=license_path,
                device_id="RRD1-MAC-test",
                client=client,
            )

        self.assertEqual(result, {"status": "prediction_ready"})
        self.assertEqual(client.calls, [("preflight", {"license": envelope})])

    def test_service_outage_is_not_a_license_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            license_path = Path(temporary) / "license.rrlicense"
            license_path.write_text(json.dumps({"format": "test"}), encoding="utf-8")
            verifier = Mock()
            client = FakeClient(
                error=CalibrationServiceError(
                    "calibration_service_unavailable",
                    "The vocabulary prediction service could not be reached.",
                )
            )

            result = prediction_preflight_status(
                verifier=verifier,
                license_path=license_path,
                device_id="RRD1-MAC-test",
                client=client,
            )

        self.assertEqual(result["status"], "prediction_service_unavailable")
        self.assertEqual(result["code"], "calibration_service_unavailable")
        self.assertNotIn("license_required", result["status"])


class SkillLicenseRoutingTests(unittest.TestCase):
    def test_normal_initialization_uses_only_the_offline_license_check(self) -> None:
        skill = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        workflow = (ROOT / "references" / "full-workflow.md").read_text(
            encoding="utf-8"
        )
        license_reference = (
            ROOT / "references" / "license-activation.md"
        ).read_text(encoding="utf-8")

        self.assertIn("scripts/researchramp_license.py status", skill)
        self.assertNotIn("scripts/prediction_preflight.py", skill)
        self.assertNotIn("prediction_preflight.py", workflow)
        self.assertNotIn("scripts/prediction_preflight.py", license_reference)


if __name__ == "__main__":
    unittest.main()
