import ccxt
import os
import numpy as np
from dotenv import load_dotenv

# Carregar variáveis do .env
load_dotenv()

# Conectar à Binance
binance = ccxt.binance({
    'apiKey': os.getenv('BINANCE_API_KEY'),
    'secret': os.getenv('BINANCE_SECRET_KEY'),
})

#########################################################################
#Você pode usar qualquer um dos timeframes abaixo:
#'1m'  → 1 minuto
#'5m'  → 5 minutos
#'15m' → 15 minutos
#'1h'  → 1 hora
#'4h'  → 4 horas
#'1d'  → 1 dia

# LOCAL DE AJUSTE DO TEMPO GRAFICO "timeframe" E QUANTIDADE DE VELAS "limit"
#########################################################################
def fetch_recent_data(symbol, timeframe='1h', limit=100):################
#########################################################################
    """ Obtém candles recentes para cálculo de indicadores. """
    try:
        candles = binance.fetch_ohlcv(symbol, timeframe, limit=limit)
        closes = np.array([candle[4] for candle in candles])
        volumes = np.array([candle[5] for candle in candles])
        highs = np.array([candle[2] for candle in candles])
        lows = np.array([candle[3] for candle in candles])
        
        return closes, volumes, highs, lows
    except Exception as e:
        print(f"Erro ao buscar dados para {symbol}: {e}")
        return None, None, None, None

# Indicadores Técnicos
def calculate_ema(prices, period):
    """ Calcula a Média Móvel Exponencial (EMA). """
    if len(prices) < period:
        return None
    return np.mean(prices[-period:])

def calculate_vwap(highs, lows, closes, volumes):
    """ Calcula o VWAP (Preço Médio Ponderado pelo Volume). """
    typical_price = (highs + lows + closes) / 3
    return np.sum(typical_price * volumes) / np.sum(volumes)

def calculate_macd(prices, short=12, long=26, signal=9):
    """ Calcula o MACD e a linha de sinal. """
    if len(prices) < long:
        return None, None
    short_ema = calculate_ema(prices, short)
    long_ema = calculate_ema(prices, long)
    macd = short_ema - long_ema
    signal_line = calculate_ema(prices, signal)
    return macd, signal_line

def calculate_adx(highs, lows, closes, period=14):
    """ Calcula o ADX. """
    if len(highs) < period or len(lows) < period or len(closes) < period:
        return None
    
    highs, lows, closes = np.array(highs), np.array(lows), np.array(closes)
    tr = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    atr = np.mean(tr[-period:])
    
    up_move = highs[1:] - highs[:-1]
    down_move = lows[:-1] - lows[1:]
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    
    plus_di = 100 * np.mean(plus_dm[-period:]) / atr
    minus_di = 100 * np.mean(minus_dm[-period:]) / atr
    
    dx = 100 * np.abs(plus_di - minus_di) / (plus_di + minus_di)
    return np.mean(dx)

def calculate_rsi(prices, period=14):
    """ Calcula o RSI (Índice de Força Relativa). """
    if len(prices) < period:
        return None
    
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)

    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    
    if avg_loss == 0:
        return 100  # RSI máximo
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def get_usdt_pairs():
    """ Obtém todos os ativos /USDT disponíveis na Binance Spot. """
    try:
        markets = binance.load_markets()
        return [symbol for symbol, details in markets.items() if symbol.endswith('/USDT') and details.get('active', False) and details.get('spot', True)]
    except Exception as e:
        print(f"Erro ao carregar pares da Binance: {e}")
        return []

# Buscar ativos e aplicar filtros
usdt_pairs = get_usdt_pairs()
selected_assets = {}

for symbol in usdt_pairs:
    closes, volumes, highs, lows = fetch_recent_data(symbol)
    if closes is None or volumes is None or highs is None or lows is None:
        continue

    if len(closes) < 24:
        continue

    avg_volume = np.mean(volumes[-24:])
    volatility = (max(closes[-24:]) - min(closes[-24:])) / min(closes[-24:]) * 100
    macd, signal = calculate_macd(closes)
    adx = calculate_adx(highs, lows, closes)
    ema_1 = calculate_ema(closes, 9) # Ajuste da media
    ema_2 = calculate_ema(closes, 21) # Ajuste da media
    vwap = calculate_vwap(highs, lows, closes, volumes)
    rsi = calculate_rsi(closes)  # NOVO: RSI

    if None in [macd, signal, adx, ema_1, ema_2, vwap, rsi]:
        continue

    score = 0
    if avg_volume > 1_000_000:  # Ajuste do volume
        score += 1
    if volatility > 2:
        score += 1
    if macd > signal:
        score += 2
    if adx > 20:
        score += 2
    if ema_1 > ema_2:
        score += 2
    if closes[-1] > vwap:
        score += 2
    if rsi < 30 or rsi > 70:  # NOVO: RSI sobrecomprado/sobrevendido
        score += 2

    if score >= 8:  # Ajuste do Score para listar ativos
        selected_assets[symbol] = score

if selected_assets:
    print("Ativos promissores (ordenados por score):")
    sorted_assets = sorted(selected_assets.items(), key=lambda x: x[1], reverse=True)
    for symbol, score in sorted_assets:
        print(f"{symbol}: Score {score}")
else:
    print("Nenhum ativo encontrado com os critérios definidos.")
