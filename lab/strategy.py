"""Shared pure strategy decision for backtest and execution. No orders here."""
from .rsi import _wilder_rsi

def signal(candles, strategy='rsi'):
    closed = sorted((r for r in candles if len(r) >= 9 and r[8] == '1'), key=lambda r: int(r[0]))
    if len(closed) < 100:
        return {'side': 'hold', 'reason': '已收盘 K 线不足 100 根', 'metric': None}
    closes = [float(r[4]) for r in closed[-100:]]
    if strategy == 'rsi':
        value = _wilder_rsi(closes, 14)
        side = 'buy' if value < 30 else 'sell' if value > 70 else 'hold'
        return {'side': side, 'reason': f'RSI(14) = {value:.1f}；买入线 30 / 卖出线 70', 'metric': value}
    window = closed[-48:]
    low, high = min(float(r[3]) for r in window), max(float(r[2]) for r in window)
    value = (closes[-1] - low) / (high - low) if high > low else .5
    side = 'buy' if value <= .2 else 'sell' if value >= .8 else 'hold'
    return {'side': side, 'reason': f'近 48 小时区间位置 {value:.0%}；买入线 20% / 卖出线 80%', 'metric': value}
