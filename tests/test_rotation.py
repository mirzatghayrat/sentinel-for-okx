import json
import time
import httpx
import pytest
from lab.engine import Engine
from lab.llm import OpenRouterClient, TypeSafeClient, rotation_snapshot
from lab.storage import Store
from test_safety import FakeClient, bars, plan, run

FLAT = [[r[0], '100', '100', '100', '100', *r[5:]] for r in bars(120)]  # RSI 50: hold


class RotFake(FakeClient):
    """FakeClient with per-pair hourly candles for rotation candidates."""
    def __init__(self, hourly):
        super().__init__(); self.hourly = hourly; self.recent_calls = []
        self.fill = {'feeCcy': 'USDT', 'fee': '-0.02'}
        self.account['details'] += [{'ccy': p.split('-')[0], 'availBal': '10'} for p in hourly]
    async def recent_candles(self, pair, *, bar='1H', limit=100):
        self.recent_calls.append(pair); return self.hourly[pair]


class Chooser:
    def __init__(self, answer): self.answer = answer; self.calls = []
    async def has_model(self, model): return True
    async def choose(self, model, view, pairs):
        self.calls.append((view, pairs))
        if isinstance(self.answer, Exception): raise self.answer
        return {'confidence': 70, 'reason': 'x', 'cost': 0., 'seconds': .1, **self.answer}


def radar(store, pairs, age=0):
    store.set('scanner', {'as_of': time.time() - age, 'universe': list(pairs), 'listed': 700,
                          'rows': [{'pair': p, 'rank': i, 'score': 90 - i, 'mom7_pct': 1., 'mom28_pct': 2.}
                                   for i, p in enumerate(pairs, 1)]})


def rot(**kw): return plan(pair_mode='rotate', **kw)


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path); fake = RotFake({'AAA-USDT': FLAT, 'BBB-USDT': bars(120), 'CCC-USDT': bars(120)})
    radar(store, ['AAA-USDT', 'BBB-USDT', 'CCC-USDT'])
    return store, fake, Engine(store, client_factory=lambda mode: fake)


def test_flat_rotation_buys_first_ranked_coin_with_a_buy_signal(desk):
    _, fake, engine = desk
    state = run(engine.start(rot()))
    assert state['pair'] is None
    run(engine.tick('demo'))
    assert [p['instId'] for p in fake.posts] == ['BBB-USDT'] and fake.posts[0]['side'] == 'buy'
    assert engine.session('demo')['pair'] == 'BBB-USDT' and fake.recent_calls == ['AAA-USDT', 'BBB-USDT', 'CCC-USDT']
    run(engine.tick('demo')); assert engine.session('demo')['quantity'] > 0  # reconciled on BBB


def test_rotation_needs_a_fresh_scanner(desk):
    store, fake, engine = desk
    radar(store, ['BBB-USDT'], age=4 * 3600)
    with pytest.raises(ValueError): run(engine.start(rot()))
    radar(store, ['BBB-USDT']); run(engine.start(rot()))
    radar(store, ['BBB-USDT'], age=4 * 3600); run(engine.tick('demo'))
    assert not fake.posts and '3 小时' in engine.session('demo')['reason']


def test_holding_coin_is_managed_without_rescanning(desk):
    _, fake, engine = desk
    run(engine.start(rot()))
    state = engine.session('demo'); state.update(pair='AAA-USDT', quantity=.2, cash=480); engine.save('demo', state)
    run(engine.tick('demo'))
    assert not fake.recent_calls and fake.posts[0]['instId'] == 'AAA-USDT'  # fake candles say buy more of AAA


def test_sold_down_coin_becomes_recorded_dust_then_rotates(desk):
    _, fake, engine = desk
    run(engine.start(rot()))
    state = engine.session('demo'); state.update(pair='AAA-USDT', quantity=3e-9, cash=499); engine.save('demo', state)
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert state['dust_left'] == {'AAA-USDT': 3e-9} and fake.posts[0]['instId'] == 'BBB-USDT'


