#!/bin/bash

# ========================================
# HAPPYNATIONBOT - Linux/Mac Installation
# ========================================

set -e

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   HAPPYNATIONBOT - Linux/Mac Setup     ║"
echo "╚════════════════════════════════════════╝"
echo ""

# Check Python version
echo "[1/4] Verificando Python 3.13..."
if ! command -v python3.13 &> /dev/null; then
    echo "✗ ERRO: Python 3.13 não encontrado!"
    echo ""
    echo "Instale com:"
    echo "  Ubuntu/Debian: sudo apt-get install python3.13"
    echo "  Mac: brew install python@3.13"
    echo ""
    exit 1
fi
python3.13 --version
echo "✓ Python 3.13 encontrado"
echo ""

# Create virtual environment
echo "[2/4] Criando ambiente virtual..."
if [ -d ".venv" ]; then
    echo "✓ Ambiente virtual já existe"
else
    python3.13 -m venv .venv
    echo "✓ Ambiente virtual criado"
fi
echo ""

# Activate virtual environment
echo "[3/4] Ativando ambiente virtual..."
source .venv/bin/activate
echo "✓ Ambiente virtual ativado"
echo ""

# Install dependencies
echo "[4/4] Instalando dependências..."
pip install -q -r requirements.txt
echo "✓ Dependências instaladas"
echo ""

# Check if .env exists
if [ ! -f ".env" ]; then
    echo "⚠ Arquivo .env não encontrado!"
    echo "Criando a partir do template..."
    cp examples/config_conservative.env .env
    echo "✓ Arquivo .env criado"
    echo ""
    echo "⚠ PRÓXIMA ETAPA: Edite o arquivo .env com suas credenciais Binance"
    echo ""
fi

echo ""
echo "╔════════════════════════════════════════╗"
echo "║   ✓ Instalação Completa!               ║"
echo "╚════════════════════════════════════════╝"
echo ""
echo "Para iniciar o bot:"
echo "  source .venv/bin/activate"
echo "  python -m src.main"
echo ""
echo "Para rodar backtests:"
echo "  python generate_backtest_report.py"
echo ""
echo "Para mais informações:"
echo "  Leia: INSTALL.md"
echo ""
