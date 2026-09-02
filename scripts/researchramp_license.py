#!/usr/bin/env python3
"""Manage the local ResearchRamp development license."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import http.client
import json
import os
import re
import subprocess
import sys
import uuid
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


LICENSE_FORMAT = "researchramp-license-envelope-v1"
SIGNING_DOMAIN = b"researchramp/license/v1\0"
DEVELOPMENT_PRODUCT = "researchramp-development"
DEVELOPMENT_MAJOR_VERSION = 1
DEVELOPMENT_APP_NAME = "ResearchRamp Development"
MAX_LICENSE_BYTES = 65_536
MAX_PAYLOAD_BYTES = 16_384
DEVICE_ID_PATTERN = re.compile(r"^RRD1-(?:MAC|WIN)-[A-Z2-7]{52}$")
KEY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BUSINESS_OPERATIONS = frozenset(
    {"initialization", "workbench", "brief_generation", "scheduling"}
)

# The development public key is safe to distribute.  Its private half lives
# outside the ResearchRamp repository and is never included in the Skill.
DEVELOPMENT_PUBLIC_KEYS: dict[str, bytes] = {
    "development-2026-01": base64.urlsafe_b64decode(
        "nSZ1UEhcGwNJM9VUsXYdEGcx7G6CpfMTRFi0dcEK7XU="
    ),
    "online-development-2026-01": base64.urlsafe_b64decode(
        "nSZ1UEhcGwNJM9VUsXYdEGcx7G6CpfMTRFi0dcEK7XU="
    ),
    "cloudflare-development-2026-01": base64.urlsafe_b64decode(
        "SeI4CqiPDr2JXFv6OVCaTe0XaJNYbnMIUBK--Cgc5lg="
    ),
}
DEFAULT_DEVELOPMENT_ACTIVATION_SERVER = (
    "https://license-dev.areaday.app"
)
MAX_ACTIVATION_RESPONSE_BYTES = 131_072


class LicenseError(RuntimeError):
    """A stable, user-visible licensing failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class LicenseInfo:
    license_id: str
    licensed_to: str
    product: str
    major_version: int
    revision: int
    device_ids: tuple[str, ...]
    issued_at: str
    key_id: str

    def public_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["device_ids"] = list(self.device_ids)
        return payload


@dataclass(frozen=True)
class ActivationReceipt:
    license: LicenseInfo
    slots_used: int
    slots_maximum: int


def _platform_name(value: str) -> tuple[str, str]:
    normalized = value.strip().lower()
    if normalized in {"darwin", "mac", "macos"}:
        return "macos", "MAC"
    if normalized in {"win32", "windows", "win"}:
        return "windows", "WIN"
    raise LicenseError(
        "device_platform_unsupported",
        "ResearchRamp development licensing supports macOS and Windows only.",
    )


def derive_device_id(platform_name: str, raw_system_id: str) -> str:
    """Derive a product-scoped device code without exposing the raw system UUID."""

    platform, label = _platform_name(platform_name)
    try:
        normalized_uuid = str(uuid.UUID(raw_system_id.strip().strip("{}")))
    except (AttributeError, ValueError) as error:
        raise LicenseError(
            "device_identity_invalid",
            "The operating system did not provide a valid device UUID.",
        ) from error
    parsed_uuid = uuid.UUID(normalized_uuid)
    if parsed_uuid.int in {0, (1 << 128) - 1}:
        raise LicenseError(
            "device_identity_invalid",
            "The operating system did not provide a usable device UUID.",
        )
    digest = hashlib.sha256(
        b"researchramp/device/v1\0"
        + platform.encode("ascii")
        + b"\0"
        + normalized_uuid.encode("ascii")
    ).digest()
    encoded = base64.b32encode(digest).decode("ascii").rstrip("=")
    return f"RRD1-{label}-{encoded}"


