from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from researchramp_license import (  # noqa: E402
    ActivationClient,
    LicenseError,
    LicenseVerifier,
    derive_device_id,
)
import researchramp_license  # noqa: E402


FORMAT = "researchramp-license-envelope-v1"
KEY_ID = "online-development-2026-01"
SIGNING_DOMAIN = b"researchramp/license/v1\0"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _signed_license(
    private_key: Ed25519PrivateKey,
    *,
    device_id: str,
) -> dict[str, object]:
    payload = {
        "license_id": "RR-ONLINE-DEV-000001",
        "product": "researchramp-development",
        "license_type": "perpetual",
        "licensed_to": "online-stage-two@example.test",
        "major_version": 1,
        "revision": 1,
        "device_ids": [device_id],
        "issued_at": date(2026, 9, 1).isoformat(),
    }
    payload_bytes = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_message = SIGNING_DOMAIN + KEY_ID.encode("ascii") + b"\0" + payload_bytes
    return {
        "format": FORMAT,
        "key_id": KEY_ID,
        "payload": _b64url(payload_bytes),
        "signature": _b64url(private_key.sign(signed_message)),
    }


class _OneRequestActivationServer:
    def __init__(self, response_body: dict[str, object]) -> None:
        self.requests: list[dict[str, object]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(json.loads(self.rfile.read(length)))
                encoded = json.dumps(response_body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def __enter__(self) -> "_OneRequestActivationServer":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class OnlineActivationClientTests(unittest.TestCase):
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

    def test_default_activation_service_is_the_cloudflare_development_worker(self) -> None:
        self.assertEqual(
            researchramp_license.DEFAULT_DEVELOPMENT_ACTIVATION_SERVER,
            "https://license-dev.areaday.app",
        )

    def test_activation_installs_a_server_signed_license_that_remains_offline(self) -> None:
        response_body = {
            "status": "activated",
            "license": _signed_license(self.private_key, device_id=self.device_id),
            "device_slots": {"used": 1, "maximum": 3},
        }

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "license.rrlicense"
            with _OneRequestActivationServer(response_body) as server:
                receipt = ActivationClient(server.endpoint).activate_and_install(
                    activation_key="RR1-STAGE-TWO-VALID-ACTIVATION-KEY",
                    device_id=self.device_id,
                    platform="macos",
                    major_version=1,
                    verifier=self.verifier,
                    destination=destination,
                )
                captured_requests = list(server.requests)

            offline_status = self.verifier.status(
                destination,
                current_device_id=self.device_id,
            )

        self.assertEqual(receipt.license.license_id, "RR-ONLINE-DEV-000001")
        self.assertEqual(receipt.slots_used, 1)
        self.assertEqual(receipt.slots_maximum, 3)
        self.assertEqual(offline_status, receipt.license)
        self.assertEqual(
            captured_requests,
            [
                {
                    "activation_key": "RR1-STAGE-TWO-VALID-ACTIVATION-KEY",
                    "device_id": self.device_id,
                    "platform": "macos",
                    "major_version": 1,
                }
            ],
        )

    def test_invalid_server_license_never_replaces_an_existing_license(self) -> None:
        valid_envelope = _signed_license(self.private_key, device_id=self.device_id)
        invalid_envelope = dict(valid_envelope)
        invalid_envelope["signature"] = _b64url(b"x" * 64)
        response_body = {
            "status": "activated",
            "license": invalid_envelope,
            "device_slots": {"used": 1, "maximum": 3},
        }

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "license.rrlicense"
            original = (json.dumps(valid_envelope, sort_keys=True) + "\n").encode()
            destination.write_bytes(original)

            with _OneRequestActivationServer(response_body) as server:
                with self.assertRaises(LicenseError) as captured:
                    ActivationClient(server.endpoint).activate_and_install(
                        activation_key="RR1-STAGE-TWO-VALID-ACTIVATION-KEY",
                        device_id=self.device_id,
                        platform="macos",
                        major_version=1,
                        verifier=self.verifier,
                        destination=destination,
                    )

            self.assertEqual(captured.exception.code, "license_signature_invalid")
            self.assertEqual(destination.read_bytes(), original)

    def test_activate_command_installs_the_license_and_reports_device_slots(self) -> None:
        response_body = {
            "status": "activated",
            "license": _signed_license(self.private_key, device_id=self.device_id),
            "device_slots": {"used": 1, "maximum": 3},
        }

        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "license.rrlicense"
            with _OneRequestActivationServer(response_body) as server:
                output = StringIO()
                with (
                    patch.object(
                        sys,
                        "argv",
                        [
                            "researchramp_license.py",
                            "activate",
                            "RR1-STAGE-TWO-VALID-ACTIVATION-KEY",
                            "--server",
                            server.endpoint,
                        ],
                    ),
                    patch.object(
                        researchramp_license,
                        "current_device_id",
                        return_value=self.device_id,
                    ),
                    patch.object(
                        researchramp_license,
                        "development_license_path",
                        return_value=destination,
                    ),
                    patch.object(
                        researchramp_license,
                        "development_verifier",
                        return_value=self.verifier,
                    ),
                    redirect_stdout(output),
                ):
                    return_code = researchramp_license.main()

            result = json.loads(output.getvalue())
            offline_status = self.verifier.status(
                destination,
                current_device_id=self.device_id,
            )

        self.assertEqual(return_code, 0)
        self.assertEqual(result["status"], "license_activated")
        self.assertEqual(result["device_slots"], {"used": 1, "maximum": 3})
        self.assertEqual(offline_status.license_id, "RR-ONLINE-DEV-000001")


if __name__ == "__main__":
    unittest.main()
