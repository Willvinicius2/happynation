#!/bin/bash
# SCRIPT PARA RODAR NA VPS

# ----------------------------------------------------

# Esse script instala o python os pacotes que estão em requirements.txt
# Se você reiniciar o container do Coolify, terá que executar esse script novamente.

# 1️⃣ Rode no terminal:
# chmod +x vps_script.sh

# 2️⃣ Rode no terminal:
# sed -i 's/\r$//' vps_script.sh

# 3️⃣ Execute o script:
# ./vps_script.sh

# 4️⃣ Ative a venv:
# source venv/bin/activate

#  5️⃣ Inicie o robô:
# python -m src.main

# ----------------------------------------------------
# 💡 DICA EXTRA:
# Se você estiver tendo problema com o robô pausando na VPS,
# pode ser alguma configuração da sua hospedagem
# Nesse caso, a solução mais fácil é executar o robô usando o comando "nohup"

# Você pode pesquisar sobre o nohup com alguma IA, mas aqui um resumo:

# Iniciar o robô:
# nohup python -m src.main > app.log 2>&1 &

# Ver se está rodando (Esse código também retorna o PID do processo e você pode parar ele com kill <PID>)
# ps -ef | grep "python -m src.main"

# Jeito mais fácil de parar o processo:
# pkill -f "python -m src.main"

# ----------------------------------------------------
# Nome da virtualenv
VENV_NAME="venv"

echo "🤖 Configurando VPS..."

echo ""
echo "🧪 Atualizando repositórios..."
sudo apt update

echo ""
echo "🐍 Instalando Python e venv..."
sudo apt install -y python3 python3-venv python3-pip

echo ""
echo "📦 Criando ambiente virtual: $VENV_NAME"
python3 -m venv $VENV_NAME

echo ""
echo "🚀 Ativando a venv..."
source $VENV_NAME/bin/activate
echo ""
echo "📚 Instalando pacotes do requirements.txt..."
pip install --upgrade pip
pip install -r requirements.txt

echo ""
echo "✅ Ambiente configurado com sucesso!"
echo ""
echo "➡️  RODE NO TERMINAL: source $VENV_NAME/bin/activate"
echo ""
echo "--------------------------------------------------"