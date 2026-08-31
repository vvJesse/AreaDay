param()

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

function Create-ConfigurationTemplate {
    New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
    if (-not (Test-Path -LiteralPath $ConfigPath -PathType Leaf)) {
        $Template = @"
[openalex]

# 请把完整的 OpenAlex API Key 粘贴到等号右侧，然后保存。
# 不要把本文件上传或发送到聊天中。
# 如果明确选择匿名额度，请填写 anonymous。
api_key =
"@
        [IO.File]::WriteAllText($ConfigPath, ($Template.TrimStart() + "`n"), $Utf8)
    }
    Restrict-Configuration
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

Create-ConfigurationTemplate
$SetupFilesOpened = $false
function Open-SetupFiles {
    if ($script:SetupFilesOpened) { return }
    Start-Process $HelpPath
    Start-Process notepad.exe -ArgumentList ('"' + $ConfigPath + '"')
    $script:SetupFilesOpened = $true
}
if ([string]::IsNullOrWhiteSpace((Read-Setting "api_key"))) {
    Open-SetupFiles
    Write-Host "OpenAlex instructions and the local configuration file are open."
    Write-Host "Look for credentials.ini in the Codex/WorkBuddy file panel or the system text editor."
    Write-Host "Paste the key after 'api_key =', save the file, and keep this task open until validation finishes."
}

$LastAttemptedValue = ""
while ($true) {
    $ApiKey = Read-Setting "api_key"
    if ([string]::IsNullOrWhiteSpace($ApiKey) -or $ApiKey -eq $LastAttemptedValue) {
        Start-Sleep -Milliseconds 500
        continue
    }
    $LastAttemptedValue = $ApiKey

    if ($ApiKey -eq "anonymous") {
        Write-Host "OpenAlex anonymous access selected at $ConfigPath"
        exit 0
    }
    if ($ApiKey -notmatch "^[A-Za-z0-9_-]{12,200}$") {
        Write-Warning "OpenAlex did not recognize the saved value. Replace it with the complete API key and save again."
        Open-SetupFiles
        continue
    }

    $Validation = Test-OpenAlexKey $ApiKey
    if ($Validation -eq "valid") {
        Write-Host "OpenAlex key verified at $ConfigPath"
        exit 0
    }
    if ($Validation -eq "invalid") {
        Write-Warning "OpenAlex did not recognize this key. Copy the complete key from OpenAlex Settings and save again."
        Open-SetupFiles
        continue
    }
    throw "Could not connect to OpenAlex to verify the key. Run this setup again when OpenAlex is reachable."
}
