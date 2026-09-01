from __future__ import annotations

import base64
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from researchramp_license import (  # noqa: E402
    LicenseError,
    LicenseVerifier,
    derive_device_id,
)


FORMAT = "researchramp-license-envelope-v1"
KEY_ID = "development-2026-01"
SIGNING_DOMAIN = b"researchramp/license/v1\0"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _payload(
    device_ids: list[str],
    *,
    product: str = "researchramp-development",
    major_version: int = 1,
) -> dict[str, object]:
    return {
        "license_id": "RR-DEV-000001",
        "product": product,
        "license_type": "perpetual",
        "licensed_to": "stage-one@example.test",
        "major_version": major_version,
        "revision": 1,
        "device_ids": device_ids,
        "issued_at": date(2026, 9, 1).isoformat(),
    }


def _signed_license(
    private_key: Ed25519PrivateKey,
    payload: dict[str, object],
) -> bytes:
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_message = SIGNING_DOMAIN + KEY_ID.encode("ascii") + b"\0" + payload_bytes
    envelope = {
        "format": FORMAT,
        "key_id": KEY_ID,
        "payload": _b64url(payload_bytes),
        "signature": _b64url(private_key.sign(signed_message)),
    }
    return (json.dumps(envelope, sort_keys=True) + "\n").encode("utf-8")


class LicenseActivationTests(unittest.TestCase):
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

    def test_device_id_is_stable_normalized_and_machine_bound(self) -> None:
        same = derive_device_id(
            "macos",
            "{01234567-89AB-CDEF-0123-456789ABCDEF}",
        )
        other = derive_device_id(
            "macos",
            "11234567-89ab-cdef-0123-456789abcdef",
        )
        windows = derive_device_id(
            "windows",
            "01234567-89ab-cdef-0123-456789abcdef",
        )

        self.assertEqual(same, self.device_id)
        self.assertNotEqual(other, self.device_id)
        self.assertNotEqual(windows, self.device_id)
        self.assertRegex(self.device_id, r"^RRD1-MAC-[A-Z2-7]{52}$")

    def test_valid_perpetual_license_verifies_for_current_device(self) -> None:
        result = self.verifier.verify_bytes(
            _signed_license(self.private_key, _payload([self.device_id])),
            current_device_id=self.device_id,
        )

        self.assertEqual(result.license_id, "RR-DEV-000001")
        self.assertEqual(result.licensed_to, "stage-one@example.test")
        self.assertEqual(result.revision, 1)
        self.assertEqual(result.device_ids, (self.device_id,))

    def test_tampering_with_signed_payload_is_rejected(self) -> None:
        encoded = _signed_license(self.private_key, _payload([self.device_id]))
        envelope = json.loads(encoded)
        payload = json.loads(
            base64.urlsafe_b64decode(envelope["payload"] + "==")
        )
        payload["licensed_to"] = "attacker@example.test"
        envelope["payload"] = _b64url(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )

        with self.assertRaisesRegex(LicenseError, "signature") as captured:
            self.verifier.verify_bytes(
                json.dumps(envelope).encode(),
                current_device_id=self.device_id,
            )
        self.assertEqual(captured.exception.code, "license_signature_invalid")

    def test_wrong_device_product_and_version_are_distinct_failures(self) -> None:
        cases = (
            (
                _payload([derive_device_id("macos", "21234567-89ab-cdef-0123-456789abcdef")]),
                "license_device_mismatch",
            ),
            (_payload([self.device_id], product="another-product"), "license_product_mismatch"),
            (_payload([self.device_id], major_version=2), "license_version_mismatch"),
        )
        for payload, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                with self.assertRaises(LicenseError) as captured:
                    self.verifier.verify_bytes(
                        _signed_license(self.private_key, payload),
                        current_device_id=self.device_id,
                    )
                self.assertEqual(captured.exception.code, expected_code)

    def test_license_with_four_devices_is_rejected(self) -> None:
        device_ids = [
            derive_device_id("macos", f"{index:08x}-89ab-cdef-0123-456789abcdef")
            for index in range(1, 5)
        ]
        with self.assertRaises(LicenseError) as captured:
            self.verifier.verify_bytes(
                _signed_license(self.private_key, _payload(device_ids)),
                current_device_id=device_ids[0],
            )
        self.assertEqual(captured.exception.code, "license_device_limit")

    def test_invalid_install_never_overwrites_an_existing_valid_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "license.rrlicense"
            source = root / "incoming.rrlicense"
            original = _signed_license(self.private_key, _payload([self.device_id]))
            installed.write_bytes(original)
            source.write_text('{"format":"broken"}\n', encoding="utf-8")

            with self.assertRaises(LicenseError):
                self.verifier.install(
                    source,
                    destination=installed,
                    current_device_id=self.device_id,
                )

            self.assertEqual(installed.read_bytes(), original)

    def test_valid_install_is_atomic_and_status_reads_installed_license(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            installed = root / "app-data" / "license.rrlicense"
            source = root / "incoming.rrlicense"
            source.write_bytes(
                _signed_license(self.private_key, _payload([self.device_id]))
            )

            result = self.verifier.install(
                source,
                destination=installed,
                current_device_id=self.device_id,
            )
            status = self.verifier.status(
                installed,
                current_device_id=self.device_id,
            )

            self.assertTrue(installed.is_file())
            self.assertEqual(result, status)
            self.assertEqual(status.license_id, "RR-DEV-000001")
            self.assertFalse(installed.with_suffix(".rrlicense.tmp").exists())


if __name__ == "__main__":
    unittest.main()
