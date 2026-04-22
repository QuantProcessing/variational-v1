# variational-v1

邀请链接：
- Variational: [https://omni.variational.io/?ref=OMNIQUANT](https://omni.variational.io/?ref=OMNIQUANT)（直升 Bronze，获得 12% 积分加成）
- Lighter: [https://app.lighter.xyz/?referral=QUANTGUY](https://app.lighter.xyz/?referral=QUANTGUY)

English version is below.

---

## 中文

### 项目目标
`variational-v1` 是一个跨交易所的 **自动价差交易（volume farming）** 运行时。核心思想：

- Variational 的 RFQ 报价相对 Lighter 订单簿存在系统性偏移（实测约 99% 的时间里 Variational 报价高出 Lighter 约 4 bp）。
- 两个交易所都是 **0 手续费**（Variational RFQ 无 maker/taker，Lighter Basic 账户 0 手续费），理论上双边开仓 + 双边平仓的滑点成本低于捕获的价差时，即可稳态刷交易量并积累积分/返佣。
- 因此程序的主要职责是：在价差达到阈值（5m/30m/1h 滚动中位数）时，同时在 Variational 开仓一边 + 在 Lighter 对冲另一边；当反方向价差也越过阈值且浮盈 ≥ 0 时自动平仓。

### 系统结构（三层管道）

```
Chrome Extension (CDP forwarder)
         │  ws://127.0.0.1:8766 (Variational /events + /portfolio WS)
         │  ws://127.0.0.1:8767 (Variational REST 响应)
         │  ws://127.0.0.1:8768 (CommandBroker，Python → 浏览器 fetch 代理)
         ▼
variational/listener.py   — 本地 WS 接收器 + VariationalMonitor 状态机
         ▼
main.py  VariationalToLighterRuntime
  ├── Lighter order_book / account_orders / user_stats WebSocket
  ├── SignalEngine（跨交易所价差绿灯信号）
  ├── AutoTrader（信号触发 → 双腿并行下单 → 成交撮合 → 平仓）
  ├── EventJournal（异步队列 JSONL 写入器）
  └── Rich Dashboard（实时面板）
```

#### 1. Chrome 扩展（`chrome_extension/`）
- Manifest V3 + Service Worker，用 CDP 附着 Variational 的交易页面。
- 拦截 `/api/quotes/indicative` REST 响应、`/events` 和 `/portfolio` WS 帧，按 JSON 信封转发到本地。
- `content_script.js` 充当 **fetch 代理**：收到 `EXECUTE_FETCH` 命令后，用浏览器会话（带 cookie）向 variational.io 发真实 fetch 请求，这是 Python 端驱动 Variational 下单 / 查询持仓的唯一通道（Variational 无公开 API key）。

#### 2. 本地接收层（`variational/`）
- `run_receiver_server`：起两个 `websockets.serve` 端点接收 CDP 帧。
- `VariationalMonitor`：Variational 状态的唯一真源——解析报价、`event_seq` 递增的 trade 事件（取 `source_rfq` 字段作为关联键）、组合与 pool。
- `CommandBroker`（:8768）：Python → 浏览器的 `EXECUTE_FETCH` / `FETCH_RESULT` 路由器。
- `signal.py` `SignalEngine`：每个 dashboard tick 记一笔跨交易所价差（减去双边盘口价差均值作为 baseline），维护 1h 滚动历史，对 5m/30m/1h 三个窗口取中位数；当 adjusted > any 一个中位数时该方向翻绿（`--signal-strict` 改为 max）。
- `auto_trader.py` `AutoTrader`：订阅信号 green-edge → 并行发送两条腿；仓位会计（带符号 qty + 均价）；平仓模式与滞回；熔断。
- `journal.py` `EventJournal`：async queue + 后台 drainer，队列满时丢弃并计数，永不阻塞主路径。
- `command_client.py` `VariationalCmdClient`：Python 端的 CommandBroker 客户端。

#### 3. 运行时（`main.py`）
- `VariationalToLighterRuntime` 一个类管理全部实时状态。
- **自动识别资产**：读取 `monitor.current_quote_asset`，应用 `VARIATIONAL_TICKER_OVERRIDES` 后，REST 查 Lighter `orderBooks` 获得 `market_id` 和 decimal 倍率。连续 N tick 看到新资产后，调用 `activate_asset` 重建 Lighter 订阅并清空历史。
- **Lighter WebSocket**：订阅 `order_book/{market_id}`、`account_orders/{market_id}/{account_id}`、`user_stats/{account_id}`；本地维护订单簿，`offset` 出现回退即重订阅取快照。认证订阅使用 SignerClient 的 auth token。
- **dashboard_loop**：`rich.Live(screen=True)` 另屏渲染，所以**禁止 `print()`**，一律走 `self.logger`。

### 交易循环

1. **开仓**：`SignalEngine.detect_edges()` 给出 `(direction, signal_turned_green)` → `AutoTrader._open_cycle`：
   - 量用 `quantize_qty` 量化到 2 位小数（两家统一，避免因精度差产生残余）。
   - 两条腿并行：`VariationalPlacerImpl._rfq_then_submit`（两步 RFQ：`/api/quotes/indicative` → `/api/orders/new/market`，传 `max_slippage`）与 `LighterAdapterImpl.place_order`（基于盘口 + `HEDGE_SLIPPAGE_BPS` 的 GTT limit）。
   - `rfq_id` 是 Variational 成交关联键，WS trade 事件里作为 `source_rfq` 返回。
   - 开仓后 `_verify_positions_after_cycle` 调 REST 查询双边仓位，如漂移超过 quantum 则把跟踪器同步到真实值。

2. **平仓（close mode）**：当 `max(|var_pos|, |lighter_pos|) ≥ position_limit` 时进入 reduce-only 平仓模式；持仓回落到 50% 后恢复。平仓门槛双重：
   - 反方向信号翻绿（`signal.is_green`）；
   - 估算 `pnl_per_unit ≥ 0`；
   - 按 Lighter 顶档 30%（`CLOSE_BOOK_FRACTION`）吃单，避免抢前。

3. **单边残余处理**：若一边仓位归零而另一边非零，`_handle_close_mode_residual` 查询两家 REST 持仓，同步跟踪器并触发 **单边 reduce-only 平仓**（`_close_var_residual` / `_close_lighter_residual`）。

4. **熔断**：3 次连续失败或单日 5 次失败 → `TraderStats.frozen=True`，dashboard 面板转红；仅进程重启清除。

### 仓位与 P&L
- 经典永续成本基：带符号 `qty` + `avg_entry_price`；开仓更新均价，减仓实现 PnL。
- 单边持仓显示的浮盈 uPnL 使用本地 mid 实时计算（`qty_signed × (current_mid - avg_entry)`），不依赖 WS 里可能延迟的 upnl。
- Cum.Volume：按成交额累计（Variational + Lighter 分别展示），用于刷量进度观察。

### 环境准备

#### macOS / Linux
```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

#### Windows（PowerShell）
```powershell
py -3 -m venv env
.\env\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env`：
```bash
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=...
LIGHTER_PRIVATE_KEY=...
# 可选：强制 Lighter 旧版 app-level ping/pong
# LIGHTER_WS_SERVER_PINGS=true
```

Variational 无需 API key：所有请求都通过 Chrome 扩展的浏览器会话 fetch，所以运行前必须先登录 variational.io。

### 加载 Chrome 插件
1. 打开 `chrome://extensions`
2. 右上角开启 `Developer mode`
3. 左上角 `Load unpacked`，选择 `variational-v1/chrome_extension`

### 运行

先启动 Python 运行时（扩展需要本地接收端就绪）：

```bash
python main.py --qty 0.01                   # 最小启动，qty 必填
python main.py --qty 0.01 --lang en         # 英文看板
python main.py --qty 0.01 --signal-strict   # 严格信号（max 而非 any）
python main.py --qty 0.01 --position-limit 5   # 指定仓位上限
```

随后打开 Variational 交易页面 → 扩展列表 → 点击 `Variational CDP Forwarder` → `Start`。

#### 常用参数
| 参数 | 默认 | 说明 |
| ---- | ---- | ---- |
| `--qty` | 必填 | 每轮开仓的基础资产数量 |
| `--throttle-seconds` | 3.0 | 相邻 cycle 之间的最小间隔 |
| `--max-trades-per-day` | 200 | 日内 cycle 上限 |
| `--position-limit` | 0 | 净持仓上限（0 = 自动 2×qty） |
| `--signal-strict` | off | 要求 adjusted > **max**(medians) |
| `--var-order-timeout-ms` | 5000 | Variational 下单超时 |
| `--leg-settle-timeout-sec` | 10 | 两腿等待成交超时 |
| `--var-max-slippage-bps` | 100 | Variational market order 的 `max_slippage`（1% = 100 bps） |
| `--debug-var-payload` | off | 把完整请求/响应写进 `order_events.jsonl` |
| `--lang` | zh | 看板语言（`zh`/`en`） |

停止：Ctrl+C。

### 输出日志（`./log/`）
- `runtime.log` — 文本运行日志。
- `signal_events.jsonl` — 每次 dashboard tick 的 SignalState 快照。
- `order_events.jsonl` — 事件源：`var_place_attempt` / `var_fill` / `lighter_fill` / `cycle_opened` / `cycle_closed` / `close_attempt` / `position_verify` 等。
- `cycle_pnl.jsonl` — 每个 cycle 的归因账单（规范重放表）。
- `trade_records.csv` — dashboard 每次刷新时按 AutoTrader 的 recent_cycles 原子覆盖（`.tmp` + `os.replace`）。

### 验证工具
```bash
python scripts/validate_signal.py --log-dir ./log
```
离线分析 `signal_events.jsonl` + `order_events.jsonl` + `cycle_pnl.jsonl`，给出方向偏差、滑点分布、close mode 命中率、熔断原因等。

### 已知风险
- **Variational /events WS 可能不稳定推送 reduce-only 平仓 trade**：目前靠 REST 轮询仓位兜底。
- **Variational HTTP 偶发 500/422**：连续 3 次触发熔断，需要重启进程。
- **close mode 会吃掉一部分 alpha**：实测 ~80% 的平仓命中在 est_pnl < 1 bp 区间，若反向价差长期不翻绿，持仓可能积压。
- **单腿失败**：目前只记录，不重试；依赖下一轮 cycle 或 residual close 清理，严重时看板会显示 partial。
- **熔断是永久冻结**：直到重启，故意保守以防异常放大。

---

## English

Referral Links:
- Variational: [https://omni.variational.io/?ref=OMNIQUANT](https://omni.variational.io/?ref=OMNIQUANT) (instant Bronze tier + 12% points bonus)
- Lighter: [https://app.lighter.xyz/?referral=QUANTGUY](https://app.lighter.xyz/?referral=QUANTGUY)

### Project Goal
`variational-v1` is a cross-venue **auto spread trader (volume farmer)**. The thesis:

- Variational's RFQ quotes sit systematically above Lighter's book (~4 bp in ~99% of samples).
- Both venues are **zero-fee** (Variational RFQ has no maker/taker, Lighter Basic account is 0-fee), so as long as the round-trip slippage stays below the captured spread, you can farm volume / points profitably at steady state.
- The runtime's job is therefore: when the cross-venue adjusted spread exceeds its rolling 5m/30m/1h medians, fire one leg into Variational and the opposite leg into Lighter; when the opposite direction turns green with positive unrealized P&L, close both.

### Architecture (three-tier pipeline)

```
Chrome Extension (CDP forwarder)
         │  ws://127.0.0.1:8766 (Variational /events + /portfolio WS)
         │  ws://127.0.0.1:8767 (Variational REST responses)
         │  ws://127.0.0.1:8768 (CommandBroker — Python → browser fetch proxy)
         ▼
variational/listener.py   — local WS receivers + VariationalMonitor
         ▼
main.py  VariationalToLighterRuntime
  ├── Lighter order_book / account_orders / user_stats WebSocket
  ├── SignalEngine (cross-venue green-light detector)
  ├── AutoTrader (edge → parallel two-leg dispatch → fills → close)
  ├── EventJournal (async-queue JSONL writer)
  └── Rich Dashboard
```

#### 1. Chrome Extension (`chrome_extension/`)
- Manifest V3 service worker, attaches the Chrome debugger to the Variational tab.
- Intercepts `/api/quotes/indicative` REST responses plus `/events` and `/portfolio` WS frames; forwards JSON envelopes to local WS servers.
- `content_script.js` is a **fetch proxy**: on `EXECUTE_FETCH` it issues real `fetch()` calls against variational.io using the browser session (cookies + auth flow automatically). This is the only channel by which Python can place orders / query positions, since Variational has no public API key.

#### 2. Local Receivers (`variational/`)
- `run_receiver_server` — two `websockets.serve` endpoints.
- `VariationalMonitor` — single source of truth: quotes, `event_seq`-indexed trades (keyed by `source_rfq`), positions, pool.
- `CommandBroker` (:8768) — Python → browser `EXECUTE_FETCH` / `FETCH_RESULT` router.
- `signal.py` `SignalEngine` — per-tick adjusted cross spread (minus mean of both venues' book-spread baselines), 1h rolling history, 5m/30m/1h medians; green when adjusted > any median (`--signal-strict` switches to max).
- `auto_trader.py` `AutoTrader` — subscribes to green-edges, fires both legs in parallel; perp cost-basis accounting; close mode + hysteresis; breaker.
- `journal.py` `EventJournal` — async queue + background drainer, drops-and-counts on overflow instead of blocking.
- `command_client.py` `VariationalCmdClient` — Python-side CommandBroker client.

#### 3. Runtime (`main.py`)
- `VariationalToLighterRuntime` owns the live system in one class.
- **Auto ticker**: reads `monitor.current_quote_asset`, applies `VARIATIONAL_TICKER_OVERRIDES`, resolves Lighter `market_id` via `/api/v1/orderBooks`. After N consecutive ticks seeing a new asset, `activate_asset` tears down the Lighter WS and re-subscribes.
- **Lighter WS**: `order_book/{market_id}`, `account_orders/{market_id}/{account_id}`, `user_stats/{account_id}`. Offset regression triggers a resubscribe for a fresh snapshot. Authenticated channels use a SignerClient auth token.
- **dashboard_loop**: `rich.Live(screen=True)` on the alternate screen buffer, so `print()` is forbidden — everything goes through `self.logger`.

### Trading Loop

1. **Open**: `SignalEngine.detect_edges()` emits `(direction, signal_turned_green)` → `AutoTrader._open_cycle`:
   - Qty is `quantize_qty`'d to 2 decimals on both legs (symmetric precision avoids residual drift).
   - Both legs fire in parallel: `VariationalPlacerImpl._rfq_then_submit` (two-step: `/api/quotes/indicative` → `/api/orders/new/market` with `max_slippage`) and `LighterAdapterImpl.place_order` (GTT limit at best bid/ask ± `HEDGE_SLIPPAGE_BPS`).
   - `rfq_id` is the Variational correlation key; the WS trade event returns it as `source_rfq`.
   - After the cycle settles, `_verify_positions_after_cycle` queries both venues' REST positions and syncs the tracker if drift exceeds the quantum.

2. **Close mode**: entered when `max(|var_pos|, |lighter_pos|) ≥ position_limit`; resumed at 50%. Close fires only when all three hold:
   - Reverse-direction signal is green,
   - Estimated `pnl_per_unit ≥ 0`,
   - Sized at 30% of Lighter top-of-book (`CLOSE_BOOK_FRACTION`) to avoid front-running.

3. **Single-venue residual**: if one side hits zero while the other is non-zero, `_handle_close_mode_residual` polls REST positions, re-syncs the tracker, and fires a **reduce-only close on the remaining venue** via `_close_var_residual` / `_close_lighter_residual`.

4. **Breaker**: 3 consecutive failures or 5 daily failures → `TraderStats.frozen = True`, dashboard panel turns red; clears only on process restart.

### Position & P&L
- Classic perp cost basis: signed `qty` + `avg_entry_price`; opens update the average, decreases realize P&L.
- Unrealized P&L is recomputed locally from the current mid (`qty_signed × (current_mid - avg_entry)`) rather than trusting potentially stale WS `upnl`.
- `Cum.Volume` is accumulated notional per venue — useful for tracking farming progress.

### Setup

#### macOS / Linux
```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

#### Windows (PowerShell)
```powershell
py -3 -m venv env
.\env\Scripts\Activate.ps1
pip install -r requirements.txt
```

`.env`:
```bash
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=...
LIGHTER_PRIVATE_KEY=...
# Optional: force legacy Lighter app-level ping/pong
# LIGHTER_WS_SERVER_PINGS=true
```

No Variational API key is needed — all Variational traffic rides the Chrome extension's authenticated browser session, so log into variational.io before running.

### Load Chrome Extension
1. Open `chrome://extensions`
2. Enable `Developer mode` (top-right)
3. Click `Load unpacked`, pick `variational-v1/chrome_extension`

### Run

Start the Python runtime first (the extension will queue against a cold socket, but it's cleaner this way):

```bash
python main.py --qty 0.01                   # minimal; --qty is required
python main.py --qty 0.01 --lang en         # English dashboard
python main.py --qty 0.01 --signal-strict   # strict green gate (max instead of any)
python main.py --qty 0.01 --position-limit 5
```

Then open the Variational trading page → extension list → click `Variational CDP Forwarder` → `Start`.

#### Key flags
| Flag | Default | Description |
| ---- | ------- | ----------- |
| `--qty` | required | Base qty per cycle |
| `--throttle-seconds` | 3.0 | Minimum gap between cycles |
| `--max-trades-per-day` | 200 | Daily cycle cap |
| `--position-limit` | 0 | Net position ceiling (0 → auto 2×qty) |
| `--signal-strict` | off | Require adjusted > **max**(medians) |
| `--var-order-timeout-ms` | 5000 | Variational order timeout |
| `--leg-settle-timeout-sec` | 10 | Two-leg settle timeout |
| `--var-max-slippage-bps` | 100 | Variational `max_slippage` (1% = 100 bps) |
| `--debug-var-payload` | off | Log full req/resp bodies to `order_events.jsonl` |
| `--lang` | zh | Dashboard language (`zh` / `en`) |

Stop with Ctrl+C.

### Output Logs (`./log/`)
- `runtime.log` — text log.
- `signal_events.jsonl` — per-tick `SignalState` snapshots.
- `order_events.jsonl` — event source: `var_place_attempt` / `var_fill` / `lighter_fill` / `cycle_opened` / `cycle_closed` / `close_attempt` / `position_verify` etc.
- `cycle_pnl.jsonl` — per-cycle attribution (canonical replay table).
- `trade_records.csv` — atomic snapshot refreshed on every dashboard tick, built from AutoTrader's recent-cycles deque.

### Validation
```bash
python scripts/validate_signal.py --log-dir ./log
```
Offline analysis over `signal_events.jsonl` + `order_events.jsonl` + `cycle_pnl.jsonl`: direction skew, slippage distribution, close-mode hit rate, breaker causes, etc.

### Known Risks
- **Variational `/events` WS may not reliably push reduce-only close trades** — we fall back on REST position polling.
- **Variational HTTP 500/422 bursts** trip the breaker after 3 consecutive failures; recovery needs a process restart.
- **Close mode can erode alpha**: ~80% of closes fire with est_pnl < 1 bp in production logs; if the reverse side stays red, positions can pile up.
- **Single-leg failure** is logged but not retried; reliance on the next cycle or residual-close to reconcile. Dashboard shows `partial` until resolved.
- **Breaker is permanent-until-restart** by design — conservative guard against runaway behavior.
