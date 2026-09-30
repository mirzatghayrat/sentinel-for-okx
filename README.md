# 哨兵 Sentinel for OKX

[![Verify](https://github.com/mirzatghayrat/sentinel-for-okx/actions/workflows/test.yml/badge.svg)](https://github.com/mirzatghayrat/sentinel-for-okx/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-b6f36b.svg)](LICENSE)

**OKX 现货 AI 交易台：AI 盯盘，规则站岗。**

在你自己电脑上运行的 OKX 现货交易台。每根小时 K 线收盘后，由规则策略或你选的大模型（经 OpenRouter，或 TypeSafe 的 Jev）给出买入／卖出／持有；下单金额、仓位、亏损线、次数和期限由代码强制执行，模型无权改变。密钥加密保存在本机，不经过任何第三方服务器。

*A local OKX spot trading desk where an LLM (or a rule) proposes buy / sell / hold once per hourly bar, and hard-coded guardrails decide what actually gets sent. Keys stay on your machine.*

[![哨兵 Sentinel for OKX 产品介绍：点击观看 30 秒视频](docs/media/sentinel-poster.png)](docs/media/sentinel-intro.mp4)

▶ [观看 30 秒产品介绍视频](docs/media/sentinel-intro.mp4)（视频中的界面为演示数据）

> [!WARNING]
> **这是待验证的交易工具，不是已证明盈利的策略。** 当前版本尚未经过真实账户端到端验证，所有策略（包括 LLM 策略）都没有盈利证据。程序默认只允许模拟盘；请先用 OKX 模拟交易跑足够长时间，再自行决定是否使用真实资金。本项目不构成投资建议，使用者自行承担全部风险。
>
> **非 OKX 官方产品，与 OKX 无隶属或合作关系。**

## 功能

- **行情**：BTC-USDT / ETH-USDT 实时价格与小时图，无需密钥。
- **账户**：模拟盘与实盘分别配置 API，加密保存在本机，可查看余额。
- **三种策略**，每 60 秒检查、每根已收盘小时 K 线最多决策一次：
  - RSI 回归（RSI 14，30 买 / 70 卖）；
  - 区间位置（48 小时区间，20% 买 / 80% 卖）；
  - LLM 决策：通过 OpenRouter 调用你选的模型，或直接调用 TypeSafe 的 Jev（只从买入／卖出／持有中选一个并给出各选项概率）；调用次数与费用（Jev 显示 token 用量）单独显示。
- **你来定边界**：试验资金、累计亏损触发线、仓位上限、单笔金额、每日次数、1–24 小时运行期限。
- **执行与对账**：市价现货单；提交前持久化订单编号，逐笔核对成交数量与手续费；结果不明时冻结，绝不换编号重发。
- **手动操作**：暂停、卖出本策略持仓、立即检查／对账、结束并归档试验。
- **历史检验**：约一个月小时数据，计入手续费与滑点，下一根开盘成交，后 30% 数据单独报告（LLM 策略不可回测）。
- **记录**：订单表、事件日志、模型问答与累计费用。

不提供提现、划转、杠杆、合约或做空；只做 BTC / ETH 的 USDT 现货市价单。

## 快速开始

| 系统 | 支持情况 |
|---|---|
| macOS | 支持，可双击 `.command` 文件启动，也可用命令行 |
| Linux | 支持，命令行启动 |
| Windows | 不能直接运行（用到 Unix 文件锁），请在 WSL2 中按 Linux 方式运行 |

需要 Python 3.11+ 和 [uv](https://docs.astral.sh/uv/getting-started/installation/)。

```sh
git clone https://github.com/mirzatghayrat/sentinel-for-okx.git
cd sentinel-for-okx
uv sync --frozen
uv run python run.py
```

1. 浏览器打开 `http://127.0.0.1:8765`。首次启动会在 `data/access-code` 生成访问码（Mac 可双击「打开控制台.command」查看）。访问码只用于登录本机控制台，不是 OKX 密码。
2. 在 OKX「模拟交易」中创建专用 API（只给读取和交易权限，**不要给提现权限**），填入网页的连接面板。
3. 设定资金、亏损线等边界，选择策略，启动模拟试验。
4. 想用 LLM 策略：在 [OpenRouter](https://openrouter.ai) 为本程序单独创建一个 Key 并设置额度上限，在「策略」里选「LLM 决策」，点「设置 OpenRouter 密钥」填入，再从 [openrouter.ai/models](https://openrouter.ai/models) 复制模型 ID。
5. 想用 Jev：在 TypeSafe 官方控制台创建 Key，「模型来源」选「TypeSafe · Jev」，模型填 `jev-latest`，点「设置 TypeSafe 密钥」填入。

Mac 上的完整步骤、后台服务与电源设置见 [docs/IMAC.md](docs/IMAC.md)。

**实盘需要你自己启用：** 停止旧服务后运行 `uv run python run.py --allow-live`（或双击「启动实盘控制台.command」）。这只打开程序的实盘能力，不会自动下单；还需要在网页选择实盘、配置独立子账户凭证、输入确认文字并手动启动。

## 安全设计

- 服务只监听 `127.0.0.1`，控制台只能在运行它的电脑上打开；需要访问码登录，并校验请求来源。不要用端口映射或隧道把它暴露到网络上。
- 交易所客户端只允许白名单接口：行情、账户配置与余额、查询订单和现货市价下单；提现、划转、杠杆接口在代码层面被拒绝。
- 实盘下单同时需要：主机以 `--allow-live` 启动、引擎签发的下单许可、用户输入确认文字。
- 单实例锁防止同一台电脑重复运行；程序重启后策略一律暂停，需要你重新启动。
- 每个试验有独立账本，只卖出本策略买入的持仓；账户余额少于账本时暂停。
- 发给模型的只有公开 K 线和本试验的账本数字，不包含任何密钥、账户编号或账户余额；模型回答不合规、超时或出错一律按持有处理。
- OKX、OpenRouter 与 TypeSafe 密钥用本机生成的密钥加密，文件权限仅限当前用户。这不是硬件级保护，无法防御已经控制你电脑的攻击者。

**不要把 API Key、Secret、Passphrase、OpenRouter 或 TypeSafe Key 发到聊天、Issue 或 GitHub。** 发现安全问题请不要在公开 Issue 中贴出任何密钥或账户信息。

## 运行语义与限制

- 服务持续运行不等于策略无限续期。最长 24 小时后停止新订单，需使用者重新启动；累计盈亏与订单计数不因续期重置。
- 程序重启、断线、异常会暂停策略，不自动恢复交易；可能仍有持仓。断电、系统睡眠、无法联网期间，程序无法执行止损。
- 亏损线按试验初始资金减当前净值计算，含已对账费用、未扣未来退出成本。触发后尝试退出并停止，不保证最终亏损不超限。
- 正常每日次数上限按 UTC 日计；亏损退出与用户显式退出可越过次数上限以降低持仓。
- 暂停与到期不卖出；用户可以显式卖出本策略持仓。现货卖出会向下取整，可能留下小于最小订单的零碎币。
- 使用独立子账户；不要同时手动交易同币种、划转资金或运行其他机器人。共享账户活动会导致账本与余额不一致并暂停。
- 同一个 OKX 账户同一时间只能在一台电脑上运行策略；单实例锁只作用于一台电脑。
- 提交前持久化唯一订单编号。结果不明时冻结，持续查询同一编号，不用新编号重试。订单被拒或长期查不到时，需要查清交易所记录后处理，当前版本不提供跳过对账的按钮。
- 只有已暂停、无未决订单、剩余持仓低于 OKX 最小下单量的试验可以归档。按币扣的手续费常留下这种零碎币；它留在账户里，数量和估值写入归档与事件记录，不当作已卖出。
- LLM 策略每根新 K 线最多询问一次模型；询问期间用户暂停，回答不会被执行；亏损线检查不等待模型。
- LLM 策略无法用历史回测检验（模型可能见过这些历史行情），只能在模拟盘向前观察。模型费用由 OpenRouter 或 TypeSafe 另计，不计入试验盈亏。
- 当前适配 `https://www.okx.com` 全球 API；不同地区账号若要求其他专用域名，需要单独适配，不能通过此程序绕过限制。
- 回测只覆盖最近约一个月，无法证明策略有稳定优势；未模拟盘口深度、最小订单、成交延迟及断网。

## 测试

```sh
uv run python -m pytest -q
node --check static/app.js
```

测试覆盖模拟标识、实盘两层开关、接口白名单、重复下单、并发、到期、仓位上限、手续费对账、零碎币归档、订单拒绝码、按 OKX 真实分页格式取 K 线、LLM 决策（每根 K 线只问一次、失败按持有、询问中暂停不下单、密钥不回显）、亏损退出、重启暂停、预算不可悄然重置、访问控制和回测前视偏差。测试不使用任何真实凭证，外部请求只进入 MockTransport。

## 目录结构

- `lab/okx.py`：受限的 OKX V5 客户端（固定域名、接口白名单、只允许现货市价单）。
- `lab/engine.py`：单实例执行引擎与独立账本。
- `lab/app.py`：本机 Web 服务、认证和接口。
- `lab/strategy.py`、`lab/research.py`：规则策略与历史检验。
- `lab/llm.py`：OpenRouter 与 TypeSafe（Jev）决策顾问、提示词与回答解析。
- `static/`：无外部脚本依赖的中文控制台。
- `scripts/service.py`：macOS 登录后台服务安装器（可选）。
- `data/`：本机运行数据（密钥、访问码、账本），已从 Git 排除，请单独加密备份。
- `docs/`：Mac 运行说明、验证记录与宣传素材。

## 许可

[MIT](LICENSE)。软件按"原样"提供，不附带任何担保；作者不对使用本软件造成的任何交易损失负责。
