from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import configure_schedule  # noqa: E402
import generate_brief  # noqa: E402
import initialize  # noqa: E402
import open_workbench  # noqa: E402
from researchramp_license import (  # noqa: E402
    ActivationClient,
    LicenseError,
    LicenseVerifier,
    derive_device_id,
    enforce_business_license,
    require_business_license,
)


KEY_ID = "gate-test-2026-01"
SIGNING_DOMAIN = b"researchramp/license/v1\0"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_license(
    private_key: Ed25519PrivateKey,
    *,
    device_id: str,
    signature: bytes | None = None,
) -> bytes:
    payload = {
        "license_id": "RR-GATE-TEST-000001",
        "product": "researchramp-development",
        "license_type": "perpetual",
        "licensed_to": "gate-test@example.test",
        "major_version": 1,
        "revision": 1,
        "device_ids": [device_id],
        "issued_at": date(2026, 9, 2).isoformat(),
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_message = SIGNING_DOMAIN + KEY_ID.encode("ascii") + b"\0" + payload_bytes
    envelope = {
        "format": "researchramp-license-envelope-v1",
        "key_id": KEY_ID,
        "payload": _b64url(payload_bytes),
        "signature": _b64url(signature or private_key.sign(signed_message)),
    }
    return (json.dumps(envelope, sort_keys=True) + "\n").encode("utf-8")


class LicenseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.private_key = Ed25519PrivateKey.generate()
        public_key = self.private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self.verifier = LicenseVerifier(
            public_keys={KEY_ID: public_key},
            product="researchramp-development",
            major_version=1,
        )
        self.device_id = derive_device_id(
            "macos",
            "01234567-89ab-cdef-0123-456789abcdef",
        )

    def test_valid_license_allows_business_operation_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            license_path = Path(temporary) / "license.rrlicense"
            license_path.write_bytes(
                _signed_license(self.private_key, device_id=self.device_id)
            )
            with patch.object(
                ActivationClient,
                "activate_and_install",
                side_effect=AssertionError("the offline gate used the network"),
            ):
                info = require_business_license(
                    "workbench",
                    verifier=self.verifier,
                    license_path=license_path,
                    device_id=self.device_id,
                )

        self.assertEqual(info.license_id, "RR-GATE-TEST-000001")

    def test_missing_license_stops_with_stable_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = StringIO()
            with redirect_stdout(output), self.assertRaises(SystemExit) as captured:
                enforce_business_license(
                    "initialization",
                    verifier=self.verifier,
                    license_path=Path(temporary) / "missing.rrlicense",
                    device_id=self.device_id,
                )

        self.assertEqual(captured.exception.code, 3)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "status": "license_required",
                "operation": "initialization",
                "code": "license_missing",
                "error": "No AreaDay license is installed.",
            },
        )

    def test_forged_and_other_device_licenses_are_rejected(self) -> None:
        other_device = derive_device_id(
            "macos",
            "11234567-89ab-cdef-0123-456789abcdef",
        )
        cases = (
            (
                _signed_license(
                    self.private_key,
                    device_id=self.device_id,
                    signature=b"x" * 64,
                ),
                self.device_id,
                "license_signature_invalid",
            ),
            (
                _signed_license(self.private_key, device_id=other_device),
                self.device_id,
                "license_device_mismatch",
            ),
        )
        for encoded, current_device, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with tempfile.TemporaryDirectory() as temporary:
                    license_path = Path(temporary) / "license.rrlicense"
                    license_path.write_bytes(encoded)
                    with self.assertRaises(LicenseError) as captured:
                        require_business_license(
                            "brief_generation",
                            verifier=self.verifier,
                            license_path=license_path,
                            device_id=current_device,
                        )
                self.assertEqual(captured.exception.code, expected_code)

    def test_every_public_business_entrypoint_checks_the_shared_gate_first(self) -> None:
        cases = (
            (open_workbench, ["open_workbench.py"], "workbench"),
            (
                initialize,
                [
                    "initialize.py",
                    "--profile",
                    "/tmp/profile.json",
                    "--workspace",
                    "/tmp/workspace",
                ],
                "initialization",
            ),
            (generate_brief, ["generate_brief.py"], "brief_generation"),
            (
                configure_schedule,
                ["configure_schedule.py", "daily", "--time", "09:00"],
                "scheduling",
            ),
        )

        class GateReached(RuntimeError):
            pass

        for module, argv, expected_operation in cases:
            calls: list[str] = []

            def stop_at_gate(operation: str) -> None:
                calls.append(operation)
                raise GateReached

            with self.subTest(module=module.__name__), patch.object(
                sys, "argv", argv
            ), patch.object(
                module,
                "enforce_business_license",
                side_effect=stop_at_gate,
            ):
                with self.assertRaises(GateReached):
                    module.main()
            self.assertEqual(calls, [expected_operation])


if __name__ == "__main__":
    unittest.main()
