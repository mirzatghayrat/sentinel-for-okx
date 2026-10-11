import time
import httpx
import pytest
from fastapi.testclient import TestClient
from lab.app import create_app
from lab.engine import Engine
from lab.storage import Store
from test_safety import FakeClient, plan, run
from test_scanner import market as scanner_market, CANDLES, PATHS


def universe(store, pairs, age=0):
    rows = [{'pair': p, 'rank': i, 'score': 90 - i} for i, p in enumerate(pairs, 1)]
    store.set('scanner', {'as_of': time.time() - age, 'rows': rows, 'universe': list(pairs), 'listed': 700})


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path); fake = FakeClient()
    return store, fake, Engine(store, client_factory=lambda mode: fake)


def test_trial_pair_must_be_in_scanner_universe(desk):
    store, fake, engine = desk
    with pytest.raises(ValueError):
        run(engine.start(plan(pair='SOL-USDT')))
    universe(store, ['SOL-USDT'])
    assert run(engine.start(plan(pair='SOL-USDT')))['pair'] == 'SOL-USDT'


def test_coin_dropped_from_universe_can_still_be_sold_not_bought(desk):
    store, fake, engine = desk
    universe(store, ['SOL-USDT']); fake.fill = {'feeCcy': 'SOL'}; fake.account['details'].append({'ccy': 'SOL', 'availBal': '10'})
    run(engine.start(plan(pair='SOL-USDT'))); run(engine.tick('demo'))  # buy signal from the fake candles
    assert fake.posts[-1]['instId'] == 'SOL-USDT' and fake.posts[-1]['side'] == 'buy'
    run(engine.tick('demo'))  # reconcile the fill
    assert engine.session('demo')['quantity'] > 0
    universe(store, [])  # SOL drops out of the filtered universe
    assert 'SOL-USDT' in engine.tradable_pairs() and 'SOL-USDT' not in engine.buyable_pairs()
    state = engine.session('demo'); state['last_candle'] -= 3600000; engine.save('demo', state)
    run(engine.tick('demo'))
    assert len(fake.posts) == 1 and '不再买入' in engine.session('demo')['reason']
    run(engine.flatten('demo', ''))
    assert fake.posts[-1]['side'] == 'sell' and fake.posts[-1]['instId'] == 'SOL-USDT'


def test_pending_order_records_its_pair(desk):
    store, fake, engine = desk
    run(engine.start(plan())); run(engine.tick('demo'))
    assert engine.session('demo')['pending']['pair'] == 'BTC-USDT'


@pytest.mark.parametrize('pair', ['btc-usdt', 'BTC-USDC', 'BTC/USDT', '-USDT', 'A' * 21 + '-USDT'])
def test_malformed_pairs_are_rejected(pair):
    with pytest.raises(Exception):
        plan(pair=pair)


def test_scanner_endpoint_refreshes_and_refuses_overlap(tmp_path):
    store = Store(tmp_path)
    app = create_app(store, allow_live=False, background=False, market_transport=scanner_market().transport)
    headers = {'X-Desk-Request': '1'}
    with TestClient(app) as client:
        client.post('/api/login', json={'code': (tmp_path / 'access-code').read_text()}, headers=headers)
        assert client.post('/api/scanner', json={'min_age_days': 90}, headers=headers).status_code == 200
        for _ in range(50):
            summary = client.get('/api/status').json()['scanner']
            if not summary['running']: break
            time.sleep(.05)
        status = client.get('/api/status').json()
        assert status['scanner']['count'] == 3 and status['tradable'][:3] == ['AAA-USDT', 'BTC-USDT', 'BBB-USDT']
        assert 'ETH-USDT' in status['tradable']  # defaults stay available
        result = client.get('/api/scanner').json()['result']
        assert result['rows'][0]['pair'] == 'AAA-USDT' and result['excluded'] == {'上线时间太短': 1, '波动过大': 1, '日线中断': 1}
        assert client.post('/api/scanner', json={'min_age_days': 30}, headers=headers).status_code == 422


def test_trial_holding_a_dropped_coin_can_resume_for_exits(desk):
    store, fake, engine = desk
    universe(store, ['SOL-USDT']); fake.fill = {'feeCcy': 'SOL'}; fake.account['details'].append({'ccy': 'SOL', 'availBal': '10'})
    run(engine.start(plan(pair='SOL-USDT'))); run(engine.tick('demo')); run(engine.tick('demo'))
    run(engine.pause('demo')); universe(store, [])
    assert run(engine.start(plan(pair='SOL-USDT')))['running']  # loss line keeps guarding the holding
    state = engine.session('demo'); state['last_candle'] -= 3600000; engine.save('demo', state)
    run(engine.tick('demo'))
    assert len(fake.posts) == 1 and '不再买入' in engine.session('demo')['reason']