def test_model_picks_among_candidates_only(desk):
    store, fake, engine = desk
    store.save_secret('openrouter', 'sk-or-test')
    chooser = Chooser({'side': 'buy', 'pair': 'CCC-USDT'})
    engine.adviser_factory = lambda provider: chooser
    run(engine.start(rot(strategy='llm', llm_model='x-ai/test'))); run(engine.tick('demo'))
    view, pairs = chooser.calls[0]
    assert pairs == ['AAA-USDT', 'BBB-USDT', 'CCC-USDT'] and [c['pair'] for c in view['candidates']] == pairs
    assert fake.posts[0]['instId'] == 'CCC-USDT' and engine.session('demo')['pair'] == 'CCC-USDT'


@pytest.mark.parametrize('answer', [{'side': 'buy', 'pair': 'ZZZ-USDT'}, {'side': 'hold', 'pair': None}, RuntimeError('down')])
def test_off_list_hold_or_failed_model_answer_buys_nothing(desk, answer):
    store, fake, engine = desk
    store.save_secret('openrouter', 'sk-or-test')
    engine.adviser_factory = lambda provider: Chooser(answer)
    run(engine.start(rot(strategy='llm', llm_model='x-ai/test'))); run(engine.tick('demo'))
    assert not fake.posts and engine.session('demo')['running'] and engine.session('demo')['pair'] is None


def test_flat_rotation_loss_line_stops_without_buying(desk):
    _, fake, engine = desk
    run(engine.start(rot()))
    state = engine.session('demo'); state['cash'] = 470; engine.save('demo', state)
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert not fake.posts and state['risk_stopped'] and not state['running']


def test_rotation_snapshot_carries_market_data_only():
    state = {'plan': rot().model_dump(), 'pair': None, 'price': 0., 'cash': 500., 'quantity': 0., 'days': {}}
    view = rotation_snapshot([({'pair': 'BBB-USDT', 'rank': 1, 'score': 80}, bars(120))], state)
    assert view['candidates'][0]['pair'] == 'BBB-USDT' and len(view['candidates'][0]['closes_24h']) == 24
    assert view['trial']['position_qty'] == 0 and 'key' not in json.dumps(view).lower()


def test_openrouter_choice_is_case_insensitive_and_limited_to_candidates():
    def reply(decision):
        content = json.dumps({'decision': decision, 'confidence': 60, 'reason': 'r'})
        return httpx.MockTransport(lambda req: httpx.Response(200, json={'choices': [{'message': {'content': content}}]}))
    pick = run(OpenRouterClient('k' * 8, transport=reply('bbb-usdt')).choose('x/y', {}, ['AAA-USDT', 'BBB-USDT']))
    assert pick['side'] == 'buy' and pick['pair'] == 'BBB-USDT'
    miss = run(OpenRouterClient('k' * 8, transport=reply('ZZZ-USDT')).choose('x/y', {}, ['AAA-USDT']))
    assert miss['side'] == 'hold' and miss['pair'] is None


def test_jev_rotation_offers_each_candidate_plus_hold():
    seen = []
    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={'model': 'jev', 'usage': {'input_tokens': 5, 'output_tokens': 1},
                                         'answers': {'action': {'type': 'choice', 'choice': 'AAA-USDT', 'confidence': .6,
                                                                'probabilities': {'AAA-USDT': .6, 'hold': .4}}}})
    pick = run(TypeSafeClient('ts', transport=httpx.MockTransport(handler)).choose('jev-latest', {}, ['AAA-USDT', 'BBB-USDT']))
    assert set(seen[0]['questions']['action']['criteria']) == {'AAA-USDT', 'BBB-USDT', 'hold'}
    assert pick['side'] == 'buy' and pick['pair'] == 'AAA-USDT' and 'AAA-USDT 60%' in pick['reason']


def test_rotation_holding_a_coin_resumes_without_a_fresh_scanner(desk):
    store, fake, engine = desk
    run(engine.start(rot())); run(engine.pause('demo'))
    state = engine.session('demo'); state.update(pair='AAA-USDT', quantity=.2, cash=480); engine.save('demo', state)
    radar(store, ['AAA-USDT'], age=5 * 3600)
    assert run(engine.start(rot()))['running']
