from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from researchramp_license import (  # noqa: E402
    DEFAULT_PRODUCTION_ACTIVATION_SERVER,
    PRODUCTION_APP_NAME,
    PRODUCTION_MAJOR_VERSION,
    PRODUCTION_PRODUCT,
    derive_device_id,
    production_license_path,
)


class ProductionLicenseDefaultsTests(unittest.TestCase):
    def test_public_defaults_are_areaday_production(self) -> None:
        self.assertEqual(PRODUCTION_PRODUCT, "areaday")
        self.assertEqual(PRODUCTION_MAJOR_VERSION, 1)
        self.assertEqual(PRODUCTION_APP_NAME, "AreaDay")
        self.assertEqual(
            DEFAULT_PRODUCTION_ACTIVATION_SERVER,
            "https://license.areaday.app",
        )

    def test_device_code_is_branded_and_platform_isolated(self) -> None:
        raw = "01234567-89ab-cdef-0123-456789abcdef"
        mac = derive_device_id("macos", raw)
        windows = derive_device_id("windows", raw)

        self.assertRegex(mac, r"^AD1-MAC-[A-Z2-7]{52}$")
        self.assertRegex(windows, r"^AD1-WIN-[A-Z2-7]{52}$")
        self.assertNotEqual(mac, windows)

    def test_license_is_saved_in_areaday_application_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch("pathlib.Path.home", return_value=Path(temporary)):
                path = production_license_path("macos")
        self.assertEqual(
            path,
            Path(temporary) / "Library" / "Application Support" / "AreaDay" / "license.rrlicense",
        )

    def test_windows_license_uses_local_app_data(self) -> None:
        with patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Buyer\AppData\Local"}):
            path = production_license_path("windows")
        self.assertEqual(
            path,
            Path(r"C:\Users\Buyer\AppData\Local") / "AreaDay" / "license.rrlicense",
        )


if __name__ == "__main__":
    unittest.main()
