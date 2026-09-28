import asyncio
import json
import time
from copy import deepcopy
import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from lab.okx import OkxClient, OkxCredentials, OkxError, _sign
from lab.models import Plan, ResearchIn
from lab.engine import Engine, initial_session, apply_fill
from lab.storage import Store
from lab.app import create_app
from lab.rsi import _wilder_rsi
from lab.research import backtest


def run(coro): return asyncio.run(coro)
def plan(**kw): return Plan(**dict({'budget':500,'max_loss':25},**kw))
def bars(n=400):
    end=int(time.time()//3600)*3600000-3600000
    return [[str(end-i*3600000),str(100+i),str(102+i),str(99+i),str(100+i),'1','1','1','1'] for i in range(n)]

class FakeClient:
    def __init__(self): self.posts=[];self.fail=False;self.error=OkxError('timeout',code='network');self.fill={};self.candle_data=bars(100);self.last=100;self.account={'details':[{'ccy':'USDT','availBal':'10000'},{'ccy':'BTC','availBal':'10'}]}
    async def balance(self): return self.account
    async def instrument(self,pair): return {'lotSz':'0.00001','minSz':'0.00001'}
    async def ticker(self,pair): return {'price':self.last,'timestamp':int(time.time()*1000)}
    async def candles(self,pair,*,pages=1): return self.candle_data
    async def place_order(self,body,*,write_permit=False):
        assert write_permit
        self.posts.append(body)
        if self.fail: raise self.error
        return {'ordId':'123'}
    async def order(self,pair,client_id):
        if self.fail: raise OkxError('unknown')
        return {'instId':pair,'clOrdId':client_id,'ordId':'123','side':self.posts[-1]['side'],
                'state':'filled','accFillSz':'0.2','avgPx':'100','fee':'-0.0002','feeCcy':'BTC',**self.fill}

class OkxSim:
    """OKX V5 over MockTransport. Like the real API, the newest candle page starts with the in-progress bar."""
    def __init__(self): self.hour=int(time.time()//3600)*3600000;self.posts=[]
    def candle(self,ts):
        px=str(100+(self.hour-ts)//3600000*.5)  # falling prices -> RSI buy signal
        return [str(ts),px,px,px,px,'1','1','1','0' if ts==self.hour else '1']
    def __call__(self,req):
        path,q=req.url.path,dict(req.url.params)
        if path.endswith('/market/history-candles'):
            first=int(q['after'])-3600000 if 'after' in q else self.hour
            rows=[self.candle(first-i*3600000) for i in range(int(q['limit']))]
        elif path.endswith('/market/ticker'): rows=[{'last':'100','open24h':'101','ts':str(int(time.time()*1000))}]
        elif path.endswith('/public/instruments'): rows=[{'state':'live','minSz':'0.00001','lotSz':'0.00000001'}]
        elif path.endswith('/account/balance'): rows=[{'details':[{'ccy':'USDT','availBal':'10000'}]}]
        elif path.endswith('/trade/order') and req.method=='POST':
            body=json.loads(req.content);self.posts.append(body);rows=[{'ordId':'1','clOrdId':body['clOrdId'],'sCode':'0'}]
        else: return httpx.Response(404)
        return httpx.Response(200,json={'code':'0','data':rows})

@pytest.fixture
def setup(tmp_path):
    store=Store(tmp_path);fake=FakeClient();engine=Engine(store,client_factory=lambda mode:fake)
    return store,fake,engine

def test_demo_header_and_public_requests_do_not_leak_keys():
    seen=[]
    def handler(req): seen.append(req);return httpx.Response(200,json={'code':'0','data':[]})
    client=OkxClient(OkxCredentials('api-key','secret-value','passphrase'),transport=httpx.MockTransport(handler))
    run(client.request('GET','/market/ticker'))
    run(client.request('GET','/account/config'))
    assert 'ok-access-key' not in seen[0].headers
    assert seen[1].headers['x-simulated-trading']=='1'
    assert seen[1].headers['ok-access-sign']


def test_live_write_requires_both_runtime_optin_and_permit():
    body={'instId':'BTC-USDT','tdMode':'cash','side':'buy','ordType':'market','sz':'10'}
    calls=[]
    def handler(req):calls.append(req);return httpx.Response(200,json={'code':'0','data':[{'ordId':'x','sCode':'0'}]})
    for allow,permit in [(False,False),(False,True),(True,False)]:
        c=OkxClient(OkxCredentials('key','secret','pass',False),allow_live=allow,transport=httpx.MockTransport(handler))
        with pytest.raises(OkxError):run(c.place_order(body,write_permit=permit))
    assert not calls
    c=OkxClient(OkxCredentials('key','secret','pass',False),allow_live=True,transport=httpx.MockTransport(handler))
    run(c.place_order(body,write_permit=True))  # mock transport only; never contacts OKX
    assert 'x-simulated-trading' not in calls[0].headers

@pytest.mark.parametrize('path',['/asset/withdrawal','/asset/transfer','/account/set-leverage'])
def test_unapproved_financial_endpoints_are_blocked(path):
    with pytest.raises(OkxError):run(OkxClient().request('POST',path,write_permit=True))


def test_tick_with_real_okx_candle_pages_reaches_decision(tmp_path):
    sim=OkxSim()
    engine=Engine(Store(tmp_path),client_factory=lambda mode:OkxClient(OkxCredentials('key','secret','pass'),transport=httpx.MockTransport(sim)))
    run(engine.start(plan()));run(engine.tick('demo'))
    state=engine.session('demo')
    assert state['error']=='' and state['running'] and state['signal']['side']=='buy'
    assert len(sim.posts)==1 and sim.posts[0]['side']=='buy'


def test_order_rejection_reports_order_level_code():
    body={'instId':'BTC-USDT','tdMode':'cash','side':'buy','ordType':'market','sz':'10'}
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'code':'1','msg':'All operations failed','data':[{'ordId':'','sCode':'51008','sMsg':'Insufficient balance'}]}))
    with pytest.raises(OkxError) as err:run(OkxClient(OkxCredentials('key','secret','pass'),transport=transport).place_order(body,write_permit=True))
    assert err.value.code=='51008'


