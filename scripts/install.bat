@echo off
REM ========================================
REM HAPPYNATIONBOT - Windows Installation
REM ========================================
chcp 65001 > nul
color 0A
cls

echo.
echo ╔════════════════════════════════════════╗
echo ║   HAPPYNATIONBOT - Windows Setup       ║
echo ╚════════════════════════════════════════╝
echo.

REM Check if Python 3.13 is installed
echo [1/4] Verificando Python 3.13...
py -3.13 --version >nul 2>&1
if errorlevel 1 (
    color 0C
    echo ✗ ERRO: Python 3.13 não encontrado!
    echo.
    echo Baixe em: https://www.python.org/downloads/
    echo Certifique-se de marcar "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
echo ✓ Python 3.13 encontrado
echo.

REM Create virtual environment
echo [2/4] Criando ambiente virtual...
if exist .venv (
    echo ✓ Ambiente virtual já existe
) else (
    py -3.13 -m venv .venv
    if errorlevel 1 (
        color 0C
        echo ✗ Erro ao criar venv
        pause
        exit /b 1
    )
    echo ✓ Ambiente virtual criado
)
echo.

REM Activate virtual environment
echo [3/4] Ativando ambiente virtual...
call .venv\Scripts\activate.bat
if errorlevel 1 (
    color 0C
    echo ✗ Erro ao ativar venv
    pause
    exit /b 1
)
echo ✓ Ambiente virtual ativado
echo.

REM Install dependencies
echo [4/4] Instalando dependências...
pip install -q -r requirements.txt
if errorlevel 1 (
    color 0C
    echo ✗ Erro ao instalar dependências
    pause
    exit /b 1
)
echo ✓ Dependências instaladas
echo.

REM Check if .env exists
if not exist .env (
    echo ⚠ Arquivo .env não encontrado!
    echo Criando a partir do template...
    copy examples\config_conservative.env .env >nul
    echo ✓ Arquivo .env criado
    echo.
    echo ⚠ PRÓXIMA ETAPA: Edite o arquivo .env com suas credenciais Binance
    echo.
)

echo.
color 0B
echo ╔════════════════════════════════════════╗
echo ║   ✓ Instalação Completa!               ║
echo ╚════════════════════════════════════════╝
echo.
echo Para iniciar o bot:
echo   python -m src.main
echo.
echo Para rodar backtests:
echo   python generate_backtest_report.py
echo.
echo Para mais informações:
echo   Leia: INSTALL.md
echo.
pause
