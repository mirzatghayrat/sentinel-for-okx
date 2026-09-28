import json
import time
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from lab.app import create_app
from lab.engine import Engine
from lab.llm import LlmError, OpenRouterClient, parse, snapshot
from lab.storage import Store
from test_safety import FakeClient, bars, plan, run

MODEL = 'x-ai/test-model'


class FakeAdviser:
    def __init__(self): self.calls=[];self.side='buy';self.error=None;self.during=None
    async def has_model(self,model): return model == MODEL
    async def decide(self,model,view):
        self.calls.append((model,view))
        if self.during: await self.during()
        if self.error: raise self.error
        return {'side':self.side,'confidence':70,'reason':'测试理由','cost':.002,'seconds':1.}

def llm_plan(**kw): return plan(strategy='llm',llm_model=MODEL,**kw)

@pytest.fixture
def desk(tmp_path):
    store=Store(tmp_path);store.save_secret('openrouter','sk-or-test-key')
    fake,adviser=FakeClient(),FakeAdviser()
    engine=Engine(store,client_factory=lambda mode:fake,adviser_factory=lambda:adviser)
    return store,fake,adviser,engine


def test_model_asked_once_per_closed_bar_and_order_goes_through_engine(desk):
    _,fake,adviser,engine=desk;run(engine.start(llm_plan()));run(engine.tick('demo'))
    assert len(adviser.calls)==1 and len(fake.posts)==1 and fake.posts[0]['side']=='buy'
    state=engine.session('demo')
    assert state['llm_calls']==1 and state['llm_cost']==pytest.approx(.002) and state['advice'][-1]['side']=='buy'
    view=adviser.calls[0][1]
    assert len(view['candles_utc_open_high_low_close_volume'])==48 and 'sk-or' not in json.dumps(view)
    run(engine.tick('demo'));run(engine.tick('demo'))  # reconcile, then same bar again
    assert len(adviser.calls)==1 and len(fake.posts)==1


def test_model_failure_holds_without_pausing(desk):
    _,fake,adviser,engine=desk;run(engine.start(llm_plan()));adviser.error=LlmError('OpenRouter 返回 HTTP 500')
    run(engine.tick('demo'));state=engine.session('demo')
    assert not fake.posts and state['running'] and not state['error'] and 'HTTP 500' in state['reason']
    run(engine.tick('demo'));assert len(adviser.calls)==1


def test_pause_during_model_call_blocks_the_order(desk):
    _,fake,adviser,engine=desk;run(engine.start(llm_plan()))
    async def pause(): await engine.pause('demo')
    adviser.during=pause
    run(engine.tick('demo'));assert not fake.posts and not engine.session('demo')['running']
    adviser.during=None;run(engine.start(llm_plan()));run(engine.tick('demo'))
    assert not fake.posts  # the stale answer is dropped on restart


def test_model_not_asked_when_daily_cap_used(desk):
    _,fake,adviser,engine=desk;state=run(engine.start(llm_plan(max_orders_day=1)))
    state['days']={time.strftime('%Y-%m-%d',time.gmtime()):1};engine.save('demo',state)
    run(engine.tick('demo'));assert not adviser.calls and not fake.posts


def test_loss_line_exit_does_not_wait_for_model(desk):
    _,fake,adviser,engine=desk;state=run(engine.start(llm_plan()))
    state.update(cash=450,quantity=.1);engine.save('demo',state)
    run(engine.tick('demo'))
    assert not adviser.calls and fake.posts[0]['side']=='sell' and engine.session('demo')['risk_stopped']


def test_sell_advice_without_position_is_not_executed(desk):
    _,fake,adviser,engine=desk;run(engine.start(llm_plan()));adviser.side='sell'
    run(engine.tick('demo'));assert not fake.posts and '无持仓' in engine.session('demo')['reason']


def test_llm_start_requires_key_and_known_model(tmp_path):
    fake,adviser=FakeClient(),FakeAdviser();store=Store(tmp_path)
    engine=Engine(store,client_factory=lambda mode:fake,adviser_factory=lambda:adviser)
    with pytest.raises(ValueError):run(engine.start(llm_plan()))
    store.save_secret('openrouter','sk-or-test-key')
    with pytest.raises(ValueError):run(engine.start(plan(strategy='llm',llm_model='x-ai/unknown')))
    for model in ['','not a model','https://evil.example/x']:
        with pytest.raises(ValidationError):plan(strategy='llm',llm_model=model)