def test_per_order_rejection_is_not_success():
    transport=httpx.MockTransport(lambda req:httpx.Response(200,json={'code':'0','data':[{'ordId':'','sCode':'51008'}]}))
    with pytest.raises(OkxError):run(OkxClient(OkxCredentials('key','secret','pass'),transport=transport).place_order({'instId':'BTC-USDT','tdMode':'cash','side':'buy','ordType':'market','sz':'10'},write_permit=True))

@pytest.mark.parametrize('values',[{'budget':float('nan')},{'max_loss':500},{'hours':25},{'order_quote':200},{'max_position_pct':0}])
def test_bad_risk_settings_rejected(values):
    with pytest.raises(ValidationError):plan(**values)


def test_daily_cap_not_bypassed_by_manual_tick(setup):
    store,fake,engine=setup
    state=run(engine.start(plan(max_orders_day=1)))
    run(engine.tick('demo'));assert len(fake.posts)==1
    run(engine.tick('demo'));assert engine.session('demo')['pending'] is None
    fake.candle_data=[[str(int(r[0])+3600000),*r[1:]] for r in fake.candle_data]
    # Use a freshly completed next bar, and prior state candle one hour earlier.
    fake.candle_data=bars(100)
    state=engine.session('demo');state['last_candle']-=3600000;engine.save('demo',state)
    run(engine.tick('demo'));assert len(fake.posts)==1


