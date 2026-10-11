"""Single-process spot engine. Only explicit user start calls arm execution.

No retries on order submission. Persist intent before network I/O. Account fills
are reconciled before any further decision. Mode and budget never auto-switch.

A trial holds at most one coin. In fixed mode it is the chosen pair; in rotate mode a flat
trial may pick one of the scanner's top-ranked coins on a new hourly bar, and only switches
coin after the previous one is sold down to unsellable dust.
"""
from __future__ import annotations
import asyncio
import math
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from .okx import PAIR_RE, PAIRS, OkxClient, OkxError
from .strategy import signal
from .llm import ADVISERS, LABELS, PROVIDER_NAMES, LlmError, rotation_snapshot, snapshot

SCAN_MAX_AGE = 3*3600  # rotation only opens positions from a scanner result this fresh


def pair_of(state):
    """The coin this trial holds or trades now; None for a flat rotation trial."""
    if 'pair' in state:
        return state['pair']
    return state['plan'].get('pair') if state['plan'].get('pair_mode', 'fixed') == 'fixed' else None


def hourly_ts(candles, now):
    """Open time of the newest closed hourly bar, after freshness and continuity checks."""
    if len(candles) < 100:
        raise ValueError('已收盘行情不足 100 根')
    ts = max(int(r[0]) for r in candles)
    if not 3600000 <= now*1000-ts <= 7500000:
        raise ValueError('小时行情过期或时间异常，停止决策')
    stamps = sorted(int(r[0]) for r in candles[:100])
    if any(b-a != 3600000 for a,b in zip(stamps,stamps[1:])):
        raise ValueError('K 线不连续，停止决策')
    return ts


def initial_session(plan, now):
    return {'id': uuid.uuid4().hex[:10], 'plan': plan, 'running': False,
            'pair': plan['pair'] if plan.get('pair_mode', 'fixed') == 'fixed' else None, 'dust_left': {},
            'started': now, 'expires': 0, 'cash': plan['budget'], 'quantity': 0.,
            'price': 0., 'equity': plan['budget'], 'peak': plan['budget'],
            'last_candle': 0, 'last_tick': 0, 'last_action': 0, 'pending': None,
            'days': {}, 'orders': [], 'reason': '等待用户启动', 'error': '', 'risk_stopped': False}


def daily_cap_reached(state):
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return state['days'].get(day, 0) >= state['plan']['max_orders_day']


def available(balance, currency):
    for row in balance.get('details', []):
        if row.get('ccy') == currency:
            return float(row.get('availBal') or 0)
    return 0.


def floor_size(value, lot):
    step = Decimal(str(lot))
    if step <= 0:
        raise ValueError('无效交易精度')
    units = (Decimal(str(value))/step).to_integral_value(rounding=ROUND_DOWN)
    return format(units*step, 'f')


def apply_fill(state, order):
    """Exactly one terminal aggregate per pending intent; fee signs follow OKX."""
    pending = state['pending']
    if not pending:
        return
    pair = pending.get('pair') or pair_of(state)
    if order.get('clOrdId') != pending['client_id'] or order.get('instId') != pair:
        raise ValueError('订单标识不一致，暂停对账')
    if order.get('side') != pending['side']:
        raise ValueError('订单方向不一致，暂停对账')
    qty = float(order.get('accFillSz') or 0)
    price = float(order.get('avgPx') or 0)
    fee, rebate = float(order.get('fee') or 0), float(order.get('rebate') or 0)
    if not all(math.isfinite(x) for x in [qty, price, fee, rebate]) or qty < 0 or (qty > 0 and price <= 0):
        raise ValueError('成交数据无效，暂停对账')
    base = pair.split('-')[0]
    cash, units = state['cash'], state['quantity']
    sign = 1 if pending['side'] == 'buy' else -1
    cash -= sign*qty*price
    units += sign*qty
    for amount, ccy in [(fee, order.get('feeCcy')), (rebate, order.get('rebateCcy'))]:
        if amount == 0:
            continue
        if ccy == 'USDT': cash += amount
        elif ccy == base: units += amount
        else: raise ValueError('手续费币种不受支持，请人工核对')
    state['cash'], state['quantity'] = cash, max(0., units)
    record = {'id': order.get('ordId'), 'client_id': pending['client_id'], 'side': pending['side'], 'pair': pair,
              'quantity': qty, 'price': price, 'fee': fee, 'fee_ccy': order.get('feeCcy'),
              'state': order.get('state'), 'ts': time.time(), 'reason': pending['reason']}
    state['orders'] = (state['orders']+[record])[-200:]
    state['pending'] = None
    state['reason'] = f"订单已对账：{record['state']} / 成交 {qty:g} {base}"
    if cash < -.01 or units < -1e-9:
        state['running'] = False
        state['error'] = '账本余额出现异常，已暂停；请勿重置记录'

