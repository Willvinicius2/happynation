import time
import numpy as np

# Preços base aproximados por ativo
SYMBOL_BASE_PRICES = {
    "BTC": 95000.0,
    "ETH": 3500.0,
    "XRP": 2.5,
    "SOL": 180.0,
    "ADA": 0.8,
    "JUP": 0.9,
    "BNB": 650.0,
    "DOGE": 0.35,
    "MATIC": 0.9,
}

# tick/step por ativo
SYMBOL_FILTERS = {
    "BTC":  {"tick": "0.01",    "step": "0.00001"},
    "ETH":  {"tick": "0.01",    "step": "0.0001"},
    "XRP":  {"tick": "0.0001",  "step": "0.1"},
    "SOL":  {"tick": "0.001",   "step": "0.001"},
    "ADA":  {"tick": "0.0001",  "step": "1.0"},
    "JUP":  {"tick": "0.0001",  "step": "1.0"},
    "BNB":  {"tick": "0.01",    "step": "0.001"},
    "DOGE": {"tick": "0.00001", "step": "1.0"},
}


class MockBinanceClient:
    """
    Cliente Binance falso para testes offline.
    Gera dados OHLCV sintéticos via random walk e simula os endpoints usados pelo bot.
    Não faz nenhuma chamada HTTP.
    """

    def __init__(self, base_stock="JUP", quote_stock="USDT", total_candles=2000, seed=42):
        self.base_stock = base_stock
        self.quote_stock = quote_stock
        self.timestamp_offset = 0
        self._current_index = 0

        base_price = SYMBOL_BASE_PRICES.get(base_stock, 10.0)
        self._candles = self._generate_candles(base_price, total_candles, seed)

    def _generate_candles(self, base_price, n, seed):
        rng = np.random.default_rng(seed)

        # Random walk com drift leve e volatilidade realista
        returns = rng.normal(0.0003, 0.018, n)
        closes = base_price * np.exp(np.cumsum(returns))

        now_ms = int(time.time() * 1000)
        start_ms = now_ms - n * 3_600_000  # candles de 1h retroativos

        candles = []
        for i in range(n):
            close = float(closes[i])
            open_ = float(closes[i - 1]) if i > 0 else close
            noise_up = float(rng.uniform(1.000, 1.015))
            noise_dn = float(rng.uniform(0.985, 1.000))
            high = max(open_, close) * noise_up
            low = min(open_, close) * noise_dn
            volume = float(rng.uniform(50_000, 300_000))
            open_time = start_ms + i * 3_600_000
            close_time = open_time + 3_599_999

            candles.append([
                open_time,
                str(round(open_, 6)),
                str(round(high, 6)),
                str(round(low, 6)),
                str(round(close, 6)),
                str(round(volume, 2)),
                close_time,
                str(round(volume * close, 2)),
                int(volume / 10),
                str(round(volume * 0.5, 2)),
                str(round(volume * 0.5 * close, 2)),
                "0",
            ])

        return candles

    # ------------------------------------------------------------------
    # Métodos mockados

    def ping(self):
        return {}

    def get_server_time(self):
        return {"serverTime": int(time.time() * 1000)}

    def get_account(self):
        return {
            "balances": [
                {"asset": "USDT",           "free": "10000.0", "locked": "0.0"},
                {"asset": self.base_stock,  "free": "0.0",     "locked": "0.0"},
            ]
        }

    def get_klines(self, symbol, interval, limit=1000):
        start = self._current_index
        end = start + limit
        self._current_index += 1

        # Reinicia quando chegar no fim
        if end > len(self._candles):
            self._current_index = 0
            start = 0
            end = limit

        return self._candles[start:end]

    def get_symbol_info(self, symbol):
        base = symbol.replace("USDT", "").replace("BRL", "").replace("BTC", "")
        filters = SYMBOL_FILTERS.get(base, {"tick": "0.0001", "step": "1.0"})
        return {
            "symbol": symbol,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "tickSize": filters["tick"],
                    "minPrice": "0.00001",
                    "maxPrice": "1000000.0",
                },
                {
                    "filterType": "LOT_SIZE",
                    "stepSize": filters["step"],
                    "minQty": "0.00001",
                    "maxQty": "9000000.0",
                },
                {
                    "filterType": "MIN_NOTIONAL",
                    "minNotional": "5.0",
                },
            ],
        }

    def get_all_orders(self, symbol, limit=100):
        return []

    def get_open_orders(self, symbol):
        return []

    def create_order(self, **kwargs):
        # Interceptado pelo paper trading em BinanceTraderBot — não deve chegar aqui
        return {"status": "FILLED", "executedQty": "0", "cummulativeQuoteQty": "0"}

    def cancel_order(self, symbol=None, orderId=None):
        return {}

    def _request(self, *args, **kwargs):
        return {}