def test_pending_intent_is_persistent_and_not_retried(setup):
    store,fake,engine=setup;run(engine.start(plan()));fake.fail=True
    run(engine.tick('demo'));first=engine.session('demo')
    assert first['pending'] and not first['running'] and len(fake.posts)==1
    run(engine.tick('demo'));assert len(fake.posts)==1
    with pytest.raises(ValueError):run(engine.start(plan()))
    restarted=Engine(store,client_factory=lambda mode:fake);restarted.restart_pause()
    run(restarted.tick('demo'));assert len(fake.posts)==1


def test_rejected_submission_stays_frozen_and_shows_code(setup):
    _,fake,engine=setup;run(engine.start(plan()));fake.fail=True;fake.error=OkxError('OKX 未接受订单（51008）。',code='51008')
    run(engine.tick('demo'));state=engine.session('demo')
    assert state['pending'] and not state['running'] and '51008' in state['error']
    run(engine.tick('demo'));assert len(fake.posts)==1


def test_expiry_stops_all_new_orders(setup):
    _,fake,engine=setup;state=run(engine.start(plan()));state['expires']=time.time()-1;engine.save('demo',state)
    run(engine.tick('demo'));assert not fake.posts;assert not engine.session('demo')['running']


def test_position_cap_includes_next_order(setup):
    _,fake,engine=setup;state=run(engine.start(plan()));state['quantity']=1.2;state['cash']=380;engine.save('demo',state)
    run(engine.tick('demo'));assert len(fake.posts)==1
    assert float(fake.posts[0]['sz'])+120<=125


def test_terminal_fill_fee_accounting_idempotent():
    s=initial_session(plan().model_dump(),time.time());s['pending']={'client_id':'test','side':'buy','reason':'x'}
    row={'instId':'BTC-USDT','clOrdId':'test','side':'buy','ordId':'123','accFillSz':'0.2','avgPx':'100','fee':'-0.0002','feeCcy':'BTC','state':'filled'}
    apply_fill(s,row);assert s['cash']==480;assert s['quantity']==pytest.approx(.1998)
    apply_fill(s,row);assert len(s['orders'])==1


def test_foreign_order_cannot_touch_ledger():
    s=initial_session(plan().model_dump(),time.time());s['pending']={'client_id':'x','side':'buy','reason':'x'}
    with pytest.raises(ValueError):apply_fill(s,{'instId':'BTC-USDT','clOrdId':'different','side':'buy'})
    assert s['cash']==500


def test_loss_exit_sells_only_owned_position(setup):
    _,fake,engine=setup;state=run(engine.start(plan()));state.update(cash=450,quantity=.1);engine.save('demo',state)
    run(engine.tick('demo'))
    assert len(fake.posts)==1 and fake.posts[0]['side']=='sell'
    assert float(fake.posts[0]['sz'])==.1  # account actually has 10 BTC
    assert not engine.session('demo')['running'] and engine.session('demo')['risk_stopped']


def test_fee_dust_after_full_exit_is_archived_on_record(setup):
    store,fake,engine=setup;run(engine.start(plan()));state=engine.session('demo')
    for i,(qty,px,fee) in enumerate([('0.00017123','116800.5','-0.00000017123'),('0.00017201','116270.1','-0.00000017201')]):
        state['pending']={'client_id':f'c{i}','side':'buy','reason':'x'}
        apply_fill(state,{'instId':'BTC-USDT','clOrdId':f'c{i}','side':'buy','ordId':str(i),'accFillSz':qty,'avgPx':px,'fee':fee,'feeCcy':'BTC','state':'filled'})
    engine.save('demo',state);run(engine.flatten('demo',''))
    sold=fake.posts[-1]['sz'];fake.fill={'accFillSz':sold,'avgPx':'100','fee':'-0.01','feeCcy':'USDT'}
    run(engine.tick('demo'));left=engine.session('demo')['quantity']
    assert 0<left<1e-5  # fee taken in BTC leaves a remainder below the lot size
    run(engine.finish('demo'))
    assert engine.session('demo') is None
    assert store.get('archive:'+state['id'])['dust']==pytest.approx(left)
    assert '零碎币' in store.events()[0]['message']


