"""Read-only MCP server: lets an agent (pi, OpenClaw, Claude Code, Codex...) look at Sentinel.

Run it as a local stdio server, e.g. ``uv run python -m lab.mcp_server``. It can read public OKX market
data, run the same backtests as the desk, and read trial ledgers, orders and the event log. It cannot
place, cancel or change anything, and it never opens the encrypted credential files: the journal is
opened in SQLite read-only mode and no tool touches keys, the access code or exchange balances.

Everything a tool returns is data for the agent to report, not an instruction to follow.
"""
from __future__ import annotations
import asyncio
import os
import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from .grid import open_orders
from .models import ResearchIn
from .okx import PAIR_RE, OkxError, market_client
from .research import backtest, grid_backtest

ROOT = Path(__file__).resolve().parents[1]
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)
MARKET = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True)

INSTRUCTIONS = """Sentinel for OKX is a local spot trading desk. This server is read-only: it shows market data,
backtests and the desk's trials, and cannot trade, cancel, start or stop anything. Changes are made by the
person in the Sentinel web console. Report losses, errors and paused trials plainly. Backtests are short
historical samples, not forecasts; never present a result as expected or guaranteed profit."""


class Journal:
    """The desk's SQLite journal opened read-only; missing data reads as empty, nothing is created."""

    def __init__(self, directory):
        self.path = Path(directory) / 'lab.sqlite3'

    def _rows(self, sql, args=()):
        if not self.path.exists():
            return []
        db = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def get(self, key):
        rows = self._rows('SELECT value FROM state WHERE key=?', (key,))
        return json.loads(rows[0][0]) if rows else None

    def events(self, limit):
        return [{'time': _iso(ts), 'kind': kind, 'message': message}
                for ts, kind, message in self._rows('SELECT ts,kind,message FROM events ORDER BY id DESC LIMIT ?', (limit,))]


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if ts else None


def _pair(pair):
    pair = pair.strip().upper()
    if not PAIR_RE.fullmatch(pair):
        raise ValueError('交易对格式应为 XXX-USDT')
    return pair


def trial_summary(state):
    plan = state['plan']
    pnl = state['equity'] - plan['budget']
    out = {
        'pair': plan['pair'], 'strategy': plan.get('strategy'), 'running': state['running'],
        'status': state['reason'], 'error': state.get('error') or None, 'risk_stopped': state.get('risk_stopped', False),
        'budget_usdt': plan['budget'], 'loss_trigger_usdt': plan['max_loss'],
        'equity_usdt': round(state['equity'], 4), 'pnl_usdt': round(pnl, 4), 'pnl_pct': round(pnl / plan['budget'] * 100, 3),
        'cash_usdt': round(state['cash'], 4), 'coin_quantity': state['quantity'], 'last_price': state['price'],
        'started': _iso(state.get('started')), 'expires': _iso(state.get('expires')), 'last_check': _iso(state.get('last_tick')),
        'order_awaiting_reconciliation': bool(state.get('pending')),
        'note': '账本只记本试验的资金与持仓，不是交易所账户余额；盈亏含已对账手续费。',
    }
    if plan.get('strategy') == 'grid' and state.get('grid'):
        grid = state['grid']
        out['grid'] = {
            'step_pct': plan['grid_step_pct'], 'levels': plan['grid_levels'], 'anchor_price': grid['anchor'],
            'round_trips': grid['round_trips'], 'resting_orders': len(open_orders(state)),
            'levels_detail': [{'level': lv['k'], 'buy_px': lv['buy_px'], 'sell_px': lv['sell_px'], 'held_qty': lv['qty'],
                               'order': {k: lv['order'][k] for k in ('side', 'px', 'sz', 'status')} if lv['order'] else None}
                              for lv in grid['levels']]}
    if plan.get('strategy') == 'llm':
        out['llm'] = {'provider': plan.get('llm_provider'), 'model': plan.get('llm_model'), 'calls': state.get('llm_calls', 0)}
    return out


def _segment(seg):
    keep = ('return_pct', 'max_drawdown_pct', 'benchmark_return_pct', 'orders', 'round_trips', 'costs', 'stopped')
    out = {k: (round(seg[k], 3) if isinstance(seg[k], float) else seg[k]) for k in keep if k in seg}
    out.update(start=_iso(seg['start'] / 1000), end=_iso(seg['end'] / 1000))
    return out


