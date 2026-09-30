"""Coin scanner: filter and rank OKX USDT spot pairs from public market data. Places no orders.

Hard filters decide which pairs may be traded at all. The score only orders the survivors by
momentum, strength against BTC, liquidity and trend; it is not a forecast of profit. Scanning
hundreds of coins and trading the best-looking one invites selection bias, so every candidate
still needs an out-of-sample backtest and a demo run.

Market cap and rank stay empty until a market-data provider (e.g. CoinMarketCap) is connected;
rows carry the fields so an enricher can fill them without changing the table.
"""
from __future__ import annotations
import asyncio
import math
import re
import statistics
import time
from collections import Counter
from .okx import OkxError, PAIR_RE

DEFAULTS = {'min_volume': 1_000_000.0, 'max_spread_pct': 0.2, 'min_age_days': 90, 'max_vol_pct': 150.0, 'max_candidates': 200}
# Pegged or asset-backed tokens: holding them is not a crypto price position.
PEGGED = {'USDT', 'USDC', 'DAI', 'FDUSD', 'TUSD', 'USDG', 'RLUSD', 'PYUSD', 'USDE', 'USDS', 'USDD', 'USDP', 'USD1',
          'BUSD', 'GUSD', 'FRAX', 'LUSD', 'EURC', 'EURT', 'XAUT', 'PAXG'}
LEVERAGED = re.compile(r'\d+[LS]$')
WEIGHTS = {'mom28': .35, 'mom7': .15, 'rel_btc28': .25, 'liquidity': .15}
TREND_WEIGHT = .10
STALE_MS = 3*86400000  # newest closed daily bar older than this means trading paused


def _num(value):
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def hard_filter(instruments, tickers, params):
    """Tradability from instrument + ticker data alone. Returns (rows, excluded reason counts)."""
    by_id = {t.get('instId'): t for t in tickers}
    rows, excluded = [], Counter()
    for inst in instruments:
        pair, base = str(inst.get('instId', '')), str(inst.get('baseCcy', ''))
        ticker = by_id.get(pair) or {}
        last, bid, ask = _num(ticker.get('last')), _num(ticker.get('bidPx')), _num(ticker.get('askPx'))
        volume, open24 = _num(ticker.get('volCcy24h')), _num(ticker.get('open24h'))
        if inst.get('quoteCcy') != 'USDT' or not PAIR_RE.fullmatch(pair):
            reason = '非 USDT 现货'
        elif inst.get('state') != 'live' or inst.get('ruleType') not in (None, '', 'normal'):
            reason = '暂不可交易'
        elif base in PEGGED or LEVERAGED.search(base):
            reason = '稳定币或杠杆代币'
        elif inst.get('instCategory') not in (None, '', '1'):
            reason = '股票代币等非加密资产'  # OKX lists tokenized equities as spot; they track stock hours, not crypto
        elif not (last and bid and ask and volume is not None and last > 0 and 0 < bid <= ask):
            reason = '无成交或盘口不全'
        elif volume < params['min_volume']:
            reason = '成交额不足'
        elif (ask - bid) / ((ask + bid) / 2) * 100 > params['max_spread_pct']:
            reason = '价差过大'
        else:
            reason = None
        if reason:
            excluded[reason] += 1
            continue
        rows.append({'pair': pair, 'base': base, 'price': last, 'volume_24h': volume,
                     'spread_pct': round((ask - bid) / ((ask + bid) / 2) * 100, 4),
                     'change_24h_pct': round((last / open24 - 1) * 100, 2) if open24 else None,
                     'min_size': inst.get('minSz'), 'lot_size': inst.get('lotSz'),
                     'market_cap': None, 'market_cap_rank': None})
    return rows, excluded


def metrics(daily, btc_mom28):
    """Momentum, strength vs BTC, trend and volatility from closed daily bars (newest first)."""
    closes = [float(r[4]) for r in reversed(daily)]
    returns = [math.log(b / a) for a, b in zip(closes[-31:], closes[-30:])]
    mom28 = closes[-1] / closes[-29] - 1
    return {'age_days': len(closes), 'mom7_pct': round((closes[-1] / closes[-8] - 1) * 100, 2),
            'mom28_pct': round(mom28 * 100, 2),
            'rel_btc28_pct': round(((1 + mom28) / (1 + btc_mom28) - 1) * 100, 2),
            'trend_up': closes[-1] > statistics.fmean(closes[-50:]),
            'vol_pct': round(statistics.pstdev(returns) * math.sqrt(365) * 100, 1)}


def _percentiles(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.] * len(values)
    for position, i in enumerate(order):
        ranks[i] = position / (len(values) - 1) if len(values) > 1 else .5
    return ranks


def score(rows):
    """0-100 weighted percentile score; ties and a single row sit at the middle."""
    columns = {'mom28': [r['mom28_pct'] for r in rows], 'mom7': [r['mom7_pct'] for r in rows],
               'rel_btc28': [r['rel_btc28_pct'] for r in rows], 'liquidity': [math.log10(r['volume_24h']) for r in rows]}
    ranks = {k: _percentiles(v) for k, v in columns.items()}
    for i, row in enumerate(rows):
        total = sum(WEIGHTS[k] * ranks[k][i] for k in WEIGHTS) + TREND_WEIGHT * row['trend_up']
        row['score'] = round(total * 100, 1)
    rows.sort(key=lambda r: r['score'], reverse=True)
    for rank, row in enumerate(rows, 1):
        row['rank'] = rank
    return rows


async def scan(client, params=None, *, pace=.25, progress=None):
    """Full scan: instruments + tickers, hard filters, daily candles per survivor, score.

    One page of 100 daily candles holds at most 99 closed bars, so min_age_days stays at or below 90.
    """
    params = {**DEFAULTS, **(params or {})}
    instruments, tickers = await client.instruments(), await client.tickers()
    prelim, excluded = hard_filter(instruments, tickers, params)
    prelim.sort(key=lambda r: r['volume_24h'], reverse=True)
    if len(prelim) > params['max_candidates']:
        excluded['超出扫描上限'] += len(prelim) - params['max_candidates']
        prelim = prelim[:params['max_candidates']]
    btc = await client.recent_candles('BTC-USDT', bar='1D', limit=100)
    if len(btc) < 60:
        raise OkxError('BTC 日线不足，无法计算相对强弱')
    btc_closes = [float(r[4]) for r in reversed(btc)]
    btc_mom28 = btc_closes[-1] / btc_closes[-29] - 1
    rows = []
    for done, row in enumerate(prelim, 1):
        if progress:
            progress(done, len(prelim))
        if pace:
            await asyncio.sleep(pace)  # stay well under the public rate limit shared with trading
        try:
            daily = await client.recent_candles(row['pair'], bar='1D', limit=100)
        except (OkxError, ValueError):
            excluded['K 线获取失败'] += 1
            continue
        if len(daily) < max(params['min_age_days'], 60):
            excluded['上线时间太短'] += 1
            continue
        if time.time()*1000 - int(daily[0][0]) > STALE_MS:
            excluded['日线中断'] += 1
            continue
        row.update(metrics(daily, btc_mom28))
        if row['vol_pct'] > params['max_vol_pct']:
            excluded['波动过大'] += 1
            continue
        rows.append(row)
    ranked = score(rows)
    return {'as_of': time.time(), 'params': params, 'rows': ranked, 'universe': [r['pair'] for r in ranked],
            'excluded': dict(excluded), 'listed': len(instruments), 'scanned': len(prelim)}
