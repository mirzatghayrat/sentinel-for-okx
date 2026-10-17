# 接入 AI agent（pi、OpenClaw、Claude Code 等）

Sentinel 自带一个 MCP 服务。任何支持 MCP 的 agent 都能用它查看行情、跑回测、查询你的试验和成交记录，还可以**提交试验方案**和**请求暂停**。它**不能**下单、撤单，也不能自己启动试验：方案要你在 Sentinel 网页上批准后才运行。

## 能做什么

| 工具 | 作用 |
|---|---|
| `market_snapshot` | OKX 现货实时价格、24 小时涨跌、最近的小时收盘价（公开数据） |
| `scanner_results` | 选币雷达最近一次的结果和排除原因 |
| `run_backtest` | 和网页相同的历史检验（网格、RSI、区间），约 100 天小时数据 |
| `trial_status` | 模拟盘或实盘试验的状态、账本盈亏、错误，以及网格每一格的挂单 |
| `recent_orders` | 本试验已对账的成交 |
| `recent_events` | 运行日志 |
| `list_proposals` | 已提交的方案和暂停请求，以及处理结果 |
| `propose_trial` | 提交试验方案（附自动回测），进入网页的「Agent 提案」等你审批 |
| `request_pause` | 请求暂停当前试验；桌面程序约一分钟内执行（会撤下网格挂单，持仓保留） |

## 审批规则

- 方案进入网页的「Agent 提案」面板，显示参数、回测后段与同期买入持有的对比，以及 agent 的理由（只作参考）。你点「批准并启动」后，走和你自己启动完全相同的流程，所有风控照常生效。
- 实盘方案必须由你本人批准：主机要以 `--allow-live` 启动，并输入确认文字。任何设置都不能让实盘方案自动启动。
- 你可以勾选「允许模拟盘方案自动启动」（默认关闭），让 agent 的模拟盘方案自动运行；引擎拒绝的方案会标记为「未能启动」，不会反复重试。
- 暂停请求会被直接执行，因为暂停只会降低风险；agent 不能恢复运行、卖出或启动。
- 限制：同时最多 5 个待处理提案，24 小时内最多 20 个；未处理的提案 24 小时后过期；agent 的文字最长 500 字，并去掉控制字符。

## 安全边界

- 服务通过 stdio 运行在你的电脑上，不监听任何网络端口。
- 只以只读方式打开 Sentinel 的 SQLite 记录，不读取加密的密钥文件和访问码，也不返回交易所账户余额。
- agent 只能写入单独的提案库（`data/proposals.sqlite3`），碰不到 Sentinel 自己的账本。被聊天消息或网页内容"注入"了指令的 agent，最多只能提交一个等你审批的方案，或者让试验暂停。
- agent 本身（pi、OpenClaw）拥有你电脑账户的全部权限。请只安装可信的扩展，并按它们的文档建议，在独立账户或容器里运行。

## 接入方法

以下命令中的 `/path/to/sentinel-for-okx` 换成你本机的项目目录。MCP 服务默认读取该目录下的 `data/`，和桌面程序一致。如果你用 `OKX_DESK_DATA` 指定了别的数据目录，给 MCP 服务设置同样的环境变量。

### pi

```sh
pi mcp add sentinel --cwd /path/to/sentinel-for-okx -- uv run python -m lab.mcp_server
pi mcp list        # 应显示 sentinel: connected, 6 tools
```

技能：把 `skills/sentinel-okx` 复制或链接到 `~/.agents/skills/`，pi 和 OpenClaw 都会加载。

### OpenClaw

```sh
openclaw mcp add sentinel --command uv --arg run --arg python --arg -m --arg lab.mcp_server \
  --cwd /path/to/sentinel-for-okx
openclaw mcp doctor sentinel --probe
```

技能：复制到 `~/.openclaw/workspace/skills/sentinel-okx/`。之后可以在 Telegram、WhatsApp 等渠道问"我的 BTC 网格今天怎么样"。

### Claude Code

```sh
claude mcp add sentinel -- uv --directory /path/to/sentinel-for-okx run python -m lab.mcp_server
```

## 验证情况

