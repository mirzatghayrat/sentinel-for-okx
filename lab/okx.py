"""OKX V5 adapter. Fixed host and spot-only write surface.

Live writes require a process-level opt-in AND an engine-issued permit. The
application never silently changes modes or retries an uncertain order.
"""
from __future__ import annotations
import base64
import hmac
import json
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from urllib.parse import urlencode
import httpx

HOST = 'https://www.okx.com'
PUBLIC = {'/market/ticker', '/market/tickers', '/market/candles', '/market/history-candles', '/public/instruments'}
PRIVATE_GET = {'/account/config', '/account/balance', '/trade/order', '/trade/orders-pending'}
# Always-available defaults; the scanner widens the tradable set to its filtered USDT universe.
PAIRS = frozenset({'BTC-USDT', 'ETH-USDT'})
PAIR_RE = re.compile(r'[A-Z0-9]{1,20}-USDT')

class OkxError(Exception):
    def __init__(self, message: str, *, code: str = 'okx_error'):
        super().__init__(message)
        self.message, self.code = message, code

@dataclass(frozen=True)
class OkxCredentials:
    api_key: str = ''
    api_secret: str = ''
    passphrase: str = ''
    simulated: bool = True

    @property
    def is_complete(self):
        return all((self.api_key, self.api_secret, self.passphrase))


def _iso_timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def _sign(secret, timestamp, method, request_path, body):
    payload = f'{timestamp}{method.upper()}{request_path}{body}'.encode()
    return base64.b64encode(hmac.new(secret.encode(), payload, sha256).digest()).decode()

