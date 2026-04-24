# Daily rebuild: extract prompts -> AES-GCM encrypt -> git push
# Registered in Task Scheduler as PromptTimeline_Daily (runs daily)
$ErrorActionPreference = "Stop"
$root = "C:\Users\atsus\000_ClaudeCode\prompt_timeline"
Set-Location $root

$logDir = Join-Path $root "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$log = Join-Path $logDir ("run_" + (Get-Date -Format "yyyyMMdd") + ".log")
Start-Transcript -Path $log -Append | Out-Null

try {
    # Load passphrase
    Get-Content ".timeline_env" | ForEach-Object {
        if ($_ -match '^\s*([^=#\s]+)\s*=\s*(.*)\s*$') {
            Set-Item -Path ("env:" + $Matches[1]) -Value $Matches[2]
        }
    }
    if (-not $env:TIMELINE_PASS) { throw "TIMELINE_PASS not set (check .timeline_env)" }

    # Rebuild encrypted payload
    python build_timeline.py
    if ($LASTEXITCODE -ne 0) { throw "build_timeline.py failed ($LASTEXITCODE)" }

    # Commit + push only if changed
    $changed = git status --porcelain site/data.enc.json
    if ($changed) {
        git add site/data.enc.json
        git -c user.email=atsushi.tonkou@gmail.com -c user.name=MuscleLove-777 `
            commit -m ("daily rebuild " + (Get-Date -Format "yyyy-MM-dd"))
        git push origin main
        Write-Host "pushed"
    } else {
        Write-Host "no changes"
    }
} catch {
    Write-Host ("ERROR: " + $_.Exception.Message)
    Stop-Transcript | Out-Null
    exit 1
}

Stop-Transcript | Out-Null