class Engine:
    def __init__(self, store, allow_live=False, client_factory=None, adviser_factory=None):
        self.store, self.allow_live = store, allow_live
        self.lock = asyncio.Lock()
        self.client_factory = client_factory
        self.adviser_factory = adviser_factory

    def client(self, mode):
        if self.client_factory:
            return self.client_factory(mode)
        return OkxClient(self.store.credentials(mode), allow_live=self.allow_live, pairs=self.tradable_pairs())

    def buyable_pairs(self):
        """Coins that may be bought: the defaults plus the latest scanner universe."""
        scan = self.store.get('scanner') or {}
        return set(PAIRS) | {p for p in scan.get('universe', []) if isinstance(p, str) and PAIR_RE.fullmatch(p)}

    def tradable_pairs(self):
        """Buyable coins plus anything a trial holds or awaits, so exits and reconciliation never lock."""
        held = set()
        for mode in ('demo', 'live'):
            state = self.session(mode)
            if state:
                held |= {p for p in (pair_of(state), (state.get('pending') or {}).get('pair')) if p}
        return self.buyable_pairs() | held

    def adviser(self, provider='openrouter'):
        if self.adviser_factory:
            return self.adviser_factory(provider)
        return ADVISERS[provider](self.store.secret(provider))

    def session(self, mode):
        return self.store.get('session:'+mode)

    def save(self, mode, state):
        self.store.set('session:'+mode, state)

    def log(self, mode, kind, text):
        self.store.event(kind, f'[{"模拟" if mode == "demo" else "实盘"}] {text}')

    def restart_pause(self):
        for mode in ('demo','live'):
            state = self.session(mode)
            if state and state['running']:
                state['running'] = False
                state['reason'] = '程序重启，等待用户重新启动；原持仓和订单继续保留'
                self.save(mode, state)
                self.log(mode, 'pause', state['reason'])

    async def start(self, plan):
        async with self.lock:
            cfg = plan.model_dump(exclude={'confirmation'})
            mode = plan.mode
            if mode == 'live' and (not self.allow_live or plan.confirmation != '我自行启用实盘'):
                raise ValueError('实盘需在主机以 --allow-live 启动，并输入实盘确认文字')
            other = self.session('live' if mode == 'demo' else 'demo')
            if other and other['running']:
                raise ValueError('请先暂停另一模式的策略')
            old = self.session(mode)
            if old and old['running']:
                raise ValueError('策略已在运行；请先暂停再修改')
            if old and old['pending']:
                raise ValueError('存在未完成对账的订单，禁止重新启动')
            if old and (old['plan']['budget'] != plan.budget or old['plan'].get('pair_mode', 'fixed') != plan.pair_mode
                        or (plan.pair_mode == 'fixed' and old['plan']['pair'] != plan.pair)):
                raise ValueError('现有试验不能更换本金、交易对或选币方式；先卖出本策略持仓，再结束试验')
            if old and old.get('risk_stopped'):
                raise ValueError('本试验已触及亏损线，不允许续期掩盖亏损；请先结束并复盘')
            if plan.strategy == 'llm':
                name = PROVIDER_NAMES[plan.llm_provider]
                if not self.store.has_secret(plan.llm_provider):
                    raise ValueError(f'LLM 策略需要先配置 {name} 密钥')
                if not await self.adviser(plan.llm_provider).has_model(plan.llm_model):
                    raise ValueError(f'{name} 上找不到模型 {plan.llm_model}')
            # Resuming a trial that still holds a coin is always allowed, so the loss line and exits
            # keep working after the coin leaves the scanner list; such a coin is never bought again.
            holding = bool(old and pair_of(old) and old['quantity'] > 0)
            if plan.pair_mode == 'fixed' and plan.pair not in self.buyable_pairs() and not holding:
                raise ValueError(f'{plan.pair} 不在最新的选币雷达名单中；请先刷新雷达或换一个币')
            if plan.pair_mode == 'rotate' and not holding:
                scan = self.store.get('scanner') or {}
                if time.time() - scan.get('as_of', 0) > SCAN_MAX_AGE or not scan.get('rows'):
                    raise ValueError('轮动模式需要 3 小时内的选币雷达结果；请先刷新雷达')
            client = self.client(mode)
            balance = await client.balance()
            needed = old['cash'] if old else plan.budget
            if available(balance, 'USDT') + .01 < needed:
                raise ValueError('OKX 可用 USDT 小于本试验的现金预算')
            held = pair_of(old) if old else None
            if old and held and available(balance, held.split('-')[0])+1e-10 < old['quantity']:
                raise ValueError('账户持仓少于策略账本；请核对手动交易或资金划转')
            if plan.pair_mode == 'fixed':
                instrument = await client.instrument(plan.pair)
                market = await client.ticker(plan.pair)
                if plan.order_quote / market['price'] < float(instrument['minSz']):
                    raise ValueError('单笔金额小于 OKX 当前最小订单')
            state = old or initial_session(cfg, time.time())
            state.update(plan=cfg, running=True, expires=time.time()+plan.hours*3600,
                         error='', reason='用户已启动；等待下一次策略检查', llm_order=None)
            self.save(mode, state)
            strategy = 'LLM '+plan.llm_model if plan.strategy == 'llm' else plan.strategy.upper()
            coins = f'轮动·雷达前 {plan.top_n} 名' if plan.pair_mode == 'rotate' else plan.pair
            self.log(mode, 'start', f'{coins} / {strategy} / 预算 {plan.budget:g} / 亏损触发线 {plan.max_loss:g} USDT / {plan.hours} 小时')
            return state

    async def pause(self, mode):
        async with self.lock:
            state = self.session(mode)
            if state:
                state['running'] = False
                state['reason'] = '用户暂停；现有持仓保留，不自动卖出'
                self.save(mode,state)
                self.log(mode,'pause',state['reason'])

    async def finish(self, mode):
        async with self.lock:
            state = self.session(mode)
            if not state:
                return
            if state['running'] or state['pending']:
                raise ValueError('只能结束已暂停、没有未决订单的试验')
            dust = ''
            pair = pair_of(state)
            if state['quantity'] > 1e-12 and pair:
                # Fee deductions in the base coin leave remainders below the lot size. Only such
                # unsellable dust may be archived, and it stays on record rather than being zeroed.
                instrument = await self.client(mode).instrument(pair)
                if float(floor_size(state['quantity'], instrument['lotSz'])) >= float(instrument['minSz']):
                    raise ValueError('仍有可卖出的本策略持仓；请先卖出后再结束试验')
                state['dust'] = state['quantity']
                base = pair.split('-')[0]
                dust = f"；零碎币 {state['quantity']:.12g} {base}（约 {state['quantity']*state['price']:.4f} USDT）低于 OKX 最小下单量，留在账户未卖出"
            if state.get('dust_left'):
                dust += '；轮动留下的零碎币：' + '、'.join(f'{q:.12g} {p.split("-")[0]}' for p, q in state['dust_left'].items())
            self.store.set('archive:'+state['id'], state)
            self.store.set('session:'+mode, None)
            self.log(mode,'archive',f"试验已归档，现金净结果 {state['cash']-state['plan']['budget']:.4f} USDT{dust}")

    async def flatten(self, mode, confirmation):
        async with self.lock:
            if mode == 'live' and (not self.allow_live or confirmation != '我自行启用实盘'):
                raise ValueError('需要主机实盘开关与用户确认')
            state = self.session(mode)
            if not state or state['pending'] or state['quantity'] <= 0 or not pair_of(state):
                raise ValueError('没有可卖出的本策略持仓，或仍有未决订单')
            # Explicit user-requested exit is separately authorized from the timed strategy.
            client = self.client(mode)
            market = await client.ticker(pair_of(state))
            instrument = await client.instrument(pair_of(state))
            balance = await client.balance()
            await self.submit(mode, state, client, 'sell', market['price'], instrument, balance, '用户卖出本策略全部持仓', explicit_exit=True)
            state['running'] = False
            self.save(mode,state)
            return state

    async def submit(self, mode, state, client, side, price, instrument, balance, reason, *, explicit_exit=False):
        now = time.time()
        if state['pending']:
            raise ValueError('未决订单禁止重复提交')
        if not explicit_exit and (not state['running'] or now >= state['expires']):
            raise ValueError('运行已暂停或到期')
        if mode == 'live' and not self.allow_live:
            raise ValueError('主机没有启用实盘')
        plan = state['plan']
        pair = pair_of(state)
        base = pair.split('-')[0]
        if available(balance, base)+1e-10 < state['quantity']:
            raise ValueError('账户持仓与账本不一致')
        day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
        if side == 'buy':
            if pair not in self.buyable_pairs():
                state['reason'] = f'{pair} 已不在选币雷达的可交易名单中，不再买入；已有持仓仍可卖出'
                return
            if plan['budget']-(state['cash']+state['quantity']*price) >= plan['max_loss']:
                raise ValueError('已触及最大亏损触发线')
            if state['days'].get(day,0) >= plan['max_orders_day']:
                raise ValueError('达到当日订单上限')
            headroom = max(0., plan['budget']*plan['max_position_pct']/100-state['quantity']*price)
            quote = min(plan['order_quote'],state['cash']/1.01,available(balance,'USDT')/1.01,headroom/1.005)
            size = floor_size(max(0,quote), '.01')
            if float(size)/price < float(instrument['minSz']):
                state['reason'] = '可用预算或仓位余量不足最小订单；不交易'
                return
            target = 'quote_ccy'
        else:
            size = floor_size(state['quantity'], instrument['lotSz'])
            if float(size) < float(instrument['minSz']):
                state['reason'] = '本策略持仓低于最小卖出量；保留零碎币，请在 OKX 核对'
                return
            target = 'base_ccy'
        client_id = 'od'+uuid.uuid4().hex[:28]
        body = {'instId': pair, 'tdMode': 'cash', 'side': side, 'ordType': 'market',
                'sz': size, 'tgtCcy': target, 'clOrdId': client_id, 'banAmend': True}
        state['pending'] = {'client_id':client_id, 'pair':pair, 'side':side, 'size':size, 'ts':now, 'reason':reason}
        state['last_action'] = now
        state['days'] = {k:v for k,v in state['days'].items() if k == day}
        state['days'][day] = state['days'].get(day,0)+1
        self.save(mode,state)  # durable intent, before POST; failures freeze until reconciliation
        self.log(mode,'intent',f'{pair} {side} {size} {target} / {reason}')
        try:
            result = await client.place_order(body, write_permit=True)
            state['pending']['order_id'] = result['ordId']
            state['reason'] = 'OKX 已接收订单，等待成交对账'
        except Exception as exc:
            state['running'] = False
            detail = exc.message if isinstance(exc, OkxError) else ''
            state['error'] = detail+'提交结果未确认，已冻结；不会重发。请检查 OKX 订单与连接后对账。'
            self.log(mode,'error',state['error'])
        self.save(mode,state)

    async def tick(self, mode):
        ask = None
        async with self.lock:
            state = self.session(mode)
            if not state:
                return
            client, now = self.client(mode), time.time()
            try:
                if state['pending']:
                    pending = state['pending']
                    row = await client.order(pending.get('pair') or pair_of(state), pending['client_id'])
                    if row.get('state') in {'filled','canceled','mmp_canceled'}:
                        apply_fill(state,row)
                        self.log(mode,'fill',state['reason'])
                    else:
                        state['reason'] = '订单仍未终结，继续等待对账；不提交新订单'
                    self.save(mode,state)
                    return
                if state['running'] and now >= state['expires']:
                    state['running'] = False
                    state['reason'] = '运行期限已到；持仓保留，不再自动下单'
                    self.log(mode,'pause',state['reason'])
                if not state['running'] and state['quantity'] <= 0:
                    self.save(mode,state)
                    return
                pair = pair_of(state)
                if pair:
                    market = await client.ticker(pair)
                    state['price'] = market['price']
                state['last_tick'] = now
                state['equity'] = state['cash']+state['quantity']*state['price'] if pair else state['cash']
                state['peak'] = max(state['peak'],state['equity'])
                if not state['running']:
                    self.save(mode,state)
                    return
                plan = state['plan']
                loss = plan['budget']-state['equity']
                if loss >= plan['max_loss']:
                    state['risk_stopped'] = True
                    if state['quantity'] > 0 and pair:
                        await self.submit(mode,state,client,'sell',state['price'],await client.instrument(pair),
                                          await client.balance(),'触及亏损线，尝试卖出本策略持仓')
                    state['running'] = False
                    state['reason'] = '已触及亏损触发线，策略停止；请检查持仓和订单是否完成'
                    self.log(mode,'risk',state['reason'])
                else:
                    rotating = plan.get('pair_mode', 'fixed') == 'rotate'
                    if rotating and pair and state['quantity'] > 0:
                        pair = await self.release_dust(mode, state, client, now)
                    if rotating and not (pair and state['quantity'] > 0):
                        ask = await self.rotate(mode, state, client, now)
                    else:
                        ask = await self.decide(mode, state, client, now, pair)
                self.save(mode,state)
            except Exception as exc:
                message = exc.message if isinstance(exc,OkxError) else str(exc) if isinstance(exc,ValueError) else '检查失败，请查看连接和运行环境'
                state['running'] = False
                state['error'] = message
                state['reason'] = '异常已暂停；持仓保留，请检查后由用户重新启动'
                state['last_tick'] = now
                self.save(mode,state)
                self.log(mode,'error',message)
                ask = None
        if ask and await self.advise(mode, ask):
            await self.tick(mode)

    async def decide(self, mode, state, client, now, pair):
        """One coin: the strategy (or the model's answer for this bar) decides buy, sell or hold."""
        plan = state['plan']
        # OKX includes the in-progress bar in the newest page; two pages keep >= 100 closed bars.
        candles = await client.candles(pair, pages=2)
        ts = hourly_ts(candles, now)
        if plan['strategy'] == 'llm':
            order, state['llm_order'] = state.get('llm_order'), None
            if ts > state['last_candle']:
                state['last_candle'] = ts
                if daily_cap_reached(state):
                    state['reason'] = '今日订单次数已用完，继续监测亏损线；本小时不询问模型'
                    self.log(mode,'decision',state['reason'])
                else:
                    # Asked after the lock is released; the answer returns as llm_order and is
                    # executed by a fresh tick, so every gate above runs again first.
                    state['reason'] = f"已询问模型 {plan['llm_model']}，等待回答"
                    return self.question(state, ts, snapshot(candles, state))
            elif order and order['candle'] == ts and not order.get('pair'):
                await self.act(mode, state, client, order)
            return None
        decision = signal(candles,plan['strategy'])
        state['signal'] = decision
        state['reason'] = decision['reason']
        if ts > state['last_candle']:
            state['last_candle'] = ts
            await self.act(mode, state, client, decision)
        return None

    async def release_dust(self, mode, state, client, now):
        """Rotation: on a new bar, a holding below the minimum sell size is set aside as recorded dust."""
        pair = pair_of(state)
        if int(now // 3600) * 3600000 - 3600000 <= state['last_candle']:
            return pair
        instrument = await client.instrument(pair)
        if float(floor_size(state['quantity'], instrument['lotSz'])) >= float(instrument['minSz']):
            return pair
        dust = state.setdefault('dust_left', {})
        dust[pair] = dust.get(pair, 0.)+state['quantity']
        self.log(mode, 'decision', f"{pair} 剩余 {state['quantity']:.12g} 低于最小下单量，记为零碎币并回到空仓")
        state.update(quantity=0., pair=None, price=0.)
        state['equity'] = state['cash']
        return None

    async def rotate(self, mode, state, client, now):
        """Flat rotation trial: on a new hourly bar pick one top-ranked coin to buy, or stay in cash."""
        plan = state['plan']
        bar = int(now // 3600) * 3600000 - 3600000
        order, state['llm_order'] = state.get('llm_order'), None
        if bar <= state['last_candle']:
            if order and order['candle'] == bar and order.get('pair'):
                await self.enter(mode, state, client, order['pair'], order)
            return None
        state['last_candle'] = bar
        if daily_cap_reached(state):
            state['reason'] = '今日订单次数已用完，保持空仓'
            self.log(mode, 'decision', state['reason'])
            return None
        scan = self.store.get('scanner') or {}
        if now - scan.get('as_of', 0) > SCAN_MAX_AGE:
            state['reason'] = '选币雷达超过 3 小时未更新，本小时不开新仓'
            self.log(mode, 'decision', state['reason'])
            return None
        buyable = self.buyable_pairs()
        candidates = []
        for row in [r for r in scan.get('rows', []) if r.get('pair') in buyable][:plan['top_n']]:
            try:
                candles = await client.recent_candles(row['pair'], bar='1H', limit=120)
                hourly_ts(candles, now)
            except (OkxError, ValueError):
                continue
            candidates.append((row, candles))
        if not candidates:
            state['reason'] = '候选币的小时行情均不可用，本小时不开新仓'
            self.log(mode, 'decision', state['reason'])
            return None
        if plan['strategy'] == 'llm':
            state['reason'] = f"已请模型 {plan['llm_model']} 从 {len(candidates)} 个候选币中选择，等待回答"
            return self.question(state, bar, rotation_snapshot(candidates, state), [r['pair'] for r, _ in candidates])
        for row, candles in candidates:
            decision = signal(candles, plan['strategy'])
            if decision['side'] == 'buy':
                state['signal'] = decision
                await self.enter(mode, state, client, row['pair'],
                                 {**decision, 'reason': f"雷达第 {row['rank']} 名 {row['pair']}：{decision['reason']}"})
                return None
        state['reason'] = f'雷达前 {len(candidates)} 名均无买入信号，保持空仓'
        self.log(mode, 'decision', state['reason'])
        return None

    async def enter(self, mode, state, client, pair, decision):
        state['pair'] = pair
        state['price'] = (await client.ticker(pair))['price']
        await self.act(mode, state, client, {**decision, 'side': 'buy'})
        if state['quantity'] <= 0 and not state['pending']:
            state['pair'] = None  # nothing bought; the trial stays flat

    def question(self, state, candle, view, choices=None):
        plan = state['plan']
        return {'session': state['id'], 'candle': candle, 'provider': plan.get('llm_provider', 'openrouter'),
                'model': plan['llm_model'], 'view': view, 'choices': choices}

    async def act(self, mode, state, client, decision):
        if daily_cap_reached(state):
            state['reason'] = '今日订单次数已用完，继续监测亏损线'
        elif decision['side'] == 'buy' or (decision['side'] == 'sell' and state['quantity'] > 0):
            await self.submit(mode, state, client, decision['side'], state['price'],
                              await client.instrument(pair_of(state)), await client.balance(), decision['reason'])
        self.log(mode, 'decision', state['reason'])

    async def advise(self, mode, ask):
        """Ask the model without holding the lock, then record its answer. True if it may trade."""
        model, choices = ask['model'], ask['choices']
        try:
            adviser = self.adviser(ask['provider'])
            advice = await (adviser.choose(model, ask['view'], choices) if choices else adviser.decide(model, ask['view']))
        except Exception as exc:
            message = exc.message if isinstance(exc, LlmError) else '模型调用异常'
            advice = {'side': 'hold', 'confidence': None, 'reason': message+'；本小时按持有处理',
                      'cost': 0., 'seconds': None, 'failed': True}
        async with self.lock:
            state = self.session(mode)
            if not state or state['id'] != ask['session'] or state['last_candle'] != ask['candle']:
                return False
            side, pair = advice['side'], advice.get('pair')
            record = {'ts': time.time(), 'candle': ask['candle'], 'model': model, 'pair': pair,
                      **{k: advice.get(k) for k in ('side', 'confidence', 'reason', 'cost', 'seconds', 'tokens')}}
            state['advice'] = (state.get('advice', [])+[record])[-200:]
            state['llm_calls'] = state.get('llm_calls', 0)+1
            state['llm_cost'] = state.get('llm_cost', 0.)+advice['cost']
            state['llm_tokens'] = state.get('llm_tokens', 0)+(advice.get('tokens') or 0)
            if advice.get('failed'):
                state['reason'] = f"{model}：{advice['reason']}"
            else:
                confidence = '' if advice['confidence'] is None else f"，信心 {advice['confidence']:g}"
                target = f' {pair}' if pair else ''
                state['reason'] = f"{model}：{LABELS[side]}{target}{confidence} · {advice['reason']}"
            state['signal'] = {'side': side, 'reason': state['reason'], 'metric': advice['confidence']}
            if choices:
                tradable = side == 'buy' and pair in choices
            else:
                tradable = side == 'buy' or (side == 'sell' and state['quantity'] > 0)
                if side == 'sell' and not tradable:
                    state['reason'] += '（无持仓，不执行）'
            state['llm_order'] = {'candle': ask['candle'], 'side': side, 'reason': state['reason'],
                                  'pair': pair if choices else None} if tradable else None
            self.save(mode, state)
            self.log(mode, 'advice', state['reason'])
            return tradable

    async def loop(self):
        while True:
            for mode in ('demo','live'):
                await self.tick(mode)
            await asyncio.sleep(60)
