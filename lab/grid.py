"""Maker grid: resting post-only limit orders on one coin, buying a step below and selling a step above.

Levels sit below an anchor price, one budget slice each. A filled buy becomes a sell one step up;
a filled sell frees the level to buy again. When the grid holds nothing and the price has risen a
step above the anchor, the grid moves up to the new price instead of waiting below it.

The engine's rules carry over: every order is recorded before it is sent; a submission whose outcome
is unknown pauses the trial and is only ever looked up again by the same client ID, never resent;
pausing, expiry, restarts and errors take every resting order down; the loss line takes orders down
and then sells the trial's holding at market. A filled order counts only once OKX reports it terminal.
"""
from __future__ import annotations
import math
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from .okx import OkxError

MAX_NEW_PER_TICK = 6     # pace new orders; the rest follow on the next check a minute later
EDGE = .0005             # keep resting orders at least this far from the last price so post-only rests
LOST_AFTER = 120         # seconds before an accepted order missing from OKX freezes the trial


def to_tick(price, tick, up=False):
    step = Decimal(str(tick))
    if step <= 0:
        raise ValueError('无效价格精度')
    units = (Decimal(str(price)) / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return _plain(units * step)


def lot(value, size):
    step = Decimal(str(size))
    units = (Decimal(str(value)) / step).to_integral_value(rounding=ROUND_FLOOR)
    return _plain(units * step)


def _plain(d):
    text = format(d, 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def build(anchor, plan, instrument):
    # Decimal arithmetic so a price on the tick grid is not nudged a tick up or down by float error.
    step, tick = Decimal(str(plan['grid_step_pct'])) / 100, instrument['tickSz']
    levels = []
    for k in range(1, plan['grid_levels'] + 1):
        buy = to_tick(Decimal(str(anchor)) * (1 - step * k), tick)
        if Decimal(buy) <= 0:
            break
        levels.append({'k': k, 'buy_px': buy, 'sell_px': to_tick(Decimal(buy) * (1 + step), tick, up=True),
                       'qty': 0., 'order': None})
    if len(levels) < 2:
        raise ValueError('网格价格太低，无法按当前格距布置')
    return {'anchor': anchor, 'levels': levels, 'round_trips': 0, 'built': time.time()}


def definitive(exc):
    """OKX parsed the request and said no: nothing rests on the book. System errors stay uncertain."""
    code = getattr(exc, 'code', '')
    return isinstance(exc, OkxError) and code.isdigit() and not code.startswith('500')


def open_orders(state):
    return [lv for lv in (state.get('grid') or {}).get('levels', []) if lv['order']]


def held(state):
    return sum(lv['qty'] for lv in state['grid']['levels'])


class GridMixin:
    """Grid behaviour for Engine; relies on its submit/save/log helpers and runs under its lock."""

    async def grid_tick(self, mode, state, client, now):
        plan, grid = state['plan'], state['grid']
        pair = plan['pair']
        instrument = await client.instrument(pair)
        if held(state) > state['quantity'] + 1e-12:
            # A market exit sold the levels' coin; what is left is unsellable dust on the ledger.
            for lv in grid['levels']:
                lv['qty'] = 0.
        await self.grid_reconcile(mode, state, client, instrument, now)
        if state['running'] and now >= state['expires']:
            state['running'] = False
            state['reason'] = '运行期限已到；撤下全部挂单，持仓保留'
            self.log(mode, 'pause', state['reason'])
        price = state['price'] = (await client.ticker(pair))['price']
        state['last_tick'] = now
        state['equity'] = state['cash'] + state['quantity'] * price
        state['peak'] = max(state['peak'], state['equity'])
        if state['running'] and plan['budget'] - state['equity'] >= plan['max_loss']:
            state['running'], state['risk_stopped'] = False, True
            state['reason'] = '已触及亏损触发线：撤下全部挂单，然后市价卖出本策略持仓'
            self.log(mode, 'risk', state['reason'])
        if not state['running']:
            await self.grid_cancel_all(mode, state, client, now)
            # Orders whose submission is unconfirmed cannot be cancelled with certainty; they do not hold the
            # loss exit back. If one did rest and locked coin, the balance check in submit stops the sale.
            settled = all(lv['order']['status'] in ('uncertain', 'missing') for lv in open_orders(state))
            if state.get('risk_stopped') and settled and not state['pending'] and state['quantity'] > 0:
                if float(lot(state['quantity'], instrument['lotSz'])) >= float(instrument['minSz']):
                    for lv in grid['levels']:
                        lv['qty'] = 0.
                    await self.submit(mode, state, client, 'sell', price, instrument, await client.balance(),
                                      '触及亏损线，市价卖出网格持仓', explicit_exit=True)
            return
        step = plan['grid_step_pct'] / 100
        if not open_orders(state) and held(state) <= 0 and price >= grid['anchor'] * (1 + step):
            state['grid'] = grid = {**build(price, plan, instrument), 'round_trips': grid['round_trips']}
            self.log(mode, 'decision', f"{pair} 网格空仓且价格上移，基准价调整到 {price:g}")
        await self.grid_place(mode, state, client, instrument, price, now)

    async def grid_reconcile(self, mode, state, client, instrument, now):
        waiting = open_orders(state)
        if not waiting:
            return
        pair = state['plan']['pair']
        live = {r.get('clOrdId'): r for r in await client.pending_orders(pair)}
        for lv in waiting:
            o = lv['order']
            row = live.get(o['client_id'])
            if row is None:
                try:
                    row = await client.order(pair, o['client_id'])
                except OkxError:
                    if o['status'] in ('uncertain', 'missing') or now - o['ts'] < LOST_AFTER:
                        continue  # keep asking for the same client ID; never resend under a new one
                    o['status'] = 'missing'
                    state['running'] = False
                    state['error'] = f"挂单 {o['client_id']} 在 OKX 查不到；已暂停，不会重发。请在 OKX 核对。"
                    self.log(mode, 'error', state['error'])
                    continue
            self.grid_apply(mode, state, lv, row, instrument)

    def grid_apply(self, mode, state, lv, row, instrument):
        o, pair = lv['order'], state['plan']['pair']
        if row.get('clOrdId') != o['client_id'] or row.get('instId') != pair or row.get('side') != o['side']:
            raise ValueError('挂单标识不一致，暂停对账')
        status = row.get('state')
        if status in ('live', 'partially_filled'):
            if o['status'] in ('uncertain', 'missing', 'submitting'):
                self.log(mode, 'fill', f"已在 OKX 查到挂单 {o['client_id']}，恢复跟踪")
                o['status'] = 'live'
            o['order_id'] = row.get('ordId')
            return
        if status not in ('filled', 'canceled', 'mmp_canceled'):
            return
        qty, px = float(row.get('accFillSz') or 0), float(row.get('avgPx') or 0)
        fee, rebate = float(row.get('fee') or 0), float(row.get('rebate') or 0)
        if not all(math.isfinite(x) for x in (qty, px, fee, rebate)) or qty < 0 or (qty > 0 and px <= 0):
            raise ValueError('挂单成交数据无效，暂停对账')
        base = pair.split('-')[0]
        sign = 1 if o['side'] == 'buy' else -1
        cash, units = state['cash'] - sign * qty * px, sign * qty
        for amount, ccy in ((fee, row.get('feeCcy')), (rebate, row.get('rebateCcy'))):
            if amount == 0:
                continue
            if ccy == 'USDT': cash += amount
            elif ccy == base: units += amount
            else: raise ValueError('手续费币种不受支持，请人工核对')
        state['cash'] = cash
        state['quantity'] = max(0., state['quantity'] + units)
        lv['qty'] = max(0., lv['qty'] + units)
        lv['order'] = None
        if qty > 0:
            if o['side'] == 'sell' and status == 'filled':
                state['grid']['round_trips'] += 1
            state['orders'] = (state['orders'] + [{
                'id': row.get('ordId'), 'client_id': o['client_id'], 'side': o['side'], 'pair': pair, 'quantity': qty,
                'price': px, 'fee': fee, 'fee_ccy': row.get('feeCcy'), 'state': status, 'ts': time.time(),
                'reason': f"网格第 {lv['k']} 格挂单{'买入' if o['side'] == 'buy' else '卖出'}"}])[-200:]
            state['reason'] = f"网格第 {lv['k']} 格{'买入' if o['side'] == 'buy' else '卖出'}成交 {qty:g} {base} @ {px:g}"
            self.log(mode, 'fill', state['reason'])
        if cash < -.01:
            state['running'] = False
            state['error'] = '账本现金出现负数，已暂停；请勿重置记录'

    async def grid_cancel_all(self, mode, state, client, now):
        pair = state['plan']['pair']
        for lv in open_orders(state):
            o = lv['order']
            if now - o.get('cancel_ts', 0) < 60:
                continue
            o['cancel_ts'] = now
            try:
                await client.cancel_order(pair, o['client_id'], write_permit=True)
            except OkxError as exc:
                # Already filled or cancelled orders refuse cancellation; the next lookup settles them.
                if not o.get('cancel_error'):
                    o['cancel_error'] = exc.message
                    self.log(mode, 'error', f"撤销挂单 {o['client_id']} 未成功：{exc.message} 将继续核对")

    async def grid_place(self, mode, state, client, instrument, price, now):
        plan = state['plan']
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        state['days'] = {k: v for k, v in state['days'].items() if k == day}
        lot_sz, min_sz = instrument['lotSz'], float(instrument['minSz'])
        reserved = sum(float(lv['order']['sz']) * float(lv['order']['px']) * 1.001
                       for lv in open_orders(state) if lv['order']['side'] == 'buy')
        spare = state['cash'] - reserved
        quote = plan['budget'] / plan['grid_levels'] / 1.002  # room for the maker fee in every slice
        balance = None
        placed = 0
        # Sells first so held coin is always offered back; then buys from the nearest level down.
        for lv in sorted(state['grid']['levels'], key=lambda lv: (lv['qty'] <= 0, lv['k'])):
            if placed >= MAX_NEW_PER_TICK or state['days'].get(day, 0) >= plan['max_orders_day'] or not state['running']:
                break
            if lv['order']:
                continue
            if lv['qty'] > 0 and float(lot(lv['qty'], lot_sz)) < min_sz:
                lv['qty'] = 0.  # unsellable remainder stays on the ledger as dust; the level may buy again
            if lv['qty'] > 0:
                size = lot(lv['qty'], lot_sz)
                side, px = 'sell', to_tick(max(float(lv['sell_px']), price * (1 + EDGE)), instrument['tickSz'], up=True)
            else:
                if float(lv['buy_px']) >= price * (1 - EDGE):
                    continue
                size = lot(quote / float(lv['buy_px']), lot_sz)
                cost = float(size) * float(lv['buy_px']) * 1.001
                if float(size) < min_sz or cost > spare:
                    continue
                if balance is None:
                    balance = await client.balance()
                    usdt = next((float(r.get('availBal') or 0) for r in balance.get('details', []) if r.get('ccy') == 'USDT'), 0.)
                if cost > usdt:
                    state['reason'] = 'OKX 可用 USDT 不足以挂出下一格买单；暂不挂新买单'
                    continue
                usdt -= cost
                spare -= cost
                side, px = 'buy', lv['buy_px']
            if not await self.grid_send(mode, state, client, lv, side, px, size, now, day):
                return
            placed += 1
        if placed:
            state['reason'] = f"网格运行中：本次新挂 {placed} 单，共 {len(open_orders(state))} 单挂在 OKX"
        elif state['running'] and not state['error']:
            state['reason'] = f"网格运行中：{len(open_orders(state))} 单挂在 OKX，等待成交"

    async def grid_send(self, mode, state, client, lv, side, px, size, now, day):
        pair = state['plan']['pair']
        client_id = 'og' + uuid.uuid4().hex[:28]
        body = {'instId': pair, 'tdMode': 'cash', 'side': side, 'ordType': 'post_only',
                'px': px, 'sz': size, 'clOrdId': client_id}
        lv['order'] = {'client_id': client_id, 'side': side, 'px': px, 'sz': size, 'ts': now, 'status': 'submitting'}
        state['days'][day] = state['days'].get(day, 0) + 1
        state['last_action'] = now
        self.save(mode, state)  # durable intent before the request leaves
        self.log(mode, 'intent', f"{pair} 网格第 {lv['k']} 格挂{'买' if side == 'buy' else '卖'} {size} @ {px}")
        try:
            result = await client.place_order(body, write_permit=True)
            lv['order']['order_id'] = result['ordId']
            lv['order']['status'] = 'live'
            self.save(mode, state)
            return True
        except Exception as exc:
            state['running'] = False
            if definitive(exc):
                lv['order'] = None
                state['error'] = f'OKX 未接受网格挂单（{exc.code}）；网格已暂停，请检查余额、权限或价格后再启动。'
            else:
                lv['order']['status'] = 'uncertain'
                detail = exc.message if isinstance(exc, OkxError) else ''
                state['error'] = detail + '网格挂单提交结果未确认，已暂停；不会重发，将继续按同一编号查询。'
            self.log(mode, 'error', state['error'])
            self.save(mode, state)
            return False
