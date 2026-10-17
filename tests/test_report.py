import json
from lab.engine import Engine
from lab.okx import OkxCredentials
from lab.proposals import Proposals
from lab.report import SILENT, Desk, alerts, daily, main
from lab.storage import Store
from test_grid import Book, grid_plan
from test_safety import run


def desk_with_grid(tmp_path):
    store = Store(tmp_path)
    store.save_credentials(OkxCredentials('api-key-marker', 'secret-marker', 'passphrase-marker'), 'demo')
    book = Book()
    engine = Engine(store, client_factory=lambda mode: book)
    run(engine.start(grid_plan())); run(engine.tick('demo'))
    book.last = 98.; book.fill([r for r in book.live('buy') if r['px'] == '98'][0]); run(engine.tick('demo'))
    return store, book, engine


def test_daily_report_summarises_both_modes(tmp_path):
    desk_with_grid(tmp_path)
    text = daily(Desk(tmp_path))
    assert '【模拟盘】BTC-USDT · 网格 5×2% · 运行中' in text and '【实盘】没有进行中的试验' in text
    assert '24 小时成交 1 笔' in text and '挂单 5 单' in text and '距亏损线' in text
    assert 'marker' not in text


def test_alerts_start_quiet_then_report_each_new_event_once(tmp_path):
    store, book, engine = desk_with_grid(tmp_path)
    cursor = tmp_path / 'report-cursor.json'
    assert alerts(Desk(tmp_path), cursor) == SILENT  # first run only sets the cursor
    assert alerts(Desk(tmp_path), cursor) == SILENT
    run(engine.pause('demo'))
    store.event('error', '网格挂单提交结果未确认，已暂停')
    text = alerts(Desk(tmp_path), cursor)
    assert '暂停：' in text and '异常：' in text
    assert alerts(Desk(tmp_path), cursor) == SILENT


def test_fills_only_when_asked_and_new_proposals_are_announced_once(tmp_path):
    store, book, engine = desk_with_grid(tmp_path)
    cursor = tmp_path / 'report-cursor.json'
    alerts(Desk(tmp_path), cursor)
    store.event('fill', '网格第 1 格卖出成交')
    assert alerts(Desk(tmp_path), cursor) == SILENT
    store.event('fill', '网格第 2 格买入成交')
    assert '成交：网格第 2 格' in alerts(Desk(tmp_path), cursor, fills=True)
    Proposals(tmp_path).add('trial', 'live', {'plan': {'pair': 'ETH-USDT', 'strategy': 'grid', 'budget': 300}}, 'x')
    text = alerts(Desk(tmp_path), cursor)
    assert '新的 agent 试验方案（实盘） ETH-USDT grid 资金 300' in text and '127.0.0.1:8765' in text
    assert alerts(Desk(tmp_path), cursor) == SILENT
    assert '1 个 agent 提案等你审批' in daily(Desk(tmp_path))


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('OKX_DESK_DATA', str(tmp_path / 'absent'))
    assert main(['daily']) == 1 and not (tmp_path / 'absent').exists()
    desk_with_grid(tmp_path)
    monkeypatch.setenv('OKX_DESK_DATA', str(tmp_path))
    assert main(['alerts']) == 0 and capsys.readouterr().out.strip() == SILENT
    assert json.loads((tmp_path / 'report-cursor.json').read_text())['event'] > 0
    assert main(['weekly']) == 2
