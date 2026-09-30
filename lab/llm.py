"""Model decision advisers: a model picks buy / sell / hold once per closed hourly bar.

OpenRouter reaches chat models; TypeSafe reaches Jev, which answers a typed choice question.

The prompt carries public candles and this trial's own ledger numbers only: never credentials,
account identifiers or exchange balances. The model cannot size, route or place orders; the
engine applies every budget, loss, position, frequency and expiry gate after it answers.
"""
from __future__ import annotations
import json
import math
import time
from datetime import datetime, timezone
import httpx
from .rsi import _wilder_rsi

HOST = 'https://openrouter.ai/api/v1'
TYPESAFE_HOST = 'https://api.typesafe.ai'
SIDES = {'buy', 'sell', 'hold'}
LABELS = {'buy': '买入', 'sell': '卖出', 'hold': '持有'}

SYSTEM = """你是一个现货交易决策器，只为一个小额试验账本决定下一步动作。
规则：
- 市场是 OKX 现货。你只能选择：buy（按固定金额市价买入一笔）、sell（市价卖出本试验全部持仓）或 hold（不动）。没有杠杆、合约或做空。
- 每根小时 K 线收盘后决策一次，成交在之后的市价；每笔买卖约有 0.1% 手续费外加滑点，频繁交易会被费用吃掉。
- 预算、亏损触发线、仓位上限、每日次数由程序强制执行，你无法改变；超出限制的建议会被忽略。
- 没有持仓时 sell 无效；没有把握时选择 hold。
只输出一个 JSON 对象，不要任何其他文字：
{"decision": "buy" | "sell" | "hold", "confidence": 0 到 100 的整数, "reason": "不超过 60 字的中文理由"}"""


class LlmError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d %H:00')


def snapshot(candles, state):
    """Compact market + ledger view built from closed bars only (no in-progress bar)."""
    closed = sorted((r for r in candles if len(r) >= 9 and r[8] == '1'), key=lambda r: int(r[0]))
    recent, plan = closed[-48:], state['plan']
    closes = [float(r[4]) for r in closed[-100:]]
    low, high = min(float(r[3]) for r in recent), max(float(r[2]) for r in recent)
    price, quantity = state['price'], state['quantity']
    equity = state['cash'] + quantity * price
    day = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    return {
        'pair': plan['pair'], 'bar': '1H', 'last_closed_bar_utc': _utc(int(closed[-1][0])),
        'price': price, 'rsi14': round(_wilder_rsi(closes, 14), 1),
        'change_24h_pct': round((closes[-1] / closes[-25] - 1) * 100, 2) if len(closes) > 24 else None,
        'range_48h_position_pct': round((closes[-1] - low) / (high - low) * 100, 1) if high > low else 50.,
        'candles_utc_open_high_low_close_volume': [
            [_utc(int(r[0])), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])] for r in recent],
        'trial': {
            'budget_usdt': plan['budget'], 'cash_usdt': round(state['cash'], 4),
            'position_qty': quantity, 'position_value_usdt': round(quantity * price, 4),
            'equity_usdt': round(equity, 4), 'loss_so_far_usdt': round(plan['budget'] - equity, 4),
            'loss_trigger_usdt': plan['max_loss'],
            'max_position_usdt': round(plan['budget'] * plan['max_position_pct'] / 100, 4),
            'buy_order_usdt': plan['order_quote'],
            'orders_left_today': max(0, plan['max_orders_day'] - state['days'].get(day, 0)),
        },
    }


def parse(content):
    """Anything other than one of the offered choices is a hold."""
    text = content if isinstance(content, str) else ''
    start, end = text.find('{'), text.rfind('}')
    try:
        data = json.loads(text[start:end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    side = str(data.get('decision', '')).strip().lower() if isinstance(data, dict) else ''
    if side not in SIDES:
        return {'side': 'hold', 'confidence': None, 'reason': '模型回答不符合格式，按持有处理'}
    confidence = data.get('confidence')
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) \
            or not math.isfinite(confidence) or not 0 <= confidence <= 100:
        confidence = None
    reason = ''.join(ch for ch in ' '.join(str(data.get('reason') or '').split()) if ch.isprintable())
    return {'side': side, 'confidence': confidence, 'reason': reason[:120] or '模型未给出理由'}


class _JsonApi:
    NAME, BASE, REASONS = '', '', {}

    def __init__(self, api_key='', *, transport=None):
        self.api_key = api_key
        self.transport = transport

    async def _request(self, method, path, body=None, *, timeout=20):
        if not self.api_key:
            raise LlmError(f'尚未配置 {self.NAME} 密钥')
        headers = {'Authorization': f'Bearer {self.api_key}', 'Content-Type': 'application/json', 'Accept': 'application/json'}
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=self.transport) as client:
                resp = await client.request(method, self.BASE + path, headers=headers, json=body)
        except httpx.HTTPError as exc:
            raise LlmError(f'无法连接 {self.NAME}') from exc
        if resp.status_code != 200:
            # Status only: provider error bodies are not reflected into logs or the browser.
            reasons = {401: f'{self.NAME} 密钥无效或已停用', 429: f'{self.NAME} 请求过于频繁', **self.REASONS}
            raise LlmError(reasons.get(resp.status_code, f'{self.NAME} 返回 HTTP {resp.status_code}'))
        try:
            data = resp.json()
        except ValueError as exc:
            raise LlmError(f'{self.NAME} 返回无法解析的数据') from exc
        if not isinstance(data, dict):
            raise LlmError(f'{self.NAME} 返回无法解析的数据')
        return data