def _command_output(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise LicenseError(
            "device_identity_unavailable",
            "ResearchRamp could not read this computer's device identity.",
        ) from error
    if completed.returncode != 0:
        raise LicenseError(
            "device_identity_unavailable",
            "ResearchRamp could not read this computer's device identity.",
        )
    return completed.stdout


def _macos_system_uuid() -> str:
    output = _command_output(
        ["/usr/sbin/ioreg", "-rd1", "-c", "IOPlatformExpertDevice"]
    )
    match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', output)
    if match is None:
        raise LicenseError(
            "device_identity_unavailable",
            "macOS did not provide an IOPlatformUUID.",
        )
    return match.group(1)


def _windows_system_uuid() -> str:
    output = _command_output(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID",
        ]
    )
    values = [line.strip() for line in output.splitlines() if line.strip()]
    if len(values) != 1:
        raise LicenseError(
            "device_identity_unavailable",
            "Windows did not provide one SMBIOS system UUID.",
        )
    return values[0]


def current_device_id(platform_name: str | None = None) -> str:
    selected, _ = _platform_name(platform_name or sys.platform)
    raw_system_id = (
        _macos_system_uuid() if selected == "macos" else _windows_system_uuid()
    )
    return derive_device_id(selected, raw_system_id)


def development_license_path(platform_name: str | None = None) -> Path:
    selected, _ = _platform_name(platform_name or sys.platform)
    if selected == "macos":
        root = Path.home() / "Library" / "Application Support"
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data:
            raise LicenseError(
                "license_store_unavailable",
                "Windows did not provide its local application-data directory.",
            )
        root = Path(local_app_data)
    return root / DEVELOPMENT_APP_NAME / "license.rrlicense"


def _object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LicenseError(
                "license_format_invalid",
                f"The license contains a duplicate JSON field: {key}.",
            )
        result[key] = value
    return result


def _json_object(payload: bytes, *, description: str) -> dict[str, Any]:
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_object_without_duplicates,
        )
    except LicenseError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise LicenseError(
            "license_format_invalid",
            f"The {description} is not valid UTF-8 JSON.",
        ) from error
    if not isinstance(value, dict):
        raise LicenseError(
            "license_format_invalid",
            f"The {description} must be a JSON object.",
        )
    return value


def _base64url_decode(value: Any, *, field: str, maximum: int) -> bytes:
    if not isinstance(value, str) or not value or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None:
        raise LicenseError(
            "license_format_invalid",
            f"The license {field} field is not valid base64url text.",
        )
    try:
        decoded = base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (binascii.Error, ValueError) as error:
        raise LicenseError(
            "license_format_invalid",
            f"The license {field} field is not valid base64url text.",
        ) from error
    if len(decoded) > maximum:
        raise LicenseError(
            "license_format_invalid",
            f"The license {field} field is too large.",
        )
    return decoded


def _required_text(payload: dict[str, Any], field: str, maximum: int) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise LicenseError(
            "license_format_invalid",
            f"The license {field} field is invalid.",
        )
    return value.strip()


