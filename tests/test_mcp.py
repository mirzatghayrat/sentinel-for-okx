import json
import math
import os
import sys
import time
import httpx
import pytest
from lab.engine import Engine
from lab.mcp_server import create_server
from lab.okx import OkxCredentials
from lab.storage import Store
from test_grid import Book, grid_plan
from test_safety import run

TOOLS = {'market_snapshot', 'scanner_results', 'run_backtest', 'trial_status', 'recent_orders', 'recent_events'}


def market_transport():
    hour = int(time.time() // 3600) * 3600000
    def candle(ts):
        px = 100 * (1 + .05 * math.sin((hour - ts) / 3600000 / 8))
        return [str(ts), str(px), str(px * 1.001), str(px * .999), str(px), '1', '1', '1', '0' if ts == hour else '1']
    def handler(req):
        assert 'x-simulated-trading' not in req.headers and 'ok-access-key' not in req.headers
        path, q = req.url.path, dict(req.url.params)
        if path.endswith('/market/ticker'):
            rows = [{'last': '101', 'open24h': '100', 'ts': str(int(time.time() * 1000))}]
        elif path.endswith('candles'):
            first = int(q['after']) - 3600000 if 'after' in q else hour
            rows = [candle(first - i * 3600000) for i in range(int(q['limit']))]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={'code': '0', 'data': rows})
    return httpx.MockTransport(handler)


def call(server, name, **args):
    result = run(server.call_tool(name, args))
    data = result[1] if isinstance(result, tuple) else result
    return data.get('result', data) if isinstance(data, dict) else json.loads(data[0].text)


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path)
    store.save_credentials(OkxCredentials('api-key-marker', 'secret-marker', 'passphrase-marker'), 'demo')
    store.save_secret('openrouter', 'sk-or-marker')
    book = Book()
    engine = Engine(store, client_factory=lambda mode: book)
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    book.last = 98.; book.fill([r for r in book.live('buy') if r['px'] == '98'][0]); run(engine.tick('demo'))
    return tmp_path, create_server(tmp_path, market_transport=market_transport())


def test_only_read_only_tools_are_exposed(desk):
    _, server = desk
    tools = run(server.list_tools())
    assert {t.name for t in tools} == TOOLS
    assert all(t.annotations.readOnlyHint and not t.annotations.destructiveHint for t in tools)


def test_trial_status_reports_the_grid_without_secrets(desk):
    path, server = desk
    before = {p.name: p.stat().st_mtime_ns for p in path.iterdir()}
    status = call(server, 'trial_status', mode='demo')
    orders = call(server, 'recent_orders', mode='demo')
    events = call(server, 'recent_events', limit=50)
    trial = status['trial']
    assert trial['strategy'] == 'grid' and trial['running'] and trial['grid']['resting_orders'] == 5
    assert orders['orders'][0]['side'] == 'buy' and events['events'][0]['message']
    text = json.dumps([status, orders, events], ensure_ascii=False)
    for marker in ('api-key-marker', 'secret-marker', 'passphrase-marker', 'sk-or-marker'):
        assert marker not in text
    assert {p.name: p.stat().st_mtime_ns for p in path.iterdir()} == before  # read-only: nothing written
    assert call(server, 'trial_status', mode='live')['trial'] is None


def test_missing_data_dir_reads_empty_and_creates_nothing(tmp_path):
    server = create_server(tmp_path / 'absent')
    assert call(server, 'trial_status')['trial'] is None
    assert call(server, 'recent_events')['events'] == []
    assert call(server, 'scanner_results')['rows'] == []
    assert not (tmp_path / 'absent').exists()


def test_market_and_backtest_use_public_data(desk):
    _, server = desk
    snap = call(server, 'market_snapshot', pair='btc-usdt', hours=24)
    assert snap['pair'] == 'BTC-USDT' and len(snap['hourly_closes_utc']) == 24 and snap['change_24h_pct'] == 1.0
    result = call(server, 'run_backtest', pair='BTC-USDT', strategy='grid', grid_levels=5)
    assert result['holdout']['round_trips'] > 0 and 'note' in result
    with pytest.raises(Exception):
        run(server.call_tool('market_snapshot', {'pair': 'BTC/USDT'}))


def test_stdio_server_starts_and_answers(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    Store(tmp_path)
    params = StdioServerParameters(command=sys.executable, args=['-m', 'lab.mcp_server'],
                                   env={**os.environ, 'OKX_DESK_DATA': str(tmp_path)}, cwd=os.getcwd())
    async def session():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                names = {t.name for t in (await client.list_tools()).tools}
                result = await client.call_tool('trial_status', {'mode': 'demo'})
                return names, result
    names, result = run(session())
    assert names == TOOLS and not result.isError