class OpenRouterClient(_JsonApi):
    NAME, BASE = 'OpenRouter', HOST
    REASONS = {402: 'OpenRouter 余额或密钥额度不足', 404: 'OpenRouter 上找不到该模型'}

    async def key_info(self):
        data = (await self._request('GET', '/key')).get('data') or {}
        return {k: data.get(k) for k in ('limit', 'limit_remaining', 'usage')}

    async def has_model(self, model):
        rows = (await self._request('GET', '/models', timeout=30)).get('data') or []
        return any(isinstance(r, dict) and r.get('id') == model for r in rows)

    async def decide(self, model, view):
        started = time.monotonic()
        body = {'model': model, 'temperature': 0, 'max_tokens': 2000,
                'messages': [{'role': 'system', 'content': SYSTEM},
                             {'role': 'user', 'content': json.dumps(view, ensure_ascii=False, separators=(',', ':'))}]}
        data = await self._request('POST', '/chat/completions', body, timeout=60)
        try:
            content = data['choices'][0]['message'].get('content')
        except (KeyError, IndexError, TypeError, AttributeError):
            content = None
        cost = (data.get('usage') or {}).get('cost')
        advice = parse(content)
        advice['cost'] = float(cost) if isinstance(cost, (int, float)) and math.isfinite(cost) and cost >= 0 else 0.
        advice['seconds'] = round(time.monotonic() - started, 1)
        return advice


JEV_INSTRUCTIONS = ('state 是一个小额现货试验账本的最新数据：OKX 已收盘的小时 K 线（UTC）、RSI、区间位置与本试验持仓。'
                    '决定下一步动作。每笔买卖约 0.1% 手续费加滑点，频繁交易会被费用吃掉；'
                    '预算、亏损线、仓位上限与每日次数由程序强制执行。没有把握时选 hold。')
JEV_CRITERIA = {
    'buy': '按固定金额（buy_order_usdt）市价买入一笔，受仓位上限约束',
    'sell': '市价卖出本试验全部持仓；position_qty 为 0 时无效',
    'hold': '不动：方向不明、没有把握或刚交易过',
}


def _unit(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def parse_choice(answer):
    """A TypeSafe choice answer; anything but one of the offered labels is a hold."""
    if not isinstance(answer, dict) or answer.get('type') != 'choice' or answer.get('choice') not in SIDES:
        return {'side': 'hold', 'confidence': None, 'reason': '模型回答不符合格式，按持有处理'}
    confidence = answer.get('confidence')
    probabilities = answer.get('probabilities') if isinstance(answer.get('probabilities'), dict) else {}
    parts = [f'{LABELS[k]} {probabilities[k] * 100:.0f}%' for k in ('buy', 'sell', 'hold') if _unit(probabilities.get(k))]
    return {'side': answer['choice'], 'confidence': round(confidence * 100) if _unit(confidence) else None,
            'reason': '概率 ' + ' · '.join(parts) if parts else '未返回各选项概率'}


class TypeSafeClient(_JsonApi):
    """Jev through the TypeSafe System One API (wire format from the official typesafe-sdk 0.7.2)."""
    NAME, BASE = 'TypeSafe', TYPESAFE_HOST
    REASONS = {422: 'TypeSafe 拒绝了请求内容（HTTP 422）'}

    async def key_info(self):
        rows = (await self._request('GET', '/v1/models')).get('models') or []
        return {'models': [r['name'] for r in rows if isinstance(r, dict) and isinstance(r.get('name'), str)][:20]}

    async def has_model(self, model):
        return model in (await self.key_info())['models']

    async def decide(self, model, view):
        started = time.monotonic()
        body = {'model': model, 'state': view,
                'questions': {'action': {'type': 'choice', 'instructions': JEV_INSTRUCTIONS, 'criteria': JEV_CRITERIA}}}
        data = await self._request('POST', '/v1/systemone', body, timeout=30)
        answers = data.get('answers') if isinstance(data.get('answers'), dict) else {}
        advice = parse_choice(answers.get('action'))
        tokens = (data.get('usage') or {}).get('input_tokens') if isinstance(data.get('usage'), dict) else None
        # TypeSafe reports token counts, not a price; cost is settled on the TypeSafe bill.
        advice['cost'] = 0.
        advice['tokens'] = tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None
        advice['seconds'] = round(time.monotonic() - started, 1)
        return advice


ADVISERS = {'openrouter': OpenRouterClient, 'typesafe': TypeSafeClient}
PROVIDER_NAMES = {'openrouter': 'OpenRouter', 'typesafe': 'TypeSafe'}
