import time
import httpx
import pytest
from pydantic import ValidationError
from lab.engine import Engine
from lab.grid import MAX_NEW_PER_TICK, open_orders
from lab.models import Plan
from lab.okx import OkxClient, OkxCredentials, OkxError
from lab.storage import Store
from test_safety import run


def grid_plan(**kw):
    return Plan(**{'budget': 1000, 'max_loss': 200, 'strategy': 'grid', 'grid_step_pct': 2, 'grid_levels': 5,
                   'max_orders_day': 100, 'hours': 72, **kw})


class Book:
    """OKX spot orders for one pair: post-only orders rest until a test fills or cancels them."""
    def __init__(self, store=None):
        self.last, self.orders, self.posts, self.cancels, self.store = 100., {}, [], [], store
        self.error, self.seen_at_post = None, []

    async def instrument(self, pair): return {'lotSz': '0.0001', 'minSz': '0.0001', 'tickSz': '0.01', 'state': 'live'}
    async def ticker(self, pair): return {'price': self.last, 'timestamp': int(time.time() * 1000)}
    async def balance(self): return {'details': [{'ccy': 'USDT', 'availBal': '100000'}, {'ccy': 'BTC', 'availBal': '1000'}]}

    async def place_order(self, body, *, write_permit=False):
        assert write_permit
        self.posts.append(body)
        if self.store:  # what the engine had saved when the request left
            self.seen_at_post.append(self.store.get('session:demo'))
        if self.error:
            raise self.error
        row = {'instId': body['instId'], 'clOrdId': body['clOrdId'], 'ordId': str(len(self.posts)), 'side': body['side'],
               'px': body.get('px'), 'sz': body['sz'], 'state': 'live', 'accFillSz': '0', 'avgPx': '', 'fee': '0', 'feeCcy': 'USDT'}
        self.orders[body['clOrdId']] = row
        if body['ordType'] == 'market':
            qty = float(body['sz'])
            row.update(state='filled', accFillSz=body['sz'], avgPx=str(self.last), fee=str(-qty * self.last * .001))
        return {'ordId': row['ordId']}

    async def pending_orders(self, pair):
        return [r for r in self.orders.values() if r['state'] in ('live', 'partially_filled')]

    async def order(self, pair, client_id):
        if client_id not in self.orders:
            raise OkxError('not found', code='51603')
        return self.orders[client_id]

    async def cancel_order(self, pair, client_id, *, write_permit=False):
        assert write_permit
        self.cancels.append(client_id)
        row = self.orders.get(client_id)
        if row and row['state'] in ('live', 'partially_filled'):
            row['state'] = 'canceled'
        return [{'clOrdId': client_id, 'sCode': '0'}]

    def fill(self, row, qty=None):
        qty = float(row['sz']) if qty is None else qty
        px = float(row['px'])
        fee = (str(-qty * .0008), 'BTC') if row['side'] == 'buy' else (str(-qty * px * .0008), 'USDT')
        row.update(state='filled' if qty == float(row['sz']) else 'partially_filled', accFillSz=str(qty), avgPx=str(px),
                   fee=fee[0], feeCcy=fee[1])

    def live(self, side=None):
        return [r for r in self.orders.values() if r['state'] == 'live' and (side is None or r['side'] == side)]


@pytest.fixture
def desk(tmp_path):
    store = Store(tmp_path)
    book = Book(store)
    return store, book, Engine(store, client_factory=lambda mode: book)


def test_grid_rests_post_only_buys_below_price_after_recording_each_intent(desk):
    store, book, engine = desk
    state = run(engine.start(grid_plan()))
    assert [lv['buy_px'] for lv in state['grid']['levels']] == ['98', '96', '94', '92', '90']
    run(engine.tick('demo'))
    assert len(book.posts) == min(MAX_NEW_PER_TICK, 5)
    assert all(b['ordType'] == 'post_only' and float(b['px']) < book.last and b['side'] == 'buy' for b in book.posts)
    for body, saved in zip(book.posts, book.seen_at_post):
        recorded = [lv['order'] for lv in saved['grid']['levels'] if lv['order']]
        assert body['clOrdId'] in [o['client_id'] for o in recorded]  # durable intent before the POST
    assert float(book.posts[0]['sz']) * float(book.posts[0]['px']) <= 1000 / 5


