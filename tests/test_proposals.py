import time
import pytest
from fastapi.testclient import TestClient
from lab.app import create_app
from lab.mcp_server import create_server
from lab.proposals import MAX_PENDING, Proposals
from lab.storage import Store
from test_grid import Book
from test_mcp import call, market_transport
from test_safety import run

HEADERS = {'X-Desk-Request': '1'}


@pytest.fixture
def desk(tmp_path, monkeypatch):
    store = Store(tmp_path)
    book = Book()
    app = create_app(store, allow_live=False, background=False, market_transport=market_transport())
    app.state.engine.client_factory = lambda mode: book
    client = TestClient(app)
    client.__enter__()
    client.post('/api/login', json={'code': (tmp_path / 'access-code').read_text()}, headers=HEADERS)
    agent = create_server(tmp_path, market_transport=market_transport())
    yield store, book, app, client, agent
    client.__exit__(None, None, None)


def propose(agent, **kw):
    return call(agent, 'propose_trial', **{'rationale': '震荡行情，网格回测后段为正；风险：单边下跌会被套', 'grid_levels': 5, **kw})


def test_agent_proposal_waits_for_approval_and_then_starts_through_the_engine(desk):
    store, book, app, client, agent = desk
    filed = propose(agent)
    assert filed['status'] == 'pending' and 'holdout' in str(filed)
    run(app.state.agent_step())
    assert not store.get('session:demo') and not book.posts  # nothing runs on its own
    listed = client.get('/api/proposals').json()['proposals']
    assert listed[0]['payload']['plan']['strategy'] == 'grid' and listed[0]['payload']['backtest']['holdout']
    assert client.post('/api/proposals/approve', json={'id': filed['proposal_id']}, headers=HEADERS).status_code == 200
    assert store.get('session:demo')['running'] and store.get('session:demo')['plan']['grid_levels'] == 5
    assert call(agent, 'list_proposals')['proposals'][0]['status'] == 'approved'
    assert client.post('/api/proposals/approve', json={'id': filed['proposal_id']}, headers=HEADERS).status_code == 400


def test_rejected_proposal_never_runs(desk):
    store, book, app, client, agent = desk
    filed = propose(agent)
    assert client.post('/api/proposals/reject', json={'id': filed['proposal_id']}, headers=HEADERS).status_code == 200
    assert client.post('/api/proposals/approve', json={'id': filed['proposal_id']}, headers=HEADERS).status_code == 400
    assert not store.get('session:demo')


def test_live_proposal_needs_the_host_switch_and_confirmation(desk):
    store, book, app, client, agent = desk
    filed = propose(agent, mode='live')
    store.set('agent_settings', {'auto_demo': True})
    run(app.state.agent_step())
    assert Proposals(store.directory).get(filed['proposal_id'])['status'] == 'pending'  # never auto-started
    response = client.post('/api/proposals/approve', json={'id': filed['proposal_id'], 'confirmation': '我自行启用实盘'}, headers=HEADERS)
    assert response.status_code == 400 and not store.get('session:live')  # host not started with --allow-live
    assert Proposals(store.directory).get(filed['proposal_id'])['status'] == 'pending'


def test_demo_auto_start_only_when_the_person_enabled_it(desk):
    store, book, app, client, agent = desk
    filed = propose(agent)
    assert client.post('/api/agent-settings', json={'auto_demo': True}, headers=HEADERS).status_code == 200
    run(app.state.agent_step())
    assert store.get('session:demo')['running']
    assert Proposals(store.directory).get(filed['proposal_id'])['status'] == 'approved'
    second = propose(agent, pair='ETH-USDT')  # a trial already runs: the engine refuses, the proposal closes as failed
    run(app.state.agent_step())
    assert Proposals(store.directory).get(second['proposal_id'])['status'] == 'failed'


def test_pause_request_is_carried_out(desk):
    store, book, app, client, agent = desk
    filed = propose(agent)
    client.post('/api/proposals/approve', json={'id': filed['proposal_id']}, headers=HEADERS)
    call(agent, 'request_pause', reason='BTC 一小时跌 6%，先暂停网格')
    run(app.state.agent_step())
    assert not store.get('session:demo')['running']
    assert any('先暂停网格' in e['message'] for e in store.events())


def test_agent_input_is_validated_bounded_and_rate_limited(desk):
    store, book, app, client, agent = desk
    for bad in ({'budget': 100, 'max_loss': 200}, {'pair': 'BTC/USDT'}, {'grid_levels': 50}, {'rationale': '   '}):
        with pytest.raises(Exception):
            run(agent.call_tool('propose_trial', {'rationale': 'x', 'grid_levels': 5, **bad}))
    for _ in range(MAX_PENDING):
        propose(agent, rationale='a' * 2000 + '\x07')
    with pytest.raises(Exception):
        propose(agent)
    note = Proposals(store.directory).list(1)[0]['note']
    assert len(note) == 500 and '\x07' not in note


def test_stale_proposals_expire(desk):
    store, book, app, client, agent = desk
    filed = propose(agent)
    queue = Proposals(store.directory)
    with queue._db() as db:
        db.execute('UPDATE proposals SET created=? WHERE id=?', (time.time() - 2 * 86400, filed['proposal_id']))
    run(app.state.agent_step())
    assert queue.get(filed['proposal_id'])['status'] == 'expired'
