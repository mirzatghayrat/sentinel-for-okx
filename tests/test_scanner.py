import asyncio
import math
import time
import httpx
import pytest
from lab.okx import OkxClient, OkxCredentials, OkxError
from lab.scanner import DEFAULTS, hard_filter, metrics, scan


def run(coro): return asyncio.run(coro)
DAY = 86400000


def daily(path, days=100):
    """Closed daily bars, newest first; path(i) is the close i days ago."""
    today = int(time.time() // 86400) * DAY
    rows = [[str(today), '1', '1', '1', '1', '1', '1', '1', '0']]  # in-progress bar is dropped
    for i in range(1, days + 1):
        c = path(i)
        rows.append([str(today - i * DAY), str(c), str(c * 1.01), str(c * .99), str(c), '1', '1', '1', '1'])
    return rows


def ticker(pair, last, volume, spread=.0005):
    return {'instId': pair, 'last': str(last), 'bidPx': str(last * (1 - spread / 2)), 'askPx': str(last * (1 + spread / 2)),
            'open24h': str(last), 'volCcy24h': str(volume)}


def inst(pair, quote='USDT', state='live', rule='normal', category='1'):
    return {'instId': pair, 'baseCcy': pair.split('-')[0], 'quoteCcy': quote, 'state': state, 'ruleType': rule,
            'instCategory': category, 'minSz': '0.0001', 'lotSz': '0.0001'}


PATHS = {
    'BTC-USDT': lambda i: 100000 * (1 - .001 * i),          # gently rising into today
    'AAA-USDT': lambda i: 10 * math.exp(-.01 * i),          # strong uptrend
    'BBB-USDT': lambda i: 10 * math.exp(.01 * i),           # steady downtrend
    'WILD-USDT': lambda i: 10 * (1.6 if i % 2 else .6),     # extreme swings
}
CANDLES = {**{p: daily(f) for p, f in PATHS.items()}, 'NEW-USDT': daily(lambda i: 5, days=30),
           'HALT-USDT': [[str(int(r[0]) - 10 * DAY), *r[1:]] for r in daily(lambda i: 10 * math.exp(-.01 * i))]}


def market(extra_inst=(), extra_tickers=()):
    instruments = [inst(p) for p in [*PATHS, 'NEW-USDT', 'HALT-USDT']] + list(extra_inst)
    tickers = [ticker(p, 10, 5e6) for p in [*PATHS, 'NEW-USDT', 'HALT-USDT']] + list(extra_tickers)
    def handler(req):
        path, q = req.url.path, dict(req.url.params)
        if path.endswith('/public/instruments'): data = instruments
        elif path.endswith('/market/tickers'): data = tickers
        elif path.endswith('/market/candles'):
            assert q['bar'] == '1D'
            data = CANDLES[q['instId']]
        else: return httpx.Response(404)
        return httpx.Response(200, json={'code': '0', 'data': data})
    return OkxClient(transport=httpx.MockTransport(handler))


def test_hard_filter_excludes_untradable_pegged_and_illiquid():
    instruments = [inst('AAA-USDT'), inst('ETH-BTC', quote='BTC'), inst('OFF-USDT', state='suspend'),
                   inst('PRE-USDT', rule='pre_market'), inst('USDC-USDT'), inst('BTC3L-USDT'), inst('GHOST-USDT'),
                   inst('THIN-USDT'), inst('WIDE-USDT'), inst('XNVDA-USDT', category='3')]
    tickers = [ticker('AAA-USDT', 10, 5e6), ticker('THIN-USDT', 10, 5e4), ticker('WIDE-USDT', 10, 5e6, spread=.01),
               ticker('USDC-USDT', 1, 1e9), ticker('BTC3L-USDT', 1, 1e9), ticker('OFF-USDT', 1, 1e9), ticker('PRE-USDT', 1, 1e9), ticker('XNVDA-USDT', 180, 1e8)]
    rows, excluded = hard_filter(instruments, tickers, DEFAULTS)
    assert [r['pair'] for r in rows] == ['AAA-USDT']
    assert excluded == {'非 USDT 现货': 1, '暂不可交易': 2, '稳定币或杠杆代币': 2, '股票代币等非加密资产': 1, '无成交或盘口不全': 1, '成交额不足': 1, '价差过大': 1}
    assert rows[0]['market_cap'] is None  # left for a market-data provider


def test_metrics_follow_daily_closes():
    m = metrics(daily(lambda i: 100 * .99 ** i)[1:], btc_mom28=0)
    assert m['mom7_pct'] == pytest.approx((1 / .99 ** 7 - 1) * 100, abs=.01)
    assert m['mom28_pct'] == m['rel_btc28_pct'] and m['trend_up'] and m['age_days'] == 100


def test_scan_ranks_survivors_and_explains_exclusions():
    progress = []
    result = run(scan(market(), pace=0, progress=lambda done, total: progress.append((done, total))))
    assert result['universe'] == ['AAA-USDT', 'BTC-USDT', 'BBB-USDT']
    assert result['excluded'] == {'上线时间太短': 1, '波动过大': 1, '日线中断': 1}
    top = result['rows'][0]
    assert top['rank'] == 1 and top['trend_up'] and top['rel_btc28_pct'] > 0 and 0 <= top['score'] <= 100
    assert progress[-1] == (6, 6)


def test_scan_caps_candidate_count_by_volume():
    result = run(scan(market(), {'max_candidates': 2}, pace=0))
    assert result['scanned'] == 2 and result['excluded']['超出扫描上限'] == 4


def test_order_whitelist_is_the_passed_universe():
    body = {'instId': 'AAA-USDT', 'tdMode': 'cash', 'side': 'buy', 'ordType': 'market', 'sz': '10'}
    sent = []
    def handler(req): sent.append(req); return httpx.Response(200, json={'code': '0', 'data': [{'ordId': '1', 'sCode': '0'}]})
    creds = OkxCredentials('key', 'secret', 'pass')
    with pytest.raises(OkxError):
        run(OkxClient(creds, transport=httpx.MockTransport(handler)).place_order(body, write_permit=True))
    with pytest.raises(OkxError):  # malformed ids never enter the whitelist
        run(OkxClient(creds, transport=httpx.MockTransport(handler), pairs={'aaa-usdt'}).place_order({**body, 'instId': 'aaa-usdt'}, write_permit=True))
    assert not sent
    run(OkxClient(creds, transport=httpx.MockTransport(handler), pairs={'AAA-USDT'}).place_order(body, write_permit=True))
    assert len(sent) == 1


def test_min_age_fits_one_page_of_daily_candles():
    from pydantic import ValidationError
    from lab.models import ScanIn
    with pytest.raises(ValidationError):
        ScanIn(min_age_days=91)  # 100 daily candles hold at most 99 closed bars
    assert ScanIn().min_age_days == 90


def test_market_client_reads_the_real_market_and_cannot_trade():
    from lab.okx import market_client
    seen = []
    def handler(req):
        seen.append(req.headers)
        return httpx.Response(200, json={'code': '0', 'data': [{'instId': 'BTC-USDT', 'last': '1', 'open24h': '1'}]})
    client = market_client(httpx.MockTransport(handler))
    run(client.tickers())
    assert seen and 'x-simulated-trading' not in seen[0]  # demo-environment volumes would mislead the filters
    body = {'instId': 'BTC-USDT', 'tdMode': 'cash', 'side': 'buy', 'ordType': 'market', 'sz': '10'}
    with pytest.raises(OkxError):
        run(client.place_order(body, write_permit=True))
    assert len(seen) == 1
