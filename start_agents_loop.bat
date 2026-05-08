@echo off
REM Liga o agents loop (PolicyAgent + Tier 3 agents).
REM ATENCAO: este loop pode escrever kill switch global e liquidar
REM posicoes do bot quando agentes (Juiz, Maquinista, etc) emitem
REM CRITICAL findings. Use somente quando precisar dessa supervisao.
REM
REM Para parar: Stop-Process -Id (Get-Content src\logs\agents\loop.pid)

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_agents_loop.ps1"

echo.
echo Agents loop iniciado. PolicyAgent ativo - kill switch automatico ON.
pause
