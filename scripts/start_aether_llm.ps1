# Sobe APENAS os 3 modulos do Aether que dependem de Claude/LLM:
#   server, auto_validator, hyper_agent
# Os modulos sem LLM (critic_agent, promote_loop) ja rodam 24/7
# junto com o bot principal.
#
# Uso (com Claude Code aberto):
#   .\scripts\start_aether_llm.ps1
#
# Para parar so esses 3:
#   .\scripts\stop_aether_llm.ps1

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
$logsDir = Join-Path $root "src\logs\aether"
$pidFile = Join-Path $root "logs\aether_llm.pids"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path $pidFile) -Force | Out-Null
"" | Set-Content $pidFile -Encoding UTF8

# Verifica que ja nao esta rodando
$running = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object {
    $_.CommandLine -match 'src\.research\.aether\.(server|auto_validator|hyper_agent)'
}
if ($running) {
    Write-Host "Ja existem $($running.Count) processos LLM rodando. Pare antes com stop_aether_llm.ps1"
    exit 1
}

function Spawn-Detached($name, [string[]]$argList, $log) {
    $p = Start-Process -FilePath $python -ArgumentList $argList -WorkingDirectory $root -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logsDir\$log.out.log" -RedirectStandardError "$logsDir\$log.err.log"
    "$($p.Id)`t$name" | Add-Content $pidFile -Encoding UTF8
    Write-Host "  [$name] pid=$($p.Id)"
}

Write-Host "Subindo Aether LLM stack (server + validator + hyper):"
Spawn-Detached "server"         @("-u","-m","src.research.aether.server")          "server"
Spawn-Detached "auto_validator" @("-u","-m","src.research.aether.auto_validator")  "auto_validator"
Spawn-Detached "hyper_agent"    @("-u","-m","src.research.aether.hyper_agent")     "hyper_agent"

Write-Host "`nPIDs em $pidFile"
Write-Host "Logs em $logsDir"