def test_filled_buy_is_offered_back_one_step_up_and_a_round_trip_books_profit(desk):
    store, book, engine = desk
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    first = next(r for r in book.live('buy') if r['px'] == '98')
    book.last = 98.  # the buy filled because the price came down to it
    book.fill(first)
    run(engine.tick('demo'))
    state = engine.session('demo')
    sell = book.live('sell')
    assert len(sell) == 1 and sell[0]['px'] == '99.96' and float(sell[0]['sz']) <= float(first['sz'])
    assert state['quantity'] > 0 and state['cash'] < 1000
    book.last = 100.
    book.fill(sell[0])
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert state['grid']['round_trips'] == 1 and state['cash'] > 1000  # bought 98, sold 99.96, net of maker fees
    assert any(r['px'] == '98' and r['state'] == 'live' for r in book.orders.values())  # level buys again


def test_pause_takes_every_order_down_and_keeps_holdings(desk):
    store, book, engine = desk
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    book.fill(book.live('buy')[0], qty=.5)  # partial fill, still resting
    run(engine.pause('demo')); run(engine.tick('demo'))
    assert not book.live() and len(book.cancels) == 5
    with pytest.raises(ValueError):
        run(engine.finish('demo'))  # cancellations not yet confirmed by a lookup
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert not open_orders(state) and state['quantity'] == pytest.approx(.5 - .5 * .0008)
    run(engine.flatten('demo', ''))
    assert book.posts[-1]['ordType'] == 'market' and book.posts[-1]['side'] == 'sell'


def test_unconfirmed_submission_pauses_and_is_only_looked_up_never_resent(desk):
    store, book, engine = desk
    run(engine.start(grid_plan()))
    book.error = OkxError('timeout', code='network')
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert len(book.posts) == 1 and not state['running'] and '不会重发' in state['error']
    lost = open_orders(state)[0]['order']
    assert lost['status'] == 'uncertain'
    with pytest.raises(ValueError):
        run(engine.start(grid_plan()))
    # It had reached OKX after all: the next lookup by the same client ID adopts it, then cancels it.
    book.error = None
    book.orders[lost['client_id']] = {'instId': 'BTC-USDT', 'clOrdId': lost['client_id'], 'ordId': '9', 'side': 'buy',
                                      'px': lost['px'], 'sz': lost['sz'], 'state': 'live', 'accFillSz': '0'}
    run(engine.tick('demo'))
    assert book.cancels == [lost['client_id']] and len(book.posts) == 1


def test_rejected_order_pauses_without_leaving_a_phantom(desk):
    store, book, engine = desk
    run(engine.start(grid_plan()))
    book.error = OkxError('rejected', code='51008')
    run(engine.tick('demo'))
    state = engine.session('demo')
    assert not state['running'] and not open_orders(state) and '51008' in state['error']


def test_loss_line_cancels_then_sells_at_market(desk):
    store, book, engine = desk
    run(engine.start(grid_plan(max_loss=30))); run(engine.tick('demo'))
    for row in book.live('buy'):
        book.fill(row)
    book.last = 80.
    run(engine.tick('demo'))  # reconciles fills, crosses the loss line, sends cancels
    state = engine.session('demo')
    assert state['risk_stopped'] and not state['running']
    run(engine.tick('demo'))  # cancellations settled -> market exit
    assert book.posts[-1]['ordType'] == 'market' and book.posts[-1]['side'] == 'sell'
    run(engine.tick('demo'))  # reconcile the exit
    state = engine.session('demo')
    assert state['quantity'] < .0001 and not state['pending']
    run(engine.finish('demo'))


def test_expiry_cancels_orders(desk):
    store, book, engine = desk
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    state = engine.session('demo'); state['expires'] = time.time() - 1; engine.save('demo', state)
    run(engine.tick('demo'))
    assert not engine.session('demo')['running'] and len(book.cancels) == 5


def test_flat_grid_moves_up_with_the_price(desk):
    store, book, engine = desk
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    run(engine.pause('demo')); run(engine.tick('demo')); run(engine.tick('demo'))
    book.last = 110.
    run(engine.start(grid_plan()))
    assert engine.session('demo')['grid']['anchor'] == 110.


