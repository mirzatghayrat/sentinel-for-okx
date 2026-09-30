import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

Mode = Literal['demo', 'live']
PAIR_PATTERN = r'^[A-Z0-9]{1,20}-USDT$'
Provider = Literal['openrouter', 'typesafe']
MODEL_ID = re.compile(r'[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._:-]*')
JEV_ID = re.compile(r'[a-z0-9][a-z0-9._-]*')

class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False, str_strip_whitespace=True)

class Plan(Model):
    mode: Mode = 'demo'
    pair_mode: Literal['fixed', 'rotate'] = 'fixed'
    pair: str = Field(default='BTC-USDT', pattern=PAIR_PATTERN)
    top_n: int = Field(default=5, ge=2, le=10)
    budget: float = Field(ge=20, le=100000)
    max_loss: float = Field(gt=0)
    hours: int = Field(default=24, ge=1, le=24)
    max_position_pct: float = Field(default=25, ge=1, le=100)
    order_quote: float = Field(default=20, ge=5)
    max_orders_day: int = Field(default=4, ge=1, le=50)
    strategy: Literal['rsi', 'range', 'llm'] = 'rsi'
    llm_provider: Provider = 'openrouter'
    llm_model: str = Field(default='', max_length=100)
    confirmation: str = ''

    @model_validator(mode='after')
    def bounds(self):
        if self.max_loss >= self.budget:
            raise ValueError('亏损限额必须小于试验资金')
        if self.order_quote > self.budget * self.max_position_pct / 100:
            raise ValueError('单笔金额不能超过仓位上限')
        if self.strategy == 'llm' and self.llm_provider == 'openrouter' and not MODEL_ID.fullmatch(self.llm_model):
            raise ValueError('请填写 OpenRouter 模型 ID，格式为 provider/model')
        if self.strategy == 'llm' and self.llm_provider == 'typesafe' and not JEV_ID.fullmatch(self.llm_model):
            raise ValueError('请填写 TypeSafe 模型名，例如 jev-latest')
        return self

class CredentialsIn(Model):
    mode: Mode
    api_key: str = Field(min_length=8, max_length=256)
    api_secret: str = Field(min_length=8, max_length=256)
    passphrase: str = Field(min_length=1, max_length=256)

class LlmKeyIn(Model):
    provider: Provider = 'openrouter'
    api_key: str = Field(min_length=8, max_length=256)

class ScanIn(Model):
    min_volume: float = Field(default=1_000_000, ge=0, le=1e11)
    max_spread_pct: float = Field(default=.2, gt=0, le=5)
    min_age_days: int = Field(default=90, ge=60, le=90)
    max_vol_pct: float = Field(default=150, gt=0, le=1000)

class ResearchIn(Model):
    pair: str = Field(default='BTC-USDT', pattern=PAIR_PATTERN)
    budget: float = Field(default=500, ge=20, le=100000)
    max_loss: float = Field(default=25, gt=0, le=100000)
    fee_bps: float = Field(default=10, ge=0, le=100)
    slippage_bps: float = Field(default=5, ge=0, le=100)
    strategy: Literal['rsi', 'range'] = 'rsi'

    max_position_pct: float = Field(default=25, ge=1, le=100)
    order_quote: float = Field(default=20, ge=5)
    max_orders_day: int = Field(default=4, ge=1, le=50)

    @model_validator(mode='after')
    def limits(self):
        if self.max_loss >= self.budget or self.order_quote > self.budget*self.max_position_pct/100:
            raise ValueError('回测的亏损或单笔金额超出预算约束')
        return self

class LoginIn(Model):
    code: str = Field(max_length=256)

class ModeIn(Model):
    mode: Mode = 'demo'

class ActionIn(ModeIn):
    confirmation: str = ''
