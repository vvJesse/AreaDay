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
        self.assertIn("--runtime-only", script)
        self.assertIn('AreaDay-runtime-$PLATFORM_ID-*.zip', script)
        self.assertIn("ditto -x -k", script)
        self.assertIn("failed verification after installation", script)
        self.assertIn("base-python/bin/python3.12", script)
        self.assertIn("com.apple.quarantine", script)
        self.assertIn("prepare_portable_runtime.py", script)

    def test_windows_x64_installer_uses_windows_runtime_and_data_migration(self) -> None:
        script = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
        self.assertIn('Join-Path $LocalUvDir "uv.exe"', script)
        self.assertIn('Join-Path $VenvDir "Scripts\\python.exe"', script)
        self.assertIn("$MigrationScript", script)
        self.assertIn("AreaDay data migration did not complete", script)
        self.assertIn('AreaDay-runtime-windows-x64-*.zip', script)
        self.assertIn("Assert-BundledRuntime", script)
        self.assertIn("Get-FileHash", script)
        self.assertIn("tar.exe", script)
        self.assertIn("Select-Object -First 1", script)
        self.assertIn("Assert-WindowsRuntimePath", script)
        self.assertIn("[IO.Path]::GetTempPath()", script)
        self.assertIn('"runtime-only"', script)
        self.assertIn("$BackupVenv", script)
        self.assertIn("$VenvReplacementStarted", script)
        self.assertIn("shutil.rmtree", script)
        self.assertIn("base-python\\python.exe", script)
        self.assertIn("prepare_portable_runtime.py", script)
        self.assertIn("-Anonymous", script)
        self.assertNotIn("WaitForExit", script)
        self.assertNotIn("$OpenAlexSetupProcess", script)
        self.assertNotIn("/usr/", script)

    def test_windows_openalex_setup_hides_key_input_and_writes_ascii_configuration(self) -> None:
        script = (ROOT / "scripts" / "configure_openalex.ps1").read_text(encoding="utf-8")
        self.assertIn("Read-Host -Prompt \"OpenAlex API key\" -AsSecureString", script)
        self.assertIn('$Content = "[openalex]`napi_key = $ApiKey`n"', script)
        self.assertIn("[switch]$Reconfigure", script)
        self.assertIn("[switch]$Anonymous", script)
        self.assertIn('Save-Configuration "anonymous"', script)
        self.assertNotIn("Start-Process notepad.exe", script)
        self.assertNotIn("Start-Sleep -Milliseconds 500", script)


if __name__ == "__main__":
    unittest.main()
