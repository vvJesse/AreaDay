param(
    [switch]$Reconfigure
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillDir = Split-Path -Parent $ScriptDir
$HelpPath = Join-Path $SkillDir "assets\openalex-help.html"
$ConfigDir = Join-Path $HOME ".researchramp"
$ConfigPath = Join-Path $ConfigDir "credentials.ini"
$Utf8 = [Text.UTF8Encoding]::new($false)

if (-not (Test-Path -LiteralPath $HelpPath -PathType Leaf)) {
    throw "OpenAlex help page is missing: $HelpPath"
}

function Read-Setting {
    param([Parameter(Mandatory)][string]$Name)
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) { return "" }
    $Pattern = "(?m)^\s*" + [Regex]::Escape($Name) + "\s*=\s*(.*?)\s*$"
    $Content = [IO.File]::ReadAllText($ConfigPath, [Text.Encoding]::UTF8)
    $Match = [Regex]::Match($Content, $Pattern)
    if (-not $Match.Success) { return "" }
    return $Match.Groups[1].Value.Trim()
}

function Restrict-Configuration {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $ConfigPath /inheritance:r /grant:r "${Identity}:(F)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not restrict the OpenAlex configuration file to the current user."
    }
}

function Save-Configuration {
    param([Parameter(Mandatory)][string]$ApiKey)
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    # Keep this credential file ASCII-only so localized Windows editors cannot
    # misidentify its encoding and show the user garbled instructional text.
    $Content = "[openalex]`napi_key = $ApiKey`n"
    [IO.File]::WriteAllText($ConfigPath, $Content, $Utf8)
    Restrict-Configuration
}

function Read-OpenAlexKey {
    Write-Host "Paste the OpenAlex API key at the hidden prompt below."
    Write-Host "Enter anonymous if you want to use OpenAlex without a key."
    $SecureInput = Read-Host -Prompt "OpenAlex API key" -AsSecureString
    $Pointer = [IntPtr]::Zero
    try {
        $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureInput)
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer).Trim()
    } finally {
        if ($Pointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
        }
        $SecureInput.Dispose()
    }
}

function Test-OpenAlexKey {
    param([Parameter(Mandatory)][string]$ApiKey)
    try {
        $Headers = @{ Authorization = "Bearer $ApiKey" }
        $Response = Invoke-WebRequest -UseBasicParsing -TimeoutSec 30 -Uri "https://api.openalex.org/rate-limit" -Headers $Headers
        if ($Response.StatusCode -eq 200) { return "valid" }
        return "unavailable"
    } catch {
        $StatusCode = $null
        if ($null -ne $_.Exception.Response) {
            $StatusCode = [int]$_.Exception.Response.StatusCode
        }
        if ($StatusCode -eq 401 -or $StatusCode -eq 403) { return "invalid" }
        return "unavailable"
    }
}

$SetupHelpOpened = $false
function Open-SetupHelp {
    if ($script:SetupHelpOpened) { return }
    Start-Process $HelpPath
    $script:SetupHelpOpened = $true
}

$ApiKey = if ($Reconfigure) { "" } else { Read-Setting "api_key" }
if (-not [string]::IsNullOrWhiteSpace($ApiKey)) {
    if ($ApiKey -eq "anonymous") {
        Save-Configuration $ApiKey
        Write-Host "OpenAlex anonymous access is already configured."
        exit 0
    }
    if ($ApiKey -match "^[A-Za-z0-9_-]{12,200}$" -and (Test-OpenAlexKey $ApiKey) -eq "valid") {
        Save-Configuration $ApiKey
        Write-Host "OpenAlex key verified at $ConfigPath"
        exit 0
    }
    Write-Warning "The saved OpenAlex key could not be verified. Enter it again."
}

Open-SetupHelp
while ($true) {
    $ApiKey = Read-OpenAlexKey

    if ($ApiKey -eq "anonymous") {
        Save-Configuration $ApiKey
        Write-Host "OpenAlex anonymous access selected at $ConfigPath"
        exit 0
    }
    if ($ApiKey -notmatch "^[A-Za-z0-9_-]{12,200}$") {
        Write-Warning "OpenAlex did not recognize that value. Paste the complete API key, or enter anonymous."
        continue
    }

    $Validation = Test-OpenAlexKey $ApiKey
    if ($Validation -eq "valid") {
        Save-Configuration $ApiKey
        Write-Host "OpenAlex key verified at $ConfigPath"
        exit 0
    }
    if ($Validation -eq "invalid") {
        Write-Warning "OpenAlex did not recognize that key. Copy the complete key from OpenAlex Settings and try again."
        continue
    }
    throw "Could not connect to OpenAlex to verify the key. Run this setup again when OpenAlex is reachable."
}
