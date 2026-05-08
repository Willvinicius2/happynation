@echo off
REM Sobe o bot de trade e o dashboard do HappyNation em background.
REM Logs em logs\bot-*.log e logs\dashboard-*.log.
REM Trade dashboard: http://127.0.0.1:8000

cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_happynation_bg.ps1" -Restart

echo.
echo HappyNation iniciado. Abrindo dashboard...
start "" "http://127.0.0.1:8000"
echo.
echo Para parar: scripts\stop_happynation.ps1
pause