class LicenseVerifier:
    def __init__(
        self,
        *,
        public_keys: dict[str, bytes],
        product: str,
        major_version: int,
    ) -> None:
        self.public_keys = dict(public_keys)
        self.product = product
        self.major_version = major_version

    def verify_bytes(
        self,
        encoded_license: bytes,
        *,
        current_device_id: str,
    ) -> LicenseInfo:
        if not isinstance(encoded_license, bytes) or len(encoded_license) > MAX_LICENSE_BYTES:
            raise LicenseError(
                "license_format_invalid",
                "The license file is missing or too large.",
            )
        envelope = _json_object(encoded_license, description="license file")
        expected_envelope_fields = {"format", "key_id", "payload", "signature"}
        if set(envelope) != expected_envelope_fields:
            raise LicenseError(
                "license_format_invalid",
                "The license file has unexpected or missing fields.",
            )
        if envelope["format"] != LICENSE_FORMAT:
            raise LicenseError(
                "license_format_invalid",
                "The license file format is not supported.",
            )
        key_id = envelope["key_id"]
        if not isinstance(key_id, str) or KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise LicenseError(
                "license_format_invalid",
                "The license signing-key identifier is invalid.",
            )
        public_key_bytes = self.public_keys.get(key_id)
        if public_key_bytes is None:
            raise LicenseError(
                "license_key_unknown",
                "The license was signed by an unknown key.",
            )
        payload_bytes = _base64url_decode(
            envelope["payload"], field="payload", maximum=MAX_PAYLOAD_BYTES
        )
        signature = _base64url_decode(
            envelope["signature"], field="signature", maximum=64
        )
        if len(signature) != 64 or len(public_key_bytes) != 32:
            raise LicenseError(
                "license_signature_invalid",
                "The license signature is invalid.",
            )
        signed_message = (
            SIGNING_DOMAIN + key_id.encode("ascii") + b"\0" + payload_bytes
        )
        try:
            Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
                signature,
                signed_message,
            )
        except (InvalidSignature, ValueError) as error:
            raise LicenseError(
                "license_signature_invalid",
                "The license signature is invalid or the file was modified.",
            ) from error

        payload = _json_object(payload_bytes, description="signed license payload")
        expected_payload_fields = {
            "license_id",
            "product",
            "license_type",
            "licensed_to",
            "major_version",
            "revision",
            "device_ids",
            "issued_at",
        }
        if set(payload) != expected_payload_fields:
            raise LicenseError(
                "license_format_invalid",
                "The signed license payload has unexpected or missing fields.",
            )

        license_id = _required_text(payload, "license_id", 128)
        product = _required_text(payload, "product", 128)
        license_type = _required_text(payload, "license_type", 32)
        licensed_to = _required_text(payload, "licensed_to", 320)
        issued_at = _required_text(payload, "issued_at", 32)
        try:
            date.fromisoformat(issued_at)
        except ValueError as error:
            raise LicenseError(
                "license_format_invalid",
                "The license issued_at field is invalid.",
            ) from error
        major_version = payload.get("major_version")
        revision = payload.get("revision")
        if type(major_version) is not int or major_version < 1:
            raise LicenseError(
                "license_format_invalid",
                "The license major_version field is invalid.",
            )
        if type(revision) is not int or revision < 1:
            raise LicenseError(
                "license_format_invalid",
                "The license revision field is invalid.",
            )
        device_ids_value = payload.get("device_ids")
        if not isinstance(device_ids_value, list) or not device_ids_value:
            raise LicenseError(
                "license_format_invalid",
                "The license must contain at least one device.",
            )
        if len(device_ids_value) > 3:
            raise LicenseError(
                "license_device_limit",
                "A ResearchRamp license cannot contain more than three devices.",
            )
        if any(
            not isinstance(item, str) or DEVICE_ID_PATTERN.fullmatch(item) is None
            for item in device_ids_value
        ):
            raise LicenseError(
                "license_format_invalid",
                "The license contains an invalid device code.",
            )
        if len(set(device_ids_value)) != len(device_ids_value):
            raise LicenseError(
                "license_format_invalid",
                "The license contains duplicate device codes.",
            )
        if license_type != "perpetual":
            raise LicenseError(
                "license_type_invalid",
                "The ResearchRamp license type is not supported.",
            )
        if product != self.product:
            raise LicenseError(
                "license_product_mismatch",
                "The license belongs to another product.",
            )
        if major_version != self.major_version:
            raise LicenseError(
                "license_version_mismatch",
                "The license does not authorize this ResearchRamp major version.",
            )
        if current_device_id not in device_ids_value:
            message = "This computer is not registered in the license."
            if len(device_ids_value) == 3:
                message += " The license already contains three devices."
            raise LicenseError("license_device_mismatch", message)

        return LicenseInfo(
            license_id=license_id,
            licensed_to=licensed_to,
            product=product,
            major_version=major_version,
            revision=revision,
            device_ids=tuple(device_ids_value),
            issued_at=issued_at,
            key_id=key_id,
        )

    def verify_file(
        self,
        path: Path,
        *,
        current_device_id: str,
    ) -> LicenseInfo:
        try:
            encoded = path.read_bytes()
        except FileNotFoundError as error:
            raise LicenseError(
                "license_missing",
                "No ResearchRamp license is installed.",
            ) from error
        except OSError as error:
            raise LicenseError(
                "license_unreadable",
                "ResearchRamp could not read the license file.",
            ) from error
        return self.verify_bytes(encoded, current_device_id=current_device_id)

    def install(
        self,
        source: Path,
        *,
        destination: Path,
        current_device_id: str,
    ) -> LicenseInfo:
        try:
            encoded = source.read_bytes()
        except FileNotFoundError as error:
            raise LicenseError(
                "license_source_missing",
                "The selected ResearchRamp license file does not exist.",
            ) from error
        except OSError as error:
            raise LicenseError(
                "license_unreadable",
                "ResearchRamp could not read the selected license file.",
            ) from error
        return self.install_bytes(
            encoded,
            destination=destination,
            current_device_id=current_device_id,
        )

    def install_bytes(
        self,
        encoded: bytes,
        *,
        destination: Path,
        current_device_id: str,
    ) -> LicenseInfo:
        info = self.verify_bytes(encoded, current_device_id=current_device_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temporary.open("xb") as handle:
                if os.name != "nt":
                    os.chmod(temporary, 0o600)
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        except OSError as error:
            raise LicenseError(
                "license_install_failed",
                "ResearchRamp could not install the validated license.",
            ) from error
        finally:
            temporary.unlink(missing_ok=True)
        return info

    def status(
        self,
        path: Path,
        *,
        current_device_id: str,
    ) -> LicenseInfo:
        return self.verify_file(path, current_device_id=current_device_id)


class ActivationClient:
    def __init__(self, endpoint: str, *, timeout: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(endpoint.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise LicenseError(
                "activation_server_invalid",
                "The ResearchRamp activation-server address is invalid.",
            )
        self.endpoint = endpoint.rstrip("/")
        self.parsed_endpoint = parsed
        self.timeout = timeout

    def activate_and_install(
        self,
        *,
        activation_key: str,
        device_id: str,
        platform: str,
        major_version: int,
        verifier: LicenseVerifier,
        destination: Path,
    ) -> ActivationReceipt:
        if (
            not isinstance(activation_key, str)
            or not activation_key.strip()
            or len(activation_key) > 256
        ):
            raise LicenseError(
                "activation_key_invalid",
                "Enter a valid ResearchRamp activation key.",
            )
        request_body = json.dumps(
            {
                "activation_key": activation_key.strip(),
                "device_id": device_id,
                "platform": platform,
                "major_version": major_version,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection_type = (
            http.client.HTTPSConnection
            if self.parsed_endpoint.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_type(
            self.parsed_endpoint.hostname,
            self.parsed_endpoint.port,
            timeout=self.timeout,
        )
        try:
            connection.request(
                "POST",
                "/v1/activate",
                body=request_body,
                headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            encoded_response = response.read(MAX_ACTIVATION_RESPONSE_BYTES + 1)
            if response.status != 200:
                self._raise_server_error(encoded_response)
                raise AssertionError("unreachable")
        except LicenseError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as error:
            raise LicenseError(
                "activation_service_unavailable",
                "ResearchRamp could not reach the activation service.",
            ) from error
        finally:
            connection.close()
        if len(encoded_response) > MAX_ACTIVATION_RESPONSE_BYTES:
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned an invalid response.",
            )
        response_body = _json_object(
            encoded_response,
            description="activation response",
        )
        if set(response_body) != {"status", "license", "device_slots"}:
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned an invalid response.",
            )
        if response_body["status"] != "activated":
            raise LicenseError(
                "activation_response_invalid",
                "The activation service did not activate ResearchRamp.",
            )
        envelope = response_body["license"]
        slots = response_body["device_slots"]
        if not isinstance(envelope, dict) or not isinstance(slots, dict):
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned an invalid response.",
            )
        if set(slots) != {"used", "maximum"}:
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned invalid device information.",
            )
        used = slots["used"]
        maximum = slots["maximum"]
        if (
            type(used) is not int
            or type(maximum) is not int
            or not 1 <= used <= maximum <= 3
        ):
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned invalid device information.",
            )
        encoded_license = (
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        info = verifier.install_bytes(
            encoded_license,
            destination=destination,
            current_device_id=device_id,
        )
        return ActivationReceipt(
            license=info,
            slots_used=used,
            slots_maximum=maximum,
        )

    @staticmethod
    def _raise_server_error(encoded: bytes) -> None:
        try:
            body = _json_object(encoded, description="activation error")
            if set(body) != {"status", "code", "error"}:
                raise LicenseError("activation_response_invalid", "")
            code = body["code"]
            message = body["error"]
            if (
                body["status"] != "activation_error"
                or not isinstance(code, str)
                or not code.startswith("activation_")
                or not isinstance(message, str)
                or not message.strip()
            ):
                raise LicenseError("activation_response_invalid", "")
        except LicenseError as error:
            if error.code != "activation_response_invalid":
                raise
            raise LicenseError(
                "activation_response_invalid",
                "The activation service returned an invalid error response.",
            ) from error
        raise LicenseError(code, message.strip())


def development_verifier() -> LicenseVerifier:
    return LicenseVerifier(
        public_keys=DEVELOPMENT_PUBLIC_KEYS,
        product=DEVELOPMENT_PRODUCT,
        major_version=DEVELOPMENT_MAJOR_VERSION,
    )


def _output(status: str, **values: Any) -> None:
    print(json.dumps({"status": status, **values}, ensure_ascii=False, indent=2))


def require_business_license(
    operation: str,
    *,
    verifier: LicenseVerifier | None = None,
    license_path: Path | None = None,
    device_id: str | None = None,
) -> LicenseInfo:
    """Verify one public business operation using only the installed license."""

    if operation not in BUSINESS_OPERATIONS:
        raise ValueError(f"Unknown ResearchRamp business operation: {operation}")
    selected_verifier = verifier or development_verifier()
    selected_path = license_path or development_license_path()
    selected_device = device_id or current_device_id()
    return selected_verifier.status(
        selected_path,
        current_device_id=selected_device,
    )


def enforce_business_license(
    operation: str,
    *,
    verifier: LicenseVerifier | None = None,
    license_path: Path | None = None,
    device_id: str | None = None,
) -> LicenseInfo:
    """Stop a public business entrypoint before side effects when unlicensed."""

    try:
        return require_business_license(
            operation,
            verifier=verifier,
            license_path=license_path,
            device_id=device_id,
        )
    except LicenseError as error:
        _output(
            "license_required",
            operation=operation,
            code=error.code,
            error=str(error),
        )
        raise SystemExit(3) from None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("device-id", help="Show this computer's device code.")
    activate = subparsers.add_parser(
        "activate",
        help="Activate and automatically install a development license.",
    )
    activate.add_argument("activation_key")
    activate.add_argument(
        "--server",
        default=DEFAULT_DEVELOPMENT_ACTIVATION_SERVER,
        help="Development activation-server address.",
    )
    install = subparsers.add_parser("install", help="Validate and install a license file.")
    install.add_argument("license_file", type=Path)
    subparsers.add_parser("status", help="Show the installed license status.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        device_id = current_device_id()
        if args.command == "device-id":
            _output(
                "device_ready",
                channel="development",
                device_id=device_id,
            )
            return 0
        verifier = development_verifier()
        license_path = development_license_path()
        if args.command == "activate":
            platform, _ = _platform_name(sys.platform)
            receipt = ActivationClient(args.server).activate_and_install(
                activation_key=args.activation_key,
                device_id=device_id,
                platform=platform,
                major_version=DEVELOPMENT_MAJOR_VERSION,
                verifier=verifier,
                destination=license_path,
            )
            _output(
                "license_activated",
                channel="development",
                license_path=str(license_path),
                license=receipt.license.public_payload(),
                device_slots={
                    "used": receipt.slots_used,
                    "maximum": receipt.slots_maximum,
                },
            )
            return 0
        if args.command == "install":
            info = verifier.install(
                args.license_file.expanduser().resolve(),
                destination=license_path,
                current_device_id=device_id,
            )
            _output(
                "license_installed",
                channel="development",
                license_path=str(license_path),
                license=info.public_payload(),
            )
            return 0
        info = verifier.status(license_path, current_device_id=device_id)
        _output(
            "license_valid",
            channel="development",
            license_path=str(license_path),
            license=info.public_payload(),
        )
        return 0
    except LicenseError as error:
        _output("license_error", code=error.code, error=str(error))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
