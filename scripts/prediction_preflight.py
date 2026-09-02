#!/usr/bin/env python3
"""Check personal-vocabulary prediction access before corpus collection."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from remote_calibration import CalibrationServiceError, RemoteCalibrationClient
from researchramp_license import (
    DEFAULT_PRODUCTION_ACTIVATION_SERVER,
    LicenseError,
    LicenseVerifier,
    current_device_id,
    production_license_path,
    production_verifier,
)


def prediction_preflight_status(
    *,
    verifier: LicenseVerifier | Any | None = None,
    license_path: Path | None = None,
    device_id: str | None = None,
    client: RemoteCalibrationClient | Any | None = None,
) -> dict[str, str]:
    selected_verifier = verifier or production_verifier()
    selected_path = license_path or production_license_path()
    selected_device = device_id or current_device_id()
    try:
        selected_verifier.status(
            selected_path,
            current_device_id=selected_device,
        )
        envelope = json.loads(selected_path.read_text(encoding="utf-8"))
        if not isinstance(envelope, dict):
            raise LicenseError(
                "license_format_invalid",
                "The installed AreaDay license is invalid.",
            )
    except LicenseError as error:
        return {
            "status": "license_required",
            "code": error.code,
            "error": str(error),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {
            "status": "license_required",
            "code": "license_unreadable",
            "error": "AreaDay could not read the installed license.",
        }

    selected_client = client or RemoteCalibrationClient()
    try:
        response = selected_client.request("preflight", {"license": envelope})
        if response != {"status": "calibration_authorized"}:
            raise CalibrationServiceError(
                "calibration_response_invalid",
                "The vocabulary prediction service returned an invalid response.",
            )
        return {"status": "prediction_ready"}
    except CalibrationServiceError as error:
        if error.code == "calibration_service_unavailable":
            status = "prediction_service_unavailable"
        elif "license" in error.code:
            status = "license_required"
        else:
            status = "prediction_service_error"
        return {"status": status, "code": error.code, "error": str(error)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server",
        default=DEFAULT_PRODUCTION_ACTIVATION_SERVER,
        help="AreaDay prediction-service address.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = prediction_preflight_status(
        client=RemoteCalibrationClient(args.server),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {
        "prediction_ready": 0,
        "license_required": 3,
        "prediction_service_unavailable": 4,
    }.get(result["status"], 5)


if __name__ == "__main__":
    raise SystemExit(main())