def test_grid_cannot_switch_strategy_or_change_spacing_while_holding(desk):
    store, book, engine = desk
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    book.fill(book.live('buy')[0])
    run(engine.tick('demo')); run(engine.pause('demo')); run(engine.tick('demo')); run(engine.tick('demo'))
    with pytest.raises(ValueError):
        run(engine.start(grid_plan(grid_step_pct=3)))
    with pytest.raises(ValueError):
        run(engine.start(Plan(budget=1000, max_loss=200)))
    assert run(engine.start(grid_plan()))['running']


def test_live_grid_still_needs_host_optin_and_confirmation(tmp_path):
    engine = Engine(Store(tmp_path), client_factory=lambda mode: Book())
    with pytest.raises(ValueError):
        run(engine.start(grid_plan(mode='live', confirmation='我自行启用实盘')))


def test_plan_bounds():
    with pytest.raises(ValidationError):
        grid_plan(budget=40, max_loss=10, grid_levels=10)  # 4 USDT per level
    with pytest.raises(ValidationError):
        grid_plan(max_orders_day=5)
    with pytest.raises(ValidationError):
        Plan(budget=500, max_loss=25, hours=48)  # only the grid may run past 24 hours
    assert grid_plan(hours=168).hours == 168


def client(seen):
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={'code': '0', 'data': [{'ordId': '1', 'sCode': '0', 'clOrdId': 'og1'}]})
    return OkxClient(OkxCredentials('key', 'secret', 'pass'), transport=httpx.MockTransport(handler), pairs={'BTC-USDT'})


@pytest.mark.parametrize('body', [
    {'ordType': 'limit', 'px': '90', 'sz': '1'},                       # plain limit could take liquidity
    {'ordType': 'post_only', 'sz': '1'},                               # no price
    {'ordType': 'post_only', 'px': '-1', 'sz': '1'},
    {'ordType': 'post_only', 'px': '90', 'sz': '1', 'tgtCcy': 'quote_ccy'},
    {'ordType': 'market', 'px': '90', 'sz': '1'},
    {'ordType': 'post_only', 'px': '90', 'sz': '1', 'reduceOnly': True},
])
def test_order_whitelist_rejects_anything_but_market_or_post_only(body):
    seen = []
    with pytest.raises(OkxError):
        run(client(seen).place_order({'instId': 'BTC-USDT', 'tdMode': 'cash', 'side': 'buy', 'clOrdId': 'og1', **body},
                                     write_permit=True))
    assert not seen


def test_post_only_and_cancel_by_client_id_are_allowed():
    seen = []
    c = client(seen)
    run(c.place_order({'instId': 'BTC-USDT', 'tdMode': 'cash', 'side': 'buy', 'ordType': 'post_only', 'px': '90.5',
                       'sz': '0.01', 'clOrdId': 'og1'}, write_permit=True))
    run(c.cancel_order('BTC-USDT', 'og1', write_permit=True))
    assert [r.url.path for r in seen] == ['/api/v5/trade/order', '/api/v5/trade/cancel-order']
    with pytest.raises(OkxError):
        run(c.request('POST', '/trade/cancel-order', body={'instId': 'BTC-USDT', 'ordId': '1'}, write_permit=True))
    with pytest.raises(OkxError):
        run(c.cancel_order('BTC-USDT', 'og1'))  # no write permit
    with pytest.raises(OkxError):
        run(c.cancel_order('ETH-USDT', 'og1', write_permit=True))  # outside the tradable set


def hourly(prices):
    end = int(time.time() // 3600) * 3600000 - 3600000
    n = len(prices)
    return [[str(end - (n - 1 - i) * 3600000), str(p), str(p * 1.001), str(p * .999), str(p), '1', '1', '1', '1']
            for i, p in enumerate(prices)]


def test_grid_backtest_earns_on_a_range_and_stops_on_a_slide():
    import math
    from lab.models import ResearchIn
    from lab.research import grid_backtest
    spec = ResearchIn(strategy='grid', budget=1000, max_loss=100, grid_levels=5)
    wave = grid_backtest(hourly([100 * (1 + .05 * math.sin(i / 8)) for i in range(600)]), spec)
    assert wave['holdout']['round_trips'] > 0 and wave['holdout']['return_pct'] > 0
    slide = grid_backtest(hourly([100 * .997 ** i for i in range(600)]), spec)
    assert slide['development']['stopped'] and slide['development']['return_pct'] > -15
