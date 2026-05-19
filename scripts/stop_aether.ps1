# Mata todos os processos do stack Aether iniciados via run_aether_bg.ps1
# e tambem qualquer python.exe do projeto que esteja rodando algum src.research.aether.*,
# src.main ou src.dashboard.
# Tambem fecha as janelas powershell.exe sobreviventes do run_aether_economy/stack legados.
#
# Uso:
#   .\scripts\stop_aether.ps1            # mata aether + janelas powershell sobreviventes
#   .\scripts\stop_aether.ps1 -Quiet     # nao imprime detalhes
#   .\scripts\stop_aether.ps1 -KeepShells  # nao fecha janelas powershell, so processos aether

param(
    [switch]$Quiet,
    [switch]$KeepShells
)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path $root "logs\aether.pids"

$killed = 0

# 1. PIDs registrados por run_aether_bg.ps1
if (Test-Path $pidFile) {
    foreach ($line in Get-Content $pidFile) {
        $parts = $line -split "`t"
        if ($parts.Count -lt 1) { continue }
        $procId = [int]$parts[0]
        try {
            $p = Get-Process -Id $procId -ErrorAction Stop
            Stop-Process -Id $procId -Force -ErrorAction Stop
            if (-not $Quiet) { Write-Host ("[stop] pid={0} ({1})" -f $procId, $parts[1]) -ForegroundColor Yellow }
            $killed++
        } catch {}
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}

# 2. Qualquer python.exe ainda rodando modulos do projeto (caso processo orfao)
$pythonProcs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue
foreach ($p in $pythonProcs) {
    $cmd = $p.CommandLine
    $isProjectPython = $false
    if ($cmd) {
        $isProjectPython = (
            ($cmd -match "src\.research\.aether\.") -or
            ($cmd -match "scripts[\\/]+_watchdog_stack\.py") -or
            ($cmd -match "-m\s+scripts\._watchdog_stack(\s|$)") -or
            ($cmd -match "-m\s+src\.main(\s|$)") -or
            ($cmd -match "-m\s+src\.dashboard(\s|$)")
        )
    }
    if ($isProjectPython) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            if (-not $Quiet) { Write-Host ("[stop] pid={0} (python projeto orfao)" -f $p.ProcessId) -ForegroundColor Yellow }
            $killed++
        } catch {}
    }
}

# 2b. Codex CLI execs iniciados pelo Aether podem sobreviver por alguns
# segundos se o servidor for encerrado durante uma chamada LLM.
$codexProcs = Get-CimInstance Win32_Process -Filter "Name='codex.exe' OR Name='Codex.exe'" -ErrorAction SilentlyContinue
foreach ($p in $codexProcs) {
    $cmd = $p.CommandLine
    if ($cmd -and ($cmd -match "\\.sandbox-bin\\codex\.exe\s+exec" -or $cmd -match "\\.sandbox-bin\\codex\.exe.*\s+exec\s+")) {
        try {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
            if (-not $Quiet) { Write-Host ("[stop] pid={0} (codex cli aether)" -f $p.ProcessId) -ForegroundColor Yellow }
            $killed++
        } catch {}
    }
}

# 3. Janelas powershell.exe legadas (run_aether_economy/stack abriam Start-Process powershell)
if (-not $KeepShells) {
    $shells = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue
foreach ($p in $shells) {
        if ([int]$p.ProcessId -eq $PID) { continue }
        $cmd = $p.CommandLine
        if ($cmd -and ($cmd -match "AETHER_" -or $cmd -match "src\.research\.aether")) {
            try {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop
                if (-not $Quiet) { Write-Host ("[stop] pid={0} (powershell legacy aether)" -f $p.ProcessId) -ForegroundColor Yellow }
                $killed++
            } catch {}
        }
    }
}

if ($killed -eq 0) {
    Write-Host "Nada do Aether estava rodando." -ForegroundColor DarkGray
} else {
    Write-Host ("OK - {0} processo(s) encerrado(s)." -f $killed) -ForegroundColor Green
}
