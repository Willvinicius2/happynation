$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$logPath = Join-Path $root "logs\stack-bootstrap.log"

Set-Location $root

function Write-BootstrapLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format o), $Message
    Add-Content -Path $logPath -Value $line
}

Set-Content -Path $logPath -Value ("[{0}] bootstrap start" -f (Get-Date -Format o)) -Encoding utf8

try {
    powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\stop_happynation.ps1") | Out-Null
    powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\stop_aether.ps1") -Quiet | Out-Null
    Write-BootstrapLog "previous stack stopped"

    powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\run_aether_bg.ps1") -Full | Tee-Object -FilePath $logPath -Append | Out-Null
    Start-Sleep -Seconds 5
    powershell -ExecutionPolicy Bypass -File (Join-Path $root "scripts\run_happynation_bg.ps1") | Tee-Object -FilePath $logPath -Append | Out-Null

    Write-BootstrapLog "bootstrap done"
} catch {
    Write-BootstrapLog ("bootstrap failed: {0}" -f $_.Exception.Message)
    throw
}
