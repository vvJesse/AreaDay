param(
    [ValidateSet("check", "install", "bootstrap-only")]
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
$OpenAlexSetupScript = Join-Path $ScriptDir "configure_openalex.ps1"
$OpenAlexConfig = Join-Path $HOME ".researchramp\credentials.ini"
$OpenAlexSetupProcess = $null

if ($Mode -eq "check") {
    $VenvPython = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Error "ResearchRamp runtime is not installed at $VenvDir"
        exit 1
    }
    & $VenvPython $SetupScript --venv-dir $VenvDir --model-dir $ModelDir
    exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
if ($Mode -eq "install") {
    $OpenAlexSetupProcess = Start-Process powershell.exe -PassThru -ArgumentList @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-File", ('"' + $OpenAlexSetupScript + '"')
    )
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
    if ($null -ne $OpenAlexSetupProcess) {
        $OpenAlexSetupProcess.WaitForExit()
        if ($OpenAlexSetupProcess.ExitCode -ne 0) {
            throw "OpenAlex setup did not complete."
        }
    }
    if (-not (Test-Path -LiteralPath $OpenAlexConfig -PathType Leaf)) {
        throw "OpenAlex setup did not create $OpenAlexConfig"
    }
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
