# Weekly: generate AI summaries for completed ISO weeks, then rebuild+push
# Registered as PromptTimeline_Weekly (Sunday 07:00)
$ErrorActionPreference = "Stop"
$root = "C:\Users\atsus\000_ClaudeCode\prompt_timeline"
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("weekly_" + (Get-Date -Format "yyyyMMdd") + ".log")
Start-Transcript -Path $log -Append | Out-Null

try {
    # Load passphrase
    Get-Content ".timeline_env" | ForEach-Object {
        if ($_ -match '^\s*([^=#\s]+)\s*=\s*(.*)\s*$') {
            Set-Item -Path ("env:" + $Matches[1]) -Value $Matches[2]
        }
    }
    if (-not $env:TIMELINE_PASS) { throw "TIMELINE_PASS not set" }

    Write-Host "1/2 weekly_summary.py ..."
    python weekly_summary.py
    if ($LASTEXITCODE -ne 0) { throw "weekly_summary.py failed ($LASTEXITCODE)" }

    Write-Host "2/2 daily_rebuild.ps1 ..."
    & "$root\daily_rebuild.ps1"
    if ($LASTEXITCODE -ne 0) { throw "daily_rebuild.ps1 failed ($LASTEXITCODE)" }

    Write-Host "weekly OK"
} catch {
    Write-Host ("ERROR: " + $_.Exception.Message)
    Stop-Transcript | Out-Null
    exit 1
}
Stop-Transcript | Out-Null
