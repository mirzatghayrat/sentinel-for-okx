"""Plain-text reports for chat delivery (e.g. an OpenClaw command automation).

    uv run python -m lab.report daily     # a summary of the demo and live trials
    uv run python -m lab.report alerts    # what is new since the last call, or NO_REPLY

Deterministic and model-free. It reads the journal and the proposal queue read-only; the only file it
writes is its own cursor (``data/report-cursor.json``) so each alert is sent once. Messages carry the
trials' own ledger numbers, never keys, account identifiers or exchange balances. They pass through the
chat service you deliver them to.
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from .grid import open_orders
from .proposals import Proposals

ROOT = Path(__file__).resolve().parents[1]
SILENT = 'NO_REPLY'  # OpenClaw posts nothing for this output
ALERT_KINDS = {'risk': '触发亏损线', 'error': '异常', 'pause': '暂停', 'archive': '归档', 'agent': 'Agent', 'start': '启动'}
CONSOLE = 'http://127.0.0.1:8765'


def _local(ts):
    return datetime.fromtimestamp(ts).strftime('%m-%d %H:%M') if ts else '—'


class Desk:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.db = self.directory / 'lab.sqlite3'

    def _rows(self, sql, args=()):
        if not self.db.exists():
            return []
        db = sqlite3.connect(f'file:{self.db}?mode=ro', uri=True)
        try:
            return db.execute(sql, args).fetchall()
        finally:
            db.close()

    def get(self, key):
        rows = self._rows('SELECT value FROM state WHERE key=?', (key,))
        return json.loads(rows[0][0]) if rows else None

    def events_after(self, last_id):
        return self._rows('SELECT id, ts, kind, message FROM events WHERE id > ? ORDER BY id LIMIT 50', (last_id,))

    def last_event_id(self):
        rows = self._rows('SELECT MAX(id) FROM events')
        return (rows[0][0] or 0) if rows else 0

    def pending(self):
        return Proposals(self.directory, create=False).list(20, 'pending')


def trial_lines(mode, state, now):
    label = '模拟盘' if mode == 'demo' else '实盘'
    if not state:
        return [f'【{label}】没有进行中的试验']
    plan = state['plan']
    pnl = state['equity'] - plan['budget']
    status = '运行中' if state['running'] else '亏损线已停止' if state.get('risk_stopped') else '已暂停'
    strategy = (f"网格 {plan['grid_levels']}×{plan['grid_step_pct']:g}%" if plan.get('strategy') == 'grid'
                else {'rsi': 'RSI', 'range': '区间', 'llm': 'LLM'}.get(plan.get('strategy'), plan.get('strategy')))
    lines = [f"【{label}】{plan['pair']} · {strategy} · {status}",
             f"净值 {state['equity']:.2f} USDT（{'+' if pnl >= 0 else ''}{pnl:.2f}，{pnl / plan['budget'] * 100:+.2f}%）· 距亏损线 {plan['max_loss'] + pnl:.2f} USDT"]
    fills = [o for o in state.get('orders', []) if now - o.get('ts', 0) < 86400]
    extra = f"24 小时成交 {len(fills)} 笔"
    if plan.get('strategy') == 'grid' and state.get('grid'):
        grid = state['grid']
        held = sum(1 for lv in grid['levels'] if lv['qty'] > 0)
        extra += f" · 累计低买高卖 {grid['round_trips']} 次 · 挂单 {len(open_orders(state))} 单 · 持仓 {held} 格"
    lines.append(extra)
    if state.get('error'):
        lines.append(f"⚠️ {state['error']}")
    if state.get('expires') and state['running']:
        lines.append(f"到期 {_local(state['expires'])}")
    return lines


def daily(desk, now=None):
    now = now or time.time()
    out = [f'哨兵日报 · {_local(now)}']
    for mode in ('demo', 'live'):
        out += [''] + trial_lines(mode, desk.get('session:' + mode), now)
    pending = desk.pending()
    if pending:
        out += ['', f'有 {len(pending)} 个 agent 提案等你审批：{CONSOLE}']
    out += ['', '账本盈亏含已对账手续费，不是交易所余额；历史表现不代表未来。']
    return '\n'.join(out)


def alerts(desk, cursor_path, *, fills=False):
    cursor = {}
    if cursor_path.exists():
        try:
            cursor = json.loads(cursor_path.read_text())
        except ValueError:
            cursor = {}
    if 'event' not in cursor:  # first run: start from now instead of replaying history
        cursor = {'event': desk.last_event_id(), 'proposals': [p['id'] for p in desk.pending()]}
        _save(cursor_path, cursor)
        return SILENT
    lines = []
    last = cursor['event']
    for eid, ts, kind, message in desk.events_after(last):
        last = eid
        if kind in ALERT_KINDS or (fills and kind == 'fill'):
            lines.append(f"{_local(ts)} {'成交' if kind == 'fill' else ALERT_KINDS[kind]}：{message}")
    seen = set(cursor.get('proposals', []))
    pending = desk.pending()
    for p in pending:
        if p['id'] not in seen:
            what = '请求暂停' if p['kind'] == 'pause' else '试验方案'
            plan = p['payload'].get('plan') or {}
            detail = f" {plan.get('pair')} {plan.get('strategy')} 资金 {plan.get('budget')}" if plan else ''
            lines.append(f"新的 agent {what}（{'实盘' if p['mode'] == 'live' else '模拟盘'}）{detail}，请到 {CONSOLE} 审批")
    _save(cursor_path, {'event': last, 'proposals': sorted(seen | {p['id'] for p in pending})[-100:]})
    if not lines:
        return SILENT
    return '哨兵提醒\n' + '\n'.join(lines[-20:])


def _save(path, value):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value))
    tmp.replace(path)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    directory = Path(os.getenv('OKX_DESK_DATA', str(ROOT / 'data')))
    if not argv or argv[0] not in {'daily', 'alerts'}:
        print('用法：python -m lab.report daily | alerts [--fills]', file=sys.stderr)
        return 2
    if not (directory / 'lab.sqlite3').exists():
        print(f'找不到 Sentinel 数据：{directory}', file=sys.stderr)
        return 1
    desk = Desk(directory)
    print(daily(desk) if argv[0] == 'daily' else alerts(desk, directory / 'report-cursor.json', fills='--fills' in argv))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
