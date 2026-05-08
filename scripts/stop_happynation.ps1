# Para o bot e o dashboard (processos iniciados por run_happynation_bg.ps1).

$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "logs\happynation.pids"

$killed = 0
if (Test-Path $pidFile) {
    Get-Content $pidFile | ForEach-Object {
        $parts = $_ -split "`t"
        if ($parts.Count -ge 1) {
            $procPid = [int]$parts[0]
            try {
                Stop-Process -Id $procPid -Force -ErrorAction Stop
                Write-Host ("[stop] pid={0} ({1})" -f $procPid, $parts[1]) -ForegroundColor Yellow
                $killed++
            } catch {}
        }
    }
    Remove-Item $pidFile -Force
}

# Fallback: mata por command line caso o pidfile esteja desatualizado.
$patterns = @("src\.main", "src\.dashboard")
$pythonProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
foreach ($p in $pythonProcs) {
    foreach ($pat in $patterns) {
        if ($p.CommandLine -and $p.CommandLine -match "-m\s+$pat(\s|$)") {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                Write-Host ("[stop fallback] pid={0} ({1})" -f $p.ProcessId, $pat) -ForegroundColor DarkYellow
                $killed++
            } catch {}
        }
    }
}

if ($killed -eq 0) {
    Write-Host "Nenhum processo do happynation rodando." -ForegroundColor DarkGray
} else {
    Write-Host "Total parado: $killed" -ForegroundColor Green
}