def test_sellable_position_blocks_archive(setup):
    _,_,engine=setup;run(engine.start(plan()));run(engine.pause('demo'))
    state=engine.session('demo');state.update(quantity=.1,cash=490);engine.save('demo',state)
    with pytest.raises(ValueError):run(engine.finish('demo'))
    assert engine.session('demo')['quantity']==.1


def test_loss_stopped_trial_with_dust_can_close_then_new_trial_starts(setup):
    _,_,engine=setup;run(engine.start(plan()));run(engine.pause('demo'))
    state=engine.session('demo');state.update(quantity=3e-9,cash=470,risk_stopped=True);engine.save('demo',state)
    with pytest.raises(ValueError):run(engine.start(plan()))
    run(engine.finish('demo'));assert run(engine.start(plan()))['id']!=state['id']


def test_restart_never_rearms(setup):
    _,_,engine=setup;run(engine.start(plan()));engine.restart_pause();assert not engine.session('demo')['running']


def test_simultaneous_ticks_cannot_double_submit(setup):
    _,fake,engine=setup
    async def exercise():
        await engine.start(plan());await asyncio.gather(engine.tick('demo'),engine.tick('demo'),engine.tick('demo'))
    run(exercise());assert len(fake.posts)==1


def test_cannot_change_budget_with_existing_ledger(setup):
    _,_,engine=setup;run(engine.start(plan()));run(engine.pause('demo'))
    with pytest.raises(ValueError):run(engine.start(plan(budget=1000)))


def test_live_start_needs_exact_confirmation_and_host_flag(setup):
    _,fake,engine=setup
    with pytest.raises(ValueError):run(engine.start(plan(mode='live',confirmation='我自行启用实盘')))
    engine.allow_live=True
    with pytest.raises(ValueError):run(engine.start(plan(mode='live')))
    assert not fake.posts


def test_credentials_encrypted_and_separated(tmp_path):
    store=Store(tmp_path);creds=OkxCredentials('test-api-key','test-secret','test-passphrase')
    store.save_credentials(creds,'demo')
    assert b'test-secret' not in (tmp_path/'demo.enc').read_bytes()
    assert not store.credentials('live').is_complete
    assert store.credentials('demo').api_secret=='test-secret'
    assert (tmp_path/'demo.enc').stat().st_mode & 0o777 == 0o600


def test_access_control_csrf_and_no_secret_echo(tmp_path):
    store=Store(tmp_path);app=create_app(store,allow_live=False,background=False)
    with TestClient(app) as client:
        assert client.get('/api/status').status_code==401
        assert client.post('/api/login',json={'code':'x'}).status_code==403
        headers={'X-Desk-Request':'1'}
        assert client.post('/api/login',json={'code':(tmp_path/'access-code').read_text()},headers=headers).status_code==200
        assert client.get('/api/status').json()['live_enabled'] is False
        assert client.post('/api/pause',json={'mode':'demo'},headers={**headers,'Origin':'https://evil.example'}).status_code==403
        resp=client.post('/api/credentials',json={'mode':'demo','api_key':'secret-sensitive-marker','api_secret':'short','passphrase':'password-marker'},headers=headers)
        assert resp.status_code==422
        assert 'secret-sensitive-marker' not in resp.text and 'password-marker' not in resp.text
        assert client.get('/',headers={'Host':'evil.example'}).status_code==400


def test_flat_rsi_and_backtest_does_not_read_future():
    assert _wilder_rsi([100]*100,14)==50
    data=bars(400);spec=ResearchIn()
    before=backtest(data,spec)
    changed=deepcopy(data);changed[0][4]='1000'
    after=backtest(changed,spec)
    assert before['development']==after['development']
    assert before['holdout']['curve'][:-1]==after['holdout']['curve'][:-1]


def test_backtest_rejects_data_gaps():
    data=bars(400);del data[200]
    with pytest.raises(ValueError):backtest(data,ResearchIn())