- 自动测试（`tests/test_mcp.py`）覆盖：
  - 只有 6 个只读工具，并标注为只读；
  - 返回结果中不出现任何密钥；
  - 不写入数据目录，数据目录不存在时也不创建；
  - 公开行情不带模拟盘标记、不带密钥；
  - 通过 stdio 能正常启动并应答。
- 在开发环境用真实 OKX 公开数据通过 stdio 调用了行情、回测和试验状态。
- pi 1.3.0 实测：`pi mcp list` 显示已连接、6 个工具。没有接模型 API 跑完整对话。
- OpenClaw 2026.6.35 实测：
  - `openclaw mcp probe` 显示 `sentinel: 9 tools`；
  - 在本机网关上用 `openclaw cron add` 建了告警和日报两个任务；
  - 手动运行日报任务，命令执行成功、拿到日报文字；
  - 测试环境没有配置聊天渠道，所以最后一步发送未执行。

## 用 OpenClaw 推送告警和日报到手机

Sentinel 提供一个报告脚本 `lab/report.py`，由 OpenClaw 的定时任务调用，再由 OpenClaw 发到 Telegram、WhatsApp、飞书、QQ 等渠道。

- **不调用大模型，不产生模型费用**，输出内容固定可预期。
- **Sentinel 不保存任何聊天平台的密钥，也不对外连接**：推送全部由 OpenClaw 完成。
- 报告脚本只读 Sentinel 的记录；唯一写入的文件是它自己的进度文件 `data/report-cursor.json`，用来保证同一条告警只发一次。

```sh
uv run python -m lab.report daily    # 日报：模拟盘和实盘的状态、净值、盈亏、距亏损线、24 小时成交、网格挂单、待审批提案
uv run python -m lab.report alerts   # 新告警：亏损线、异常、暂停、启动、归档、agent 动态、新提案；没有新内容时输出 NO_REPLY
uv run python -m lab.report alerts --fills   # 同时提醒每一笔成交（网格成交频繁时消息会比较多）
```

`NO_REPLY` 是 OpenClaw 的约定：输出它时，什么都不会发送。所以告警任务可以每 5 分钟跑一次，只有真的有事才会打扰你。第一次运行只记录当前进度，不会把历史事件重新发一遍。

### 在 Mac mini 上配置（OpenClaw 需要 Node 24.16 或更新）

1. 安装 OpenClaw，完成 `openclaw onboard`。
2. 连接一个聊天渠道。以 Telegram 为例：先用 @BotFather 创建机器人，然后在终端运行：
   ```sh
   openclaw channels add --channel telegram --token <机器人 token>
   ```
   **token 只在你自己电脑的终端里输入，不要发到任何聊天里。** 给机器人发一条消息，按 OpenClaw 文档查到你的 chat id。
3. 接入 MCP 和技能（见上文 OpenClaw 一节）。
4. 添加两个定时任务。把 `<项目目录>` 和 `<chat id>` 换成你自己的：
   ```sh
   openclaw cron add --name "Sentinel 告警" --every 5m \
     --command-argv '["uv","run","python","-m","lab.report","alerts"]' \
     --command-cwd <项目目录> \
     --announce --channel telegram --to "<chat id>"

   openclaw cron add --name "Sentinel 日报" --cron "0 9 * * *" --tz Asia/Shanghai \
     --command-argv '["uv","run","python","-m","lab.report","daily"]' \
     --command-cwd <项目目录> \
     --announce --channel telegram --to "<chat id>"
   ```
   如果 `uv` 不在 OpenClaw 服务的 PATH 里，把 `"uv"` 换成 `which uv` 显示的完整路径。新版 OpenClaw 里 `openclaw automations` 是 `openclaw cron` 的别名。
5. 可选：再加一个由模型点评的周报。例如 `--message "用 sentinel 工具总结本周网格表现，如实说明亏损和风险"`，并加上 `--tools "sentinel__*"`。这种任务会产生模型费用。
6. 之后可以直接在聊天里问"我的网格今天怎么样"、"ETH 现在适合开网格吗"。agent 会查数据、跑回测，必要时提交方案，等你到 Sentinel 网页批准。

### 隐私

日报和告警里有试验的净值、盈亏和成交，会经过你选择的聊天服务（例如 Telegram 的服务器）。里面不包含密钥、账户编号和交易所余额。如果你介意，只开告警、不开日报，或者选一个你信任的渠道。
