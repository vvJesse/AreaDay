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
$PortableRuntimeScript = Join-Path $ScriptDir "prepare_portable_runtime.py"
$MigrationScript = Join-Path $ScriptDir "migrate_areaday_data.py"
$OpenAlexSetupScript = Join-Path $ScriptDir "configure_openalex.ps1"
$OpenAlexConfigDir = if ($env:RESEARCHRAMP_CONFIG_DIR) { $env:RESEARCHRAMP_CONFIG_DIR } else { Join-Path $HOME ".researchramp" }
$OpenAlexConfig = Join-Path $OpenAlexConfigDir "credentials.ini"

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

function Assert-BundledRuntime([System.IO.FileInfo]$RuntimeArchive) {
    $ReleasePath = Join-Path $SkillDir "release.json"
    if (-not (Test-Path -LiteralPath $ReleasePath -PathType Leaf)) {
        throw "The bundled runtime release metadata is missing: $ReleasePath"
    }
    $Release = Get-Content -LiteralPath $ReleasePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($Release.product -ne "areaday" -or
        $Release.platform -ne "windows-x64" -or
        $Release.runtime_artifact -ne $RuntimeArchive.Name -or
        $Release.runtime_sha256 -notmatch "^[0-9a-fA-F]{64}$") {
        throw "The bundled runtime release metadata does not match windows-x64."
    }
    $ActualHash = (Get-FileHash -LiteralPath $RuntimeArchive.FullName -Algorithm SHA256).Hash
    if ($ActualHash -ne $Release.runtime_sha256) {
        throw "The bundled runtime failed its SHA-256 integrity check."
    }
}

function Expand-BundledRuntime(
    [System.IO.FileInfo]$RuntimeArchive,
    [string]$Destination
) {
    $Tar = Get-Command tar.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($null -ne $Tar) {
        & $Tar.Source -xf $RuntimeArchive.FullName -C $Destination
        if ($LASTEXITCODE -ne 0) {
            throw "tar.exe could not extract the bundled AreaDay runtime."
        }
        return
    }
    Write-Warning "tar.exe is unavailable; falling back to the slower Expand-Archive command."
    Expand-Archive -LiteralPath $RuntimeArchive.FullName -DestinationPath $Destination
}

function Assert-WindowsRuntimePath([System.IO.FileInfo]$RuntimeArchive) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [IO.Compression.ZipFile]::OpenRead($RuntimeArchive.FullName)
    try {
        $VenvPrefix = "runtime/venv/"
        $LongestPath = 0
        foreach ($Entry in $Archive.Entries) {
            if (-not $Entry.FullName.StartsWith($VenvPrefix, [StringComparison]::Ordinal)) {
                continue
            }
            $Relative = $Entry.FullName.Substring($VenvPrefix.Length).Replace('/', '\')
            $Length = (Join-Path $VenvDir $Relative).Length
            if ($Length -gt $LongestPath) {
                $LongestPath = $Length
            }
        }
    } finally {
        $Archive.Dispose()
    }
    if ($LongestPath -ge 260) {
        throw "The AreaDay runtime path would exceed the Windows 260-character compatibility limit. Choose a shorter Skill or RESEARCHRAMP_VENV_DIR path."
    }
}

function Install-BundledRuntime([System.IO.FileInfo]$RuntimeArchive) {
    Assert-BundledRuntime $RuntimeArchive
    Assert-WindowsRuntimePath $RuntimeArchive
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    $StageDir = Join-Path ([IO.Path]::GetTempPath()) ("areaday-runtime-stage-" + [Guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $StageDir | Out-Null
    $BackupVenv = "$VenvDir.areaday-backup-$PID"
    $HadPreviousVenv = $false
    $VenvReplacementStarted = $false
    try {
        Write-Host "Installing the bundled AreaDay runtime for windows-x64..."
        Expand-BundledRuntime $RuntimeArchive $StageDir
        $StagedRoot = Join-Path $StageDir "runtime"
        $StagedVenv = Join-Path $StagedRoot "venv"
        $StagedPython = Join-Path $StagedVenv "Scripts\python.exe"
        $StagedBasePython = Join-Path $StagedVenv "base-python\python.exe"
        $StagedModel = Join-Path $StagedRoot "models\sentence-transformers"
        $StagedManifest = Join-Path $StagedRoot "runtime.json"
        if (-not (Test-Path -LiteralPath $StagedBasePython -PathType Leaf) -or
            -not (Test-Path -LiteralPath $StagedModel -PathType Container) -or
            -not (Test-Path -LiteralPath $StagedManifest -PathType Leaf)) {
            throw "The bundled runtime is incomplete."
        }
        & $StagedBasePython $PortableRuntimeScript --venv-dir $StagedVenv
        if ($LASTEXITCODE -ne 0) {
            throw "The bundled Python could not prepare the portable runtime."
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
        $VenvReplacementStarted = $true
        Move-Item -LiteralPath $StagedVenv -Destination $VenvDir
        $InstalledBasePython = Join-Path $VenvDir "base-python\python.exe"
        & $InstalledBasePython $PortableRuntimeScript --venv-dir $VenvDir
        if ($LASTEXITCODE -ne 0) {
            throw "The installed Python could not prepare the portable runtime."
        }
        $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
        & $VenvPython $SetupScript --venv-dir $VenvDir --model-dir $ModelDir
        if ($LASTEXITCODE -ne 0) {
            throw "The bundled runtime failed verification after installation."
        }
        if ($HadPreviousVenv) {
            & $VenvPython -c "import os,shutil,sys; p=os.path.abspath(sys.argv[1]); shutil.rmtree(chr(92)*2+'?'+chr(92)+p)" $BackupVenv
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "AreaDay was installed, but its previous runtime backup could not be removed: $BackupVenv"
            }
            $HadPreviousVenv = $false
        }
    } catch {
        if ($VenvReplacementStarted -and (Test-Path -LiteralPath $VenvDir)) {
            Remove-Item -LiteralPath $VenvDir -Recurse -Force
        }
        if ($HadPreviousVenv -and (Test-Path -LiteralPath $BackupVenv)) {
            Move-Item -LiteralPath $BackupVenv -Destination $VenvDir
            $HadPreviousVenv = $false
        }
        throw
    } finally {
        if (Test-Path -LiteralPath $StageDir) {
            try {
                Remove-Item -LiteralPath $StageDir -Recurse -Force
            } catch {
                Write-Warning "The temporary AreaDay runtime directory could not be removed: $StageDir"
            }
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
        if (-not (Test-Path -LiteralPath $OpenAlexConfig -PathType Leaf)) {
            & powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $OpenAlexSetupScript -Anonymous
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $OpenAlexConfig -PathType Leaf)) {
                throw "OpenAlex anonymous setup did not complete."
            }
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
exit $SetupExitCode
