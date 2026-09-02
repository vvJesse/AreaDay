param(
    [ValidateSet("check", "install", "bootstrap-only", "runtime-only")]
    [string]$Mode = "install"
)

$ErrorActionPreference = "Stop"
$UvVersion = "0.12.6"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptDir
$RuntimeDir = if ($env:RESEARCHRAMP_RUNTIME_DIR) { $env:RESEARCHRAMP_RUNTIME_DIR } else { Join-Path $SkillDir ".runtime" }
$VenvDir = if ($env:RESEARCHRAMP_VENV_DIR) { $env:RESEARCHRAMP_VENV_DIR } else { Join-Path $SkillDir ".venv" }
$ModelDir = if ($env:RESEARCHRAMP_MODEL_DIR) { $env:RESEARCHRAMP_MODEL_DIR } else { Join-Path $HOME ".researchramp\models\sentence-transformers" }
$SetupScript = Join-Path $ScriptDir "setup_dependencies.py"
$MigrationScript = Join-Path $ScriptDir "migrate_areaday_data.py"
$OpenAlexSetupScript = Join-Path $ScriptDir "configure_openalex.ps1"
$OpenAlexConfig = Join-Path $HOME ".researchramp\credentials.ini"
$OpenAlexSetupProcess = $null

function Get-BundledRuntime {
    $RuntimePackDir = Join-Path $SkillDir "runtime-packs"
    if (-not (Test-Path -LiteralPath $RuntimePackDir -PathType Container)) {
        return $null
    }
    $Matches = @(Get-ChildItem -LiteralPath $RuntimePackDir -File -Filter "AreaDay-runtime-windows-x64-*.zip")
    if ($Matches.Count -eq 0) {
        return $null
    }
    if ($Matches.Count -ne 1) {
        throw "Expected one bundled runtime for windows-x64, found $($Matches.Count)."
    }
    return $Matches[0]
}

function Install-BundledRuntime([System.IO.FileInfo]$RuntimeArchive) {
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $StageDir = Join-Path $RuntimeDir ("areaday-runtime-stage-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $StageDir | Out-Null
    $BackupVenv = "$VenvDir.areaday-backup-$PID"
    $HadPreviousVenv = $false
    try {
        Write-Host "Installing the bundled AreaDay runtime for windows-x64..."
        Expand-Archive -LiteralPath $RuntimeArchive.FullName -DestinationPath $StageDir
        $StagedRoot = Join-Path $StageDir "runtime"
        $StagedVenv = Join-Path $StagedRoot "venv"
        $StagedPython = Join-Path $StagedVenv "Scripts\python.exe"
        $StagedModel = Join-Path $StagedRoot "models\sentence-transformers"
        $StagedManifest = Join-Path $StagedRoot "runtime.json"
        if (-not (Test-Path -LiteralPath $StagedPython -PathType Leaf) -or
            -not (Test-Path -LiteralPath $StagedModel -PathType Container) -or
            -not (Test-Path -LiteralPath $StagedManifest -PathType Leaf)) {
            throw "The bundled runtime is incomplete."
        }
        $Manifest = Get-Content -LiteralPath $StagedManifest -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($Manifest.schema_version -ne 1 -or $Manifest.product -ne "areaday" -or $Manifest.platform -ne "windows-x64") {
            throw "The bundled runtime manifest does not match windows-x64."
        }
        & $StagedPython $SetupScript --venv-dir $StagedVenv --model-dir $StagedModel
        if ($LASTEXITCODE -ne 0) {
            throw "The bundled runtime failed verification before installation."
        }

        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $ModelDir) | Out-Null
        New-Item -ItemType Directory -Force -Path $ModelDir | Out-Null
        Copy-Item -Path (Join-Path $StagedModel "*") -Destination $ModelDir -Recurse -Force
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VenvDir) | Out-Null
        if (Test-Path -LiteralPath $BackupVenv) {
            throw "Cannot create the temporary runtime backup: $BackupVenv already exists."
        }
        if (Test-Path -LiteralPath $VenvDir) {
            Move-Item -LiteralPath $VenvDir -Destination $BackupVenv
            $HadPreviousVenv = $true
        }
        Move-Item -LiteralPath $StagedVenv -Destination $VenvDir
        $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
        & $VenvPython $SetupScript --venv-dir $VenvDir --model-dir $ModelDir
        if ($LASTEXITCODE -ne 0) {
            throw "The bundled runtime failed verification after installation."
        }
        if ($HadPreviousVenv) {
            Remove-Item -LiteralPath $BackupVenv -Recurse -Force
            $HadPreviousVenv = $false
        }
    } catch {
        if (Test-Path -LiteralPath $VenvDir) {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        if ($HadPreviousVenv -and (Test-Path -LiteralPath $BackupVenv)) {
            Move-Item -LiteralPath $BackupVenv -Destination $VenvDir
            $HadPreviousVenv = $false
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $StageDir) {
            Remove-Item -LiteralPath $StageDir -Recurse -Force
        }
    }
}

