from __future__ import annotations
import asyncio
import contextlib
import fcntl
import hmac
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .storage import Store
from .okx import OkxClient, OkxCredentials, OkxError, PAIRS
from .models import Plan, CredentialsIn, LlmKeyIn, ResearchIn, LoginIn, ModeIn, ActionIn
from .engine import Engine
from .llm import ADVISERS, PROVIDER_NAMES, LlmError
from .research import backtest

ROOT = Path(__file__).resolve().parents[1]

def create_app(store=None, allow_live=None, background=True, llm_transport=None):
    store = store or Store(os.getenv('OKX_DESK_DATA', str(ROOT/'data')))
    allow_live = os.getenv('OKX_DESK_ALLOW_LIVE') == '1' if allow_live is None else allow_live
    engine = Engine(store,allow_live,adviser_factory=lambda provider: ADVISERS[provider](store.secret(provider),transport=llm_transport))
    code_path = store.directory/'access-code'
    if not code_path.exists():
        fd = os.open(code_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w') as f: f.write(secrets.token_urlsafe(24))
    access_code = code_path.read_text().strip()
    session_cookie = secrets.token_urlsafe(40)
    # Local-only desk: the browser on this Mac is the only accepted origin.
    origins = {'http://127.0.0.1:8765','http://localhost:8765'}
    hosts = ['127.0.0.1','localhost','testserver']
    login_attempts = []
    research_lock = asyncio.Lock()

    @asynccontextmanager
    async def lifespan(app):
        with (store.directory/'process.lock').open('w') as lock:
            try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError: raise RuntimeError('已有一个 Sentinel for OKX 实例，禁止多实例重复交易')
            engine.restart_pause()
            task = asyncio.create_task(engine.loop()) if background else None
            try: yield
            finally:
                if task:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError): await task
                fcntl.flock(lock,fcntl.LOCK_UN)

    app = FastAPI(title='Sentinel for OKX',docs_url=None,redoc_url=None,openapi_url=None,lifespan=lifespan)
    app.state.store, app.state.engine = store, engine
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)

    @app.middleware('http')
    async def guard(request, call_next):
        path = request.url.path
        if request.method not in {'GET','HEAD'}:
            origin = request.headers.get('origin')
            if request.headers.get('x-desk-request') != '1' or (origin and origin not in origins):
                return JSONResponse({'message':'请求来源校验失败'},status_code=403)
            length = request.headers.get('content-length','0')
            if not length.isdigit() or int(length)>16384:
                return JSONResponse({'message':'请求过大'},status_code=413)
        if path.startswith('/api/') and path not in {'/api/login','/api/health'}:
            token = request.cookies.get('desk_session','')
            if not hmac.compare_digest(token,session_cookie):
                return JSONResponse({'message':'请先输入主机访问码'},status_code=401)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        # Pydantic errors normally include submitted input, which can contain API secrets.
        fields = ', '.join('.'.join(str(x) for x in e['loc']) for e in exc.errors())
        return JSONResponse({'message':'请检查输入字段：'+fields},status_code=422)

    @app.exception_handler(OkxError)
    async def okx_error(request,exc):
        return JSONResponse({'message':exc.message,'code':exc.code},status_code=502)

    @app.exception_handler(LlmError)
    async def llm_error(request,exc):
        return JSONResponse({'message':exc.message},status_code=502)

    @app.exception_handler(ValueError)
    async def value_error(request,exc):
        return JSONResponse({'message':str(exc)},status_code=400)

    @app.get('/')
    async def index(): return FileResponse(ROOT/'static/index.html')
    app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')

    @app.get('/api/health')
    async def health(): return {'ok':True}

    @app.post('/api/login')
    async def login(payload:LoginIn):
        now=time.time()
        login_attempts[:]=[t for t in login_attempts if now-t<60]
        if len(login_attempts)>=8: raise HTTPException(429,'尝试过多，请一分钟后再试')
        if not hmac.compare_digest(payload.code,access_code):
            login_attempts.append(now)
            raise HTTPException(401,'访问码不正确')
        response=JSONResponse({'ok':True})
        response.set_cookie('desk_session',session_cookie,httponly=True,samesite='strict',max_age=43200)
        return response

    @app.post('/api/logout')
    async def logout():
        response=JSONResponse({'ok':True});response.delete_cookie('desk_session');return response

    @app.get('/api/status')
    async def status():
        return {'live_enabled':allow_live,'credentials':{m:store.credentials(m).is_complete for m in ('demo','live')},
                'llm_keys':{p:store.has_secret(p) for p in ADVISERS},
                'sessions':{m:engine.session(m) for m in ('demo','live')},'events':store.events(),
                'research':store.get('research'),'time':time.time()}

    @app.post('/api/credentials')
    async def credentials(payload:CredentialsIn):
        async with engine.lock:
            state=engine.session(payload.mode)
            if state:
                raise ValueError('已有试验账本时不能更换账户；请先结束并归档空仓试验')
            creds=OkxCredentials(payload.api_key,payload.api_secret,payload.passphrase,payload.mode=='demo')
            # Validate mode at OKX before saving. This is a read-only request.
            await OkxClient(creds).request('GET','/account/config')
            store.save_credentials(creds,payload.mode)
            store.event('connection',f'{payload.mode} 凭证已验证并加密保存')
        return {'ok':True}

    @app.post('/api/llm-key')
    async def llm_key(payload:LlmKeyIn):
        # Read-only check of the key before saving; only credit numbers go back to the browser.
        info=await ADVISERS[payload.provider](payload.api_key,transport=llm_transport).key_info()
        store.save_secret(payload.provider,payload.api_key)
        store.event('connection',f'{PROVIDER_NAMES[payload.provider]} 密钥已验证并加密保存')
        return {'ok':True,'provider':payload.provider,**info}

    @app.post('/api/connection')
    async def connection(payload:ModeIn):
        client=engine.client(payload.mode)
        config=await client.request('GET','/account/config')
        return {'ok':True,'permission':config[0].get('perm','') if config else '', 'mode':payload.mode}

    @app.get('/api/account')
    async def account(mode:str='demo'):
        if mode not in {'demo','live'}: raise ValueError('模式无效')
        data=await engine.client(mode).balance()
        # Exclude UID and other account identifiers from the browser response.
        return {'total_equity':data.get('totalEq'), 'holdings':[
            {'currency':r.get('ccy'),'equity':r.get('eq'),'available':r.get('availBal'),'usd':r.get('eqUsd')}
            for r in data.get('details',[]) if float(r.get('eq') or 0)!=0]}

    @app.get('/api/market')
    async def market(pair:str='BTC-USDT'):
        if pair not in PAIRS: raise ValueError('交易对无效')
        client=OkxClient()
        ticker,candles=await asyncio.gather(client.ticker(pair),client.candles(pair))
        return {**ticker,'pair':pair,'candles':[{'ts':int(r[0]),'close':float(r[4])} for r in reversed(candles)]}

    @app.post('/api/research')
    async def research(payload:ResearchIn):
        if research_lock.locked(): raise HTTPException(409,'已有回测进行中')
        if payload.max_loss>=payload.budget: raise ValueError('亏损限额必须小于本金')
        async with research_lock:
            candles=await OkxClient().candles(payload.pair,pages=8)
            result=await asyncio.to_thread(backtest,candles,payload)
            result['as_of']=time.time()
            store.set('research',result)
            store.event('research',f'{payload.pair} 历史检验完成 / {len(candles)} 根小时 K 线')
            return result

    @app.post('/api/start')
    async def start(payload:Plan): return await engine.start(payload)
    @app.post('/api/pause')
    async def pause(payload:ModeIn): await engine.pause(payload.mode);return {'ok':True}
    @app.post('/api/check')
    async def check(payload:ModeIn):
        # No force parameter: identical risk path as the scheduled tick.
        await engine.tick(payload.mode);return {'ok':True}
    @app.post('/api/flatten')
    async def flatten(payload:ActionIn): return await engine.flatten(payload.mode,payload.confirmation)
    @app.post('/api/finish')
    async def finish(payload:ModeIn): await engine.finish(payload.mode);return {'ok':True}
    return app

app=create_app()