class OkxClient:
    def __init__(self, creds: OkxCredentials | None = None, *, transport=None, allow_live=False, pairs=PAIRS):
        self.creds = creds or OkxCredentials()
        self.transport = transport
        self.allow_live = allow_live
        self.pairs = frozenset(p for p in pairs if PAIR_RE.fullmatch(p))

    async def request(self, method, path, *, params=None, body=None, write_permit=False):
        method = method.upper()
        public = method == 'GET' and path in PUBLIC
        if not (public or (method == 'GET' and path in PRIVATE_GET)
                or (method == 'POST' and path == '/trade/order')):
            raise OkxError('不支持此接口。', code='blocked_endpoint')
        if not public and not self.creds.is_complete:
            raise OkxError('请先在连接面板配置当前模式的 API 凭证。', code='missing_credentials')
        if method == 'POST':
            if not write_permit or (not self.creds.simulated and not self.allow_live):
                raise OkxError('交易尚未由用户启用。', code='trade_disabled')
            if not body or body.get('tdMode') != 'cash' or body.get('instId') not in self.pairs:
                raise OkxError('仅允许已通过筛选的 USDT 现货订单。', code='spot_only')
            if body.get('ordType') != 'market' or body.get('side') not in {'buy', 'sell'}:
                raise OkxError('只支持受控市价订单。', code='bad_order')
            if set(body) - {'instId','tdMode','side','ordType','sz','tgtCcy','clOrdId','banAmend'}:
                raise OkxError('订单包含未允许的参数。', code='bad_order')
        query = '?' + urlencode({k: v for k, v in params.items() if v is not None}) if params else ''
        request_path = f'/api/v5{path}{query}'
        body_str = json.dumps(body, separators=(',', ':')) if body else ''
        headers = {'Content-Type': 'application/json'}
        if self.creds.simulated:
            headers['x-simulated-trading'] = '1'
        if not public:
            ts = _iso_timestamp()
            headers.update({'OK-ACCESS-KEY': self.creds.api_key, 'OK-ACCESS-TIMESTAMP': ts,
                            'OK-ACCESS-PASSPHRASE': self.creds.passphrase,
                            'OK-ACCESS-SIGN': _sign(self.creds.api_secret, ts, method, request_path, body_str)})
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=self.transport) as client:
                resp = await client.request(method, HOST + request_path, headers=headers,
                                            content=body_str.encode() if body_str else None)
        except httpx.HTTPError as exc:
            raise OkxError('无法连接 OKX；没有自动重发订单。', code='network') from exc
        if resp.status_code != 200:
            raise OkxError(f'OKX 返回 HTTP {resp.status_code}。', code='http_error')
        try:
            data = resp.json()
        except ValueError as exc:
            raise OkxError('OKX 返回无法解析的数据。', code='bad_response') from exc
        # A rejected order arrives as top-level code 1 with the reason in the row's sCode; report the sCode.
        for row in data.get('data') or []:
            if isinstance(row, dict) and str(row.get('sCode', '0')) != '0':
                raise OkxError(f"OKX 未接受订单（{row['sCode']}）。", code=str(row['sCode']))
        if str(data.get('code')) != '0':
            code = str(data.get('code', 'unknown'))
            # Do not reflect remote response text: it can contain submitted identifiers.
            raise OkxError(f'OKX 拒绝请求（{code}）；请检查凭证模式、权限、IP 白名单及账户地区。', code=code)
        return data.get('data', [])

    async def ticker(self, pair):
        rows = await self.request('GET', '/market/ticker', params={'instId': pair})
        if not rows:
            raise OkxError('行情为空。')
        row = rows[0]
        ts, price = int(row.get('ts', 0)), float(row.get('last', 0))
        if abs(time.time() * 1000 - ts) > 120_000 or not math.isfinite(price) or price <= 0:
            raise OkxError('行情超过两分钟或价格无效，暂停决策。', code='stale_market')
        return {'price': price, 'timestamp': ts, 'open24h': float(row.get('open24h') or price)}

    async def candles(self, pair, *, pages=1, bar='1H'):
        rows, after = [], None
        for _ in range(pages):
            chunk = await self.request('GET', '/market/history-candles',
                                       params={'instId': pair, 'bar': bar, 'limit': '100', 'after': after})
            if not chunk:
                break
            rows.extend(chunk)
            after = min(int(r[0]) for r in chunk)
        return _closed(rows)

    async def recent_candles(self, pair, *, bar='1D', limit=100):
        """Newest closed bars from the higher-limit recent-candles endpoint (scanner use)."""
        return _closed(await self.request('GET', '/market/candles', params={'instId': pair, 'bar': bar, 'limit': str(limit)}))

    async def tickers(self):
        """Every spot ticker in one public call."""
        return [r for r in await self.request('GET', '/market/tickers', params={'instType': 'SPOT'}) if isinstance(r, dict)]

    async def instruments(self):
        return [r for r in await self.request('GET', '/public/instruments', params={'instType': 'SPOT'}) if isinstance(r, dict)]

    async def instrument(self, pair):
        rows = await self.request('GET', '/public/instruments', params={'instType': 'SPOT', 'instId': pair})
        if not rows or rows[0].get('state') != 'live':
            raise OkxError('交易对不可交易。')
        return rows[0]

    async def balance(self):
        rows = await self.request('GET', '/account/balance')
        if not rows:
            raise OkxError('账户余额为空。')
        return rows[0]

    async def place_order(self, body, *, write_permit=False):
        rows = await self.request('POST', '/trade/order', body=body, write_permit=write_permit)
        if not rows or not rows[0].get('ordId'):
            raise OkxError('订单结果不明确，需要对账。', code='uncertain_order')
        return rows[0]

    async def order(self, pair, client_id):
        rows = await self.request('GET', '/trade/order', params={'instId': pair, 'clOrdId': client_id})
        if not rows:
            raise OkxError('订单尚未查到；保持冻结，不重发。', code='uncertain_order')
        return rows[0]


def market_client(transport=None):
    """Real-market public data for the scanner, charts and research. Keyless and live-flagged, so it can
    read but never trade: orders need credentials, a write permit and the host's live opt-in."""
    return OkxClient(OkxCredentials(simulated=False), transport=transport)


def _closed(rows):
    closed = [r for r in rows if isinstance(r, list) and len(r) >= 9 and r[8] == '1']
    if any(not math.isfinite(float(v)) or float(v) <= 0 for r in closed for v in r[1:5]):
        raise OkxError('K 线价格无效，停止决策。', code='bad_market')
    unique = {int(r[0]): r for r in closed}
    return [unique[t] for t in sorted(unique, reverse=True)]