def test_snapshot_uses_closed_bars_only():
    data=bars(100);newest=int(data[0][0])+3600000
    data.insert(0,[str(newest),'999','999','999','999','1','1','1','0'])
    state={'plan':llm_plan().model_dump(),'price':100.,'cash':500.,'quantity':0.,'days':{}}
    view=snapshot(data,state)
    assert all(row[4]!=999 for row in view['candles_utc_open_high_low_close_volume'])
    assert view['last_closed_bar_utc']==time.strftime('%Y-%m-%d %H:00',time.gmtime(int(data[1][0])/1000))


def test_openrouter_request_shape_and_answer_parsing():
    seen=[]
    def handler(req):
        seen.append(req)
        content='```json\n{"decision":"BUY","confidence":72,"reason":"超卖\\n反弹"}\n```'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}],'usage':{'cost':.0012}})
    view={'pair':'BTC-USDT','price':100}
    advice=run(OpenRouterClient('sk-or-test',transport=httpx.MockTransport(handler)).decide(MODEL,view))
    assert advice['side']=='buy' and advice['confidence']==72 and advice['reason']=='超卖 反弹' and advice['cost']==.0012
    req=seen[0];body=json.loads(req.content)
    assert str(req.url)=='https://openrouter.ai/api/v1/chat/completions' and req.headers['authorization']=='Bearer sk-or-test'
    assert body['model']==MODEL and body['temperature']==0 and json.loads(body['messages'][1]['content'])==view


@pytest.mark.parametrize('content',[None,'','buy now!','{"decision":"all-in"}','{"decision":["buy"]}'])
def test_answers_outside_offered_choices_are_hold(content):
    assert parse(content)['side']=='hold'


def test_invalid_confidence_is_dropped_but_choice_kept():
    advice=parse('{"decision":"sell","confidence":"high","reason":"<b>x</b>"}')
    assert advice['side']=='sell' and advice['confidence'] is None


def test_openrouter_errors_do_not_echo_key_or_body():
    calls=[]
    def handler(req):
        calls.append(req);return httpx.Response(401,json={'error':{'message':'bad key sk-or-body-marker'}})
    with pytest.raises(LlmError) as err:run(OpenRouterClient('sk-or-test',transport=httpx.MockTransport(handler)).key_info())
    assert 'marker' not in err.value.message and 'sk-or' not in err.value.message
    with pytest.raises(LlmError):run(OpenRouterClient('',transport=httpx.MockTransport(handler)).decide(MODEL,{}))
    assert len(calls)==1  # a missing key never reaches the network


def test_llm_key_endpoint_saves_encrypted_without_echo(tmp_path):
    def handler(req):
        ok=req.headers['authorization']=='Bearer sk-or-secret-marker' and req.url.path=='/api/v1/key'
        return httpx.Response(200,json={'data':{'limit':5,'limit_remaining':4.5,'usage':.5}}) if ok else httpx.Response(401)
    store=Store(tmp_path);app=create_app(store,allow_live=False,background=False,llm_transport=httpx.MockTransport(handler))
    headers={'X-Desk-Request':'1'}
    with TestClient(app) as client:
        client.post('/api/login',json={'code':(tmp_path/'access-code').read_text()},headers=headers)
        assert client.post('/api/llm-key',json={'api_key':'sk-or-wrong-key'},headers=headers).status_code==502
        assert client.get('/api/status').json()['llm_key'] is False
        resp=client.post('/api/llm-key',json={'api_key':'sk-or-secret-marker'},headers=headers)
        assert resp.status_code==200 and resp.json()['limit']==5 and 'marker' not in resp.text
        assert client.get('/api/status').json()['llm_key'] is True
    assert b'marker' not in (tmp_path/'openrouter.enc').read_bytes() and store.secret('openrouter')=='sk-or-secret-marker'
