# 接入 AI agent（pi、OpenClaw、Claude Code 等）

Sentinel 自带一个**只读**的 MCP 服务。任何支持 MCP 的 agent 都能用它查看行情、跑回测、查询你的试验和成交记录。它**不能**下单、撤单、启动或暂停任何东西；这些操作仍然只在 Sentinel 网页上由你本人完成。

## 能做什么

| 工具 | 作用 |
|---|---|
| `market_snapshot` | OKX 现货实时价格、24 小时涨跌、最近的小时收盘价（公开数据） |
| `scanner_results` | 选币雷达最近一次的结果和排除原因 |
| `run_backtest` | 和网页相同的历史检验（网格、RSI、区间），约 100 天小时数据 |
| `trial_status` | 模拟盘或实盘试验的状态、账本盈亏、错误，以及网格每一格的挂单 |
| `recent_orders` | 本试验已对账的成交 |
| `recent_events` | 运行日志 |

## 安全边界

- 服务通过 stdio 运行在你的电脑上，不监听任何网络端口。
- 只以只读方式打开 Sentinel 的 SQLite 记录，不读取加密的密钥文件和访问码，也不返回交易所账户余额。
- 没有任何会改变状态的工具。agent 被聊天消息或网页内容"注入"了指令，也无法通过它交易。
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
- OpenClaw 的命令按其官方文档编写，尚未实际安装运行。

## 下一阶段

第 2 阶段：agent 可以"提交试验方案"，由程序校验并回测后，放进 Sentinel 网页的待审批区，由你批准。实盘永远需要你本人确认。
