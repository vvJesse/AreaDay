from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PlatformInstallerContractTests(unittest.TestCase):
    def test_macos_installer_runs_data_migration_after_runtime_creation(self) -> None:
        script = (ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
        self.assertIn('MIGRATION_SCRIPT="$SCRIPT_DIR/migrate_areaday_data.py"', script)
        self.assertIn('"$VENV_DIR/bin/python" "$MIGRATION_SCRIPT"', script)
        self.assertIn("UV_VERSION=\"0.12.6\"", script)

    def test_windows_x64_installer_uses_windows_runtime_and_data_migration(self) -> None:
        script = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $LocalUvDir "uv.exe"', script)
        self.assertIn('Join-Path $VenvDir "Scripts\\python.exe"', script)
        self.assertIn("$MigrationScript", script)
        self.assertIn("AreaDay data migration did not complete", script)
        self.assertNotIn("/usr/", script)


if __name__ == "__main__":
    unittest.main()
