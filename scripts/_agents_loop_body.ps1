# Body do loop hidden dos agentes (chamado por run_agents_loop.ps1).
# Executa src.agent_runner em loop ate ser morto.
#
# Vars de ambiente lidas:
#   AETHER_AGENTS_INTERVAL_SEC (default 300s)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python  = Join-Path $root ".venv\Scripts\python.exe"
$logFile = Join-Path $root "src\logs\agents\runner_loop.log"
$interval = 300
if ($env:AETHER_AGENTS_INTERVAL_SEC) {
    try { $interval = [int]$env:AETHER_AGENTS_INTERVAL_SEC } catch {}
}

New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
"[$([DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ'))] LOOP START interval=${interval}s" |
    Out-File -FilePath $logFile -Append -Encoding utf8

while ($true) {
    $ts = [DateTime]::UtcNow.ToString('yyyy-MM-ddTHH:mm:ssZ')
    "[$ts] tick" | Out-File -FilePath $logFile -Append -Encoding utf8
    try {
        & $python -u -m src.agent_runner 2>&1 |
            Out-File -FilePath $logFile -Append -Encoding utf8
    } catch {
        "[$ts] ERROR: $_" | Out-File -FilePath $logFile -Append -Encoding utf8
    }
    Start-Sleep -Seconds $interval
}