function Complete-Installation {
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    & $VenvPython $MigrationScript
    if ($LASTEXITCODE -ne 0) {
        throw "AreaDay data migration did not complete."
    }
    if ($Mode -eq "install") {
        if ($null -ne $OpenAlexSetupProcess) {
            $OpenAlexSetupProcess.WaitForExit()
            if ($OpenAlexSetupProcess.ExitCode -ne 0) {
                throw "OpenAlex setup did not complete."
            }
        }
        if (-not (Test-Path -LiteralPath $OpenAlexConfig -PathType Leaf)) {
            throw "OpenAlex setup did not create $OpenAlexConfig"
        }
    }
}

if ($Mode -eq "check") {
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Error "AreaDay runtime is not installed at $VenvDir"
        exit 1
    }
    & $VenvPython $SetupScript --venv-dir $VenvDir --model-dir $ModelDir
    exit $LASTEXITCODE
}

if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -ne "AMD64") {
    throw "This AreaDay package requires Windows x64."
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if ($Mode -eq "install") {
    $OpenAlexSetupProcess = Start-Process powershell.exe -PassThru -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $OpenAlexSetupScript + '"')
    )
}
$BundledRuntime = Get-BundledRuntime
if ($null -ne $BundledRuntime) {
    Install-BundledRuntime $BundledRuntime
    Complete-Installation
    Write-Host "AreaDay is ready. The bundled runtime was verified without downloading dependencies."
    exit 0
}
if ($Mode -eq "runtime-only") {
    throw "No bundled runtime was found for windows-x64."
}
$LocalUvDir = Join-Path $RuntimeDir "uv"
$LocalUv = Join-Path $LocalUvDir "uv.exe"

if (Test-Path -LiteralPath $LocalUv -PathType Leaf) {
    $UvBin = $LocalUv
} else {
    $InstallerUrl = if ($env:RESEARCHRAMP_UV_INSTALLER_URL) { $env:RESEARCHRAMP_UV_INSTALLER_URL } else { "https://astral.sh/uv/$UvVersion/install.ps1" }
    $UvArtifactBases = if ($env:RESEARCHRAMP_UV_DOWNLOAD_URL) { $env:RESEARCHRAMP_UV_DOWNLOAD_URL } else { "https://github.com/astral-sh/uv/releases/download/$UvVersion https://releases.astral.sh/github/uv/releases/download/$UvVersion" }
    $InstallerPath = Join-Path $RuntimeDir "uv-installer-$UvVersion.ps1"
    Write-Host "Downloading the pinned uv $UvVersion installer..."
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 120 -Uri $InstallerUrl -OutFile $InstallerPath
    $PreviousUnmanaged = $env:UV_UNMANAGED_INSTALL
    $PreviousNoModifyPath = $env:UV_NO_MODIFY_PATH
    $PreviousDisableUpdate = $env:UV_DISABLE_UPDATE
    $PreviousDownloadUrl = $env:UV_DOWNLOAD_URL
    try {
        $env:UV_UNMANAGED_INSTALL = $LocalUvDir
        $env:UV_NO_MODIFY_PATH = "1"
        $env:UV_DISABLE_UPDATE = "1"
        $env:UV_DOWNLOAD_URL = $UvArtifactBases
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $InstallerPath
        if ($LASTEXITCODE -ne 0) {
            throw "uv installer exited with code $LASTEXITCODE"
        }
    } finally {
        $env:UV_UNMANAGED_INSTALL = $PreviousUnmanaged
        $env:UV_NO_MODIFY_PATH = $PreviousNoModifyPath
        $env:UV_DISABLE_UPDATE = $PreviousDisableUpdate
        $env:UV_DOWNLOAD_URL = $PreviousDownloadUrl
    }
    if (-not (Test-Path -LiteralPath $LocalUv -PathType Leaf)) {
        throw "The uv installer completed without creating $LocalUv"
    }
    $UvBin = $LocalUv
}

& $UvBin --version
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
if ($Mode -eq "bootstrap-only") {
    exit 0
}

$UvBinDir = Split-Path -Parent $UvBin
$env:PATH = "$UvBinDir;$env:PATH"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeDir "python"
$env:UV_CACHE_DIR = Join-Path $RuntimeDir "cache"
& $UvBin run --isolated --no-project --no-config --managed-python --python 3.12 $SetupScript --install --venv-dir $VenvDir --model-dir $ModelDir
$SetupExitCode = $LASTEXITCODE
if ($SetupExitCode -eq 0) {
    Complete-Installation
    Write-Host "Installation verified; removing the disposable package-download cache..."
    & $UvBin cache clean --no-config
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Installation succeeded, but the disposable uv cache could not be cleaned."
    }
    exit 0
}
if ($null -ne $OpenAlexSetupProcess -and -not $OpenAlexSetupProcess.HasExited) {
    $OpenAlexSetupProcess.Kill()
}
exit $SetupExitCode