def create_server(data_dir=None, *, market_transport=None):
    journal = Journal(data_dir or os.getenv('OKX_DESK_DATA', str(ROOT / 'data')))
    server = FastMCP('sentinel-okx', instructions=INSTRUCTIONS, log_level='WARNING')
    research_lock = asyncio.Lock()

    def market():
        return market_client(market_transport)

    @server.tool(annotations=MARKET)
    async def market_snapshot(pair: str = 'BTC-USDT', hours: int = 48) -> dict:
        """Live OKX spot price, 24h change and recent closed hourly closes for one USDT pair (public data, no keys)."""
        pair, hours = _pair(pair), max(1, min(int(hours), 300))
        client = market()
        ticker, candles = await asyncio.gather(client.ticker(pair), client.recent_candles(pair, bar='1H', limit=hours + 1))
        closes = [[_iso(int(r[0]) / 1000), float(r[4])] for r in reversed(candles[:hours])]
        return {'pair': pair, 'price': ticker['price'], 'open_24h': ticker['open24h'],
                'change_24h_pct': round((ticker['price'] / ticker['open24h'] - 1) * 100, 3), 'hourly_closes_utc': closes}

    @server.tool(annotations=READ)
    async def scanner_results(limit: int = 20) -> dict:
        """The coin radar's latest saved scan: which OKX USDT pairs passed the tradability filters, and why others did not.
        The momentum score is an ordering only; research found it had no predictive power on OKX."""
        scan = journal.get('scanner')
        if not scan:
            return {'as_of': None, 'rows': [], 'note': '尚未扫描；请在 Sentinel 网页点「刷新雷达」。'}
        keys = ('rank', 'pair', 'score', 'price', 'volume_24h', 'spread_pct', 'mom7_pct', 'mom28_pct', 'rel_btc28_pct', 'vol_pct', 'trend_up')
        return {'as_of': _iso(scan.get('as_of')), 'listed_spot_pairs': scan.get('listed'), 'passed': len(scan.get('rows', [])),
                'excluded': scan.get('excluded'), 'rows': [{k: r.get(k) for k in keys} for r in scan.get('rows', [])[:max(1, min(limit, 200))]],
                'note': '得分只是排序，不是收益预测。'}

    @server.tool(annotations=MARKET)
    async def run_backtest(pair: str = 'BTC-USDT', strategy: Literal['grid', 'rsi', 'range'] = 'grid', budget: float = 1000,
                           max_loss: float = 150, grid_step_pct: float = 2, grid_levels: int = 10,
                           maker_fee_bps: float = 8, fee_bps: float = 10, slippage_bps: float = 5) -> dict:
        """Backtest a Sentinel strategy on about 100 days of OKX hourly data with fees, exactly as the web console does.
        Results are split into a development period and a later holdout; read the holdout. Not a forecast."""
        spec = ResearchIn(pair=_pair(pair), strategy=strategy, budget=budget, max_loss=max_loss, grid_step_pct=grid_step_pct,
                          grid_levels=grid_levels, maker_fee_bps=maker_fee_bps, fee_bps=fee_bps, slippage_bps=slippage_bps,
                          max_orders_day=500 if strategy == 'grid' else 4)
        if research_lock.locked():
            raise ValueError('已有回测在运行，请稍后再试')
        async with research_lock:
            candles = await market().candles(spec.pair, pages=8, limit=300)
            result = await asyncio.to_thread(grid_backtest if strategy == 'grid' else backtest, candles, spec)
        return {'pair': result['pair'], 'strategy': strategy, 'days': round(result['sample_days'], 1),
                'settings': spec.model_dump(include={'budget', 'max_loss', 'grid_step_pct', 'grid_levels', 'fee_bps',
                                                     'maker_fee_bps', 'slippage_bps'}),
                'development': _segment(result['development']), 'holdout': _segment(result['holdout']), 'note': result['note']}

    @server.tool(annotations=READ)
    async def trial_status(mode: Literal['demo', 'live'] = 'demo') -> dict:
        """The current trial in demo or live mode: plan, running state, ledger P&L, errors, and for a grid each level's order."""
        state = journal.get('session:' + mode)
        if not state:
            return {'mode': mode, 'trial': None, 'note': '此模式下没有进行中的试验。'}
        return {'mode': mode, 'trial': trial_summary(state)}

    @server.tool(annotations=READ)
    async def recent_orders(mode: Literal['demo', 'live'] = 'demo', limit: int = 20) -> dict:
        """Reconciled fills of the current trial, newest first (price, size, fee, reason)."""
        state = journal.get('session:' + mode) or {}
        keep = ('side', 'pair', 'quantity', 'price', 'fee', 'fee_ccy', 'state', 'reason')
        orders = [{**{k: o.get(k) for k in keep}, 'time': _iso(o.get('ts'))} for o in reversed(state.get('orders', []))]
        return {'mode': mode, 'orders': orders[:max(1, min(limit, 200))]}

    @server.tool(annotations=READ)
    async def recent_events(limit: int = 30) -> dict:
        """The desk's event log, newest first: starts, pauses, intents, fills, risk stops, errors and research runs."""
        return {'events': journal.events(max(1, min(limit, 200)))}

    return server


def main():
    import logging
    logging.getLogger('httpx').setLevel(logging.WARNING)  # stdout is the protocol; keep stderr quiet too
    try:
        create_server().run('stdio')
    except OkxError as exc:  # pragma: no cover - surfaced to the client as a startup failure
        raise SystemExit(exc.message)


if __name__ == '__main__':
    main()
