# Cross-Trade Volume Farming — Design Spec

- **Date**: 2026-04-19
- **Author**: brainstorming session, Claude + 西瓜君🍉
- **Status**: design agreed, ready for implementation plan

## 1. Goal

Extend `variational-v1` into an **actively self-triggered** cross-venue trading loop that farms Variational trading volume at near-zero net cost. When a pre-defined cross-venue spread signal flips green, the system automatically opens a matched pair of orders (Variational + Lighter), in opposite directions, such that over a statistically significant sample the realized net P&L per cycle ≥ 0.

This replaces the current **passive** flow (user places Variational order by hand, Python auto-hedges on Lighter) with a **proactive** flow that does both legs itself.

### Success criteria

1. When the signal (reused from current dashboard logic) is green and all gates pass, the system places a Variational order and a Lighter order in parallel.
2. Each cycle produces one durable `cycle_pnl.jsonl` line containing complete attribution (slippage per leg, fees, fill ratio, latency) — sufficient to answer, for any given cycle, "where did the money go?"
3. The core trading path does zero synchronous disk I/O; logging never back-pressures the signal → order path.
4. On any one-leg failure, the system trips a breaker, halts new triggers, and leaves the residual position for manual handling.

### Non-goals

- Observation / dry-run mode (user explicitly rejected two-stage rollout)
- Orderbook-depth-aware adaptive qty (future work; MVP uses fixed `--qty`)
- Automatic liquidation of residual positions after a breaker trip
- External alerting (Slack/webhook); `runtime.log` + dashboard red banner are enough
- Introducing a test framework (the repo has none; verification is manual per milestone)

## 2. Current-state snapshot

Only the facts this spec depends on — everything else is in `CLAUDE.md`.

- `CommandBroker` on `ws://127.0.0.1:8768` is fully implemented in `variational/listener.py` but **not wired into `main.py`** and has **no client on the Chrome extension side**. It is a dangling feature, resurrected here.
- The current dashboard already computes the green-light signal inline (`main.py:_fmt_signal_pct`, lines 832–851). It uses `adjusted = cross_spread_pct - book_spread_baseline_pct` and `green ⇔ adjusted > any(median_5m, median_30m, median_1h)`.
- Variational's order-placement REST API contract is **not yet known** — the user will grab the request shape from browser DevTools at implementation time. The design treats the API call as an opaque `fetch(url, {method, headers, body})` inside a content script.
- The Chrome extension is MV3, currently only a CDP network forwarder.
- Python 3.10+ (code already uses `slots=True` / PEP 604 unions).

## 3. Architecture

```
┌──────────────────────────────────────────────────────────────────────┐
│ Chrome Extension (MV3)                                               │
│                                                                      │
│   content_script.js  ←message→  background.js                        │
│   (injected into variational.io)    ├── CDP forwarder (unchanged)   │
│   fetch()s Variational order API    └── CommandSocket → :8768        │
└──────────────────────────────────────────────────────────────────────┘
                                       ▲  WS (PLACE_ORDER / ORDER_RESULT)
                                       │
┌──────────────────────────────────────▼──────────────────────────────┐
│ Python runtime (main.py)                                            │
│                                                                     │
│  VariationalMonitor ──┐                                             │
│  (quotes/trades/hb)   │                                             │
│                       ▼                                             │
│                 ┌──────────────┐  green-light   ┌──────────────┐   │
│                 │ SignalEngine │───────────────▶│  AutoTrader  │   │
│                 │ adjusted vs  │                │ ├ throttle   │   │
│                 │ 5m/30m/1h    │                │ ├ day limit  │   │
│                 │ medians      │                │ ├ breaker    │   │
│                 └──────────────┘                │ └ parallel   │   │
│                       ▲                         │   dispatch   │   │
│                       │ reads                   └──┬────────┬──┘   │
│                 ┌─────┴────────┐                   │        │      │
│                 │  Dashboard   │         parallel  ▼        ▼      │
│                 │  (rich.Live) │   VariationalCmdClient  Lighter  │
│                 │  reads state │   → CommandBroker :8768  SDK     │
│                 │  only        │                                   │
│                 └──────────────┘                                   │
│                                                                    │
│         ┌────────────────────────────────────────────┐             │
│         │  EventJournal × 2   (async queue drainers) │             │
│         │    ├─ order_events.jsonl                   │             │
│         │    └─ cycle_pnl.jsonl                      │             │
│         └────────────────────────────────────────────┘             │
└────────────────────────────────────────────────────────────────────┘
```

### Key architectural decisions

| Decision | Choice | Rationale |
|---|---|---|
| Variational order channel | Chrome extension **content script** `fetch()` from variational.io origin | Inherits browser session / cookies / CSRF automatically; no need to exfiltrate auth; MV3 background `fetch` would have CORS and cookie-scoping issues. |
| Command transport | Reuse existing `CommandBroker` on port 8768 | Already coded, just needs clients. |
| Signal | Reuse the exact formula in `_fmt_signal_pct` | Respects original author intent; dashboard and trader cannot drift apart. |
| Leg ordering | **Parallel dispatch**, both legs fire simultaneously | User decision. Minimizes fill-time drift between venues; one-leg failure is treated as a halt condition. |
| Residual handling | Trip breaker, stop new triggers, leave position for manual action | User decision. Avoids compounding a bad state. |
| Qty | CLI `--qty`, required when auto-trading | Simplicity; depth-aware qty is future work. |
| Default mode | **Auto-trade runs on startup**; Ctrl+C is the only stop. No dashboard-only flag. | User wants direct live trading — no two-stage rollout, no kill-switch flag. |
| Log I/O | Off main path via async queue + background drainer | User requirement: logging must not slow down trading path. |

## 4. Components

### 4.1 `variational/signal.py` (new)

**`SignalEngine`** — pure, read-only consumer of quote state.

```python
@dataclass(frozen=True, slots=True)
class SignalState:
    ts_monotonic: float
    asset: str | None
    var_bid: Decimal | None
    var_ask: Decimal | None
    lighter_bid: Decimal | None
    lighter_ask: Decimal | None
    book_spread_baseline_pct: Decimal | None
    long_direction: "DirectionState"   # long_var_short_lighter
    short_direction: "DirectionState"

@dataclass(frozen=True, slots=True)
class DirectionState:
    cross_spread_pct: Decimal | None       # raw (lighter_bid - var_ask)/var_ask, etc.
    adjusted_pct: Decimal | None           # cross_spread_pct - book_spread_baseline_pct
    median_5m_pct: float | None
    median_30m_pct: float | None
    median_1h_pct: float | None
    is_green: bool

class SignalEngine:
    def __init__(self, monitor: VariationalMonitor, book: LighterBookView, strict: bool = False): ...
    async def tick(self) -> SignalState: ...               # called every 100ms by owner
    def get_state(self) -> SignalState: ...                # last computed, for readers
    def subscribe_edge(self, direction: str, callback): ...# called on red→green edge only
```

- `strict=False` (default): `is_green = adjusted > any(m5, m30, m1h)` (current behavior)
- `strict=True` (with `--signal-strict`): `is_green = adjusted > max(m5, m30, m1h)`
- Dashboard reads `get_state()` every frame. AutoTrader subscribes to edge events.
- History buffer (`cross_spread_history` deque) and median computation move from `main.py` into this module.

### 4.2 `variational/journal.py` (new)

**`EventJournal`** — fire-and-forget async log writer.

```python
class EventJournal:
    def __init__(self, path: Path, max_queue: int = 10000, batch_ms: int = 50, batch_size: int = 100): ...
    def emit(self, event: dict) -> None:    # sync, O(1), non-blocking; drops + counts on overflow
    async def start(self) -> None: ...       # launches background drainer task
    async def stop(self) -> None: ...        # flushes remaining, closes file

    @property
    def dropped_count(self) -> int: ...      # surfaced on dashboard if > 0
```

- `emit()` does `queue.put_nowait()`. On `QueueFull`: increments `_dropped` and returns — **never raises, never blocks**.
- Background drainer coroutine batches by count or time, serializes JSON in the drainer (not in `emit`), and writes via `asyncio.to_thread(_append_line, ...)`.
- Two instances at runtime: one for `order_events.jsonl`, one for `cycle_pnl.jsonl`.
- `runtime.log` stays on the existing synchronous logger — it's low-volume.

### 4.3 `variational/command_client.py` (new)

**`VariationalCmdClient`** — Python-side client of `CommandBroker`.

```python
class VariationalCmdClient:
    def __init__(self, host: str, port: int, logger): ...
    async def start(self) -> None: ...         # connects, retries
    async def stop(self) -> None: ...
    async def place_order(
        self, side: str, qty: Decimal, asset: str,
        request_id: str, timeout_ms: int = 5000,
    ) -> VarPlaceResult: ...                   # awaits ORDER_RESULT
```

- Uses `role=requester` on REGISTER (CommandBroker already discriminates extension vs requester).
- Owns a `dict[request_id → asyncio.Future]` for correlating ACKs.
- If socket drops while a request is in flight: the in-flight future resolves with `failed(extension_disconnected)`.

### 4.4 `variational/auto_trader.py` (new)

**`AutoTrader`** — orchestrator, the only component that mutates trading state.

```python
@dataclass(slots=True)
class TradeCycle:
    cycle_id: str
    direction: str
    asset: str
    signal_snapshot: SignalState
    plan: TradePlan
    var_leg: LegState
    lighter_leg: LegState
    status: str                  # opening → settling → closed/failed
    opened_at: datetime
    closed_at: datetime | None

class AutoTrader:
    def __init__(
        self, *,
        signal: SignalEngine,
        var_cmd: VariationalCmdClient,
        lighter: LighterAdapter,       # thin wrapper around existing SignerClient code
        monitor: VariationalMonitor,
        config: AutoTraderConfig,
        events: EventJournal,
        cycles: EventJournal,
        logger,
    ): ...

    async def run(self) -> None: ...    # main loop; subscribes to signal edges
    @property
    def is_frozen(self) -> bool: ...
    def snapshot(self) -> TraderStats: ...   # for dashboard aggregate row
```

Gates checked before firing, in order:

1. `is_frozen` → skip
2. `now - last_fire_ts[direction] < throttle_seconds` → skip
3. `trades_today >= max_trades_per_day` → skip
4. Re-read quotes synchronously from `SignalEngine.get_state()`; if `adjusted <= 0` → skip (`signal_flipped`)
5. Compute `TradePlan` (expected fills, expected net pct)
6. `cycle_id = f"cyc-{now:%Y%m%d%H%M%S}-{uuid4().hex[:8]}"`
7. `await asyncio.gather(var_cmd.place_order(...), lighter.place_order(...), return_exceptions=True)`
8. Subscribe cycle to subsequent fill events (`variational_fill` from monitor, `lighter_fill` from WS)
9. Close cycle when both legs reach a terminal state **or** `leg_settle_timeout_sec` elapses
10. Emit one `cycle_pnl` record on close; update in-memory `TraderStats`

Breaker state:
- `consecutive_failures: int` (reset on any success)
- `daily_failures: int` (reset on UTC midnight)
- Trips when either `consecutive_failures >= 3` or `daily_failures >= 5`
- Position-imbalance check after each `cycle_closed`; trips if `|net_position| > max_net_imbalance`
- Manual reset: restart process, or `SIGUSR1` → `--reset-breaker` hook (nice-to-have, can skip if trivially not needed)

### 4.5 `chrome_extension/content_script.js` (new)

- Listens for `chrome.runtime.onMessage` only; rejects `window.postMessage` from the page.
- On `PLACE_ORDER`: builds `fetch(url, {method, headers, body, signal: AbortSignal.timeout(timeoutMs)})`.
- Sends `{status, body, latencyMs, errorReason?}` back to background.
- The exact Variational API URL / body shape is a known unknown at spec time — the user will capture it from DevTools as part of M1 and paste it into the implementation session. `content_script.js` is designed to accept the URL, method, and body template as parameters inside the `PLACE_ORDER` payload, so that the Python side (which knows market, side, qty) constructs them. This keeps Variational internals out of the extension's source.

### 4.6 `chrome_extension/background.js` (modified)

- New `CommandSocket` class wrapping `ws://127.0.0.1:8768` with reconnect.
- On open: sends `REGISTER {role: "extension"}`.
- On `PLACE_ORDER`: forwards to content_script of the attached variational.io tab; on content_script response, sends back `ORDER_RESULT {requestId, ok, ...}`.
- If no variational.io tab is attached: respond `ORDER_RESULT {ok: false, error: "no_attached_tab"}` immediately.

### 4.7 `chrome_extension/manifest.json` (modified)

- Add `"scripting"` to `permissions`.
- Add `content_scripts` entry matching `https://omni.variational.io/*`.
- Keep `debugger`, `tabs`, `storage`, `activeTab`.

### 4.8 `main.py` (modified)

- Remove the passive-hedge branch inside `place_lighter_order` (no more "fire Lighter only if Variational already filled").
- `VariationalToLighterRuntime.run()` now additionally:
  - Starts `VariationalCmdClient`
  - Builds `SignalEngine` (moving median/history logic out of dashboard)
  - Starts `AutoTrader`
- `render_dashboard` reads `SignalEngine.get_state()` and `AutoTrader.snapshot()` — no more inline signal computation.
- Startup log line dumps every CLI flag + resolved env config for reproducibility.

## 5. Data flow per trade cycle

1. `SignalEngine.tick()` runs every 100ms, updates state, fires edge callback on red→green.
2. `AutoTrader._on_green(direction, state)`:
   - passes all gates → allocates `cycle_id`, builds `TradePlan`
   - emits `cycle_opened` to `events` journal
   - calls `asyncio.gather(var_cmd.place_order(...), lighter.place_order(...), return_exceptions=True)`
3. For each leg:
   - successful ACK → emit `*_place_ack`
   - failure → emit `cycle_error`, increment breaker counters
4. Fill events arrive async:
   - Variational: `VariationalMonitor` already captures `/events` trades. AutoTrader polls `monitor.get_trade_events_since(cursor)` as today, but now cross-references incoming trades to open cycles by `cycle_id → trade_id` mapping populated from the Variational API response.
   - Lighter: existing `handle_lighter_fill_update` path, mapping `client_order_id → cycle_id`.
   - Partial fills update `var_leg.filled_qty` / `lighter_leg.filled_qty`.
5. Cycle closes when both legs terminal OR `leg_settle_timeout_sec` (default 10s) elapses since opened_at.
6. On close:
   - compute `attribution` block (see §6)
   - emit single `cycle_pnl` line to `cycles` journal
   - emit `cycle_closed` to `events` journal
   - update in-memory `TraderStats`

## 6. Logging & P&L attribution

### 6.1 Principle: zero sync I/O on the trading path

- Logger calls on the trading path are always `journal.emit(event)` — pure memory + `queue.put_nowait`.
- JSON serialization and disk writes happen on background drainer tasks only.
- On queue overflow (pathological): drop + count. Dashboard shows `dropped_events` when > 0.
- `runtime.log` (text, line-oriented) stays on the stdlib sync handler — it's low-volume and used only for startup banners, breaker trips, reconnect notices.

### 6.2 File layout

| File | Kind | Owner | Notes |
|---|---|---|---|
| `log/runtime.log` | text | stdlib logger | existing, unchanged |
| `log/signal_events.jsonl` | edge events | `SignalEngine` via journal | one line per red↔green flip |
| `log/order_events.jsonl` | event stream | `AutoTrader` + adapters via journal | replaces `order_metrics.jsonl` |
| `log/cycle_pnl.jsonl` | cycle summary (⭐ analysis table) | `AutoTrader` via journal | one line per closed cycle |
| `log/trade_records.csv` | dashboard snapshot | dashboard loop | existing, extended with cycle_id column |

### 6.3 `signal_events.jsonl` schema

```
{
  "ts": ISO8601 UTC,
  "event": "signal_turned_green" | "signal_turned_red",
  "direction": "long_var_short_lighter" | "short_var_long_lighter",
  "adjusted_pct": Decimal as string,
  "book_spread_baseline_pct": ..., "cross_spread_pct": ...,
  "median_5m_pct": float, "median_30m_pct": float, "median_1h_pct": float,
  "quotes": {"var_bid": str, "var_ask": str, "lighter_bid": str, "lighter_ask": str},
  "asset": str,
  "triggered_cycle_id": str | null,   // null if green but gates blocked fire
  "skip_reason": str | null           // "throttled" | "day_limit" | "frozen" | "signal_flipped" | null
}
```

### 6.4 `order_events.jsonl` event types

| event | fields (beyond `ts`, `cycle_id`) |
|---|---|
| `cycle_opened` | direction, asset, qty_target, plan (expected_var_fill_px, expected_lighter_fill_px, expected_gross_pct, expected_net_pct), signal_snapshot |
| `var_place_attempt` | request_id, side, qty, payload_sha256 |
| `var_place_ack` | request_id, forward_latency_ms |
| `var_api_request` | url, method, body (only if `--debug-var-payload` enabled) |
| `var_api_response` | status, body (redacted per flag above), latency_ms |
| `var_fill` | trade_id, fill_px, fill_qty, ts |
| `var_partial_fill` | filled_qty_so_far, remaining_qty |
| `lighter_place_attempt` | client_order_id, side, qty, limit_px |
| `lighter_place_ack` | tx_hash, error |
| `lighter_fill` | fill_px, fill_qty, ts |
| `cycle_error` | side("var"/"lighter"/"both"), error_code, error_msg |
| `breaker_tripped` | reason, consecutive_failures, daily_failures |
| `cycle_closed` | final_status |

### 6.5 `cycle_pnl.jsonl` schema (⭐ reconstruction table)

One line per closed cycle. Columns chosen so `pandas.read_json(path, lines=True)` gives a directly-analyzable dataframe.

```
{
  "cycle_id": str,
  "triggered_at": ISO8601, "closed_at": ISO8601, "duration_ms": int,
  "asset": str, "direction": str,
  "status": "fully_filled" | "partial" | "one_leg_failed" | "both_failed",

  "signal": {                              // state at trigger time
    "adjusted_pct": str, "baseline_pct": str,
    "median_5m_pct": float, "median_30m_pct": float, "median_1h_pct": float,
    "var_bid": str, "var_ask": str, "lighter_bid": str, "lighter_ask": str
  },

  "plan": {                                // pre-trade expectation
    "qty_target": str,
    "expected_var_fill_px": str, "expected_lighter_fill_px": str,
    "expected_gross_pct": float, "expected_net_pct": float
  },

  "var_leg": {
    "placed_at": ISO8601 | null, "filled_at": ISO8601 | null,
    "requested_qty": str, "filled_qty": str,
    "avg_fill_px": str | null,
    "api_latency_ms": int | null, "partial_fill_count": int,
    "fee_pct": float | null, "fee_amount": str | null
  },
  "lighter_leg": {
    "placed_at": ISO8601 | null, "filled_at": ISO8601 | null,
    "client_order_id": int | null,
    "requested_qty": str, "filled_qty": str,
    "avg_fill_px": str | null, "limit_px": str | null,
    "fee_pct": float | null, "fee_amount": str | null
  },

  "attribution": {                         // ⭐ why did realized ≠ expected?
    "realized_net_pct": float,             // actual net P&L % of notional
    "vs_expected_pct_delta": float,        // realized - plan.expected_net_pct
    "components": {
      "var_slippage_pct":     float,       // (actual - expected) / expected, signed per direction
      "lighter_slippage_pct": float,       // same
      "fee_pct":              float,       // sum of both fees
      "fill_ratio":           float,       // filled_qty / qty_target, per leg → min
      "quote_drift_ms":       int,         // signal ts → var API request ts
      "reason_codes": [str]                // empty if fully_filled & matched plan; else codes from §7
    }
  }
}
```

### 6.6 Dashboard aggregate row

Computed from in-memory `TraderStats` (never reads disk):

```
今日 N 笔 | 成功率 X% | 累计 realized_net $Y | 平均 slippage_bps: var Z1 / lighter Z2 | 熔断: OK / FROZEN[reason]
```

If `journal.dropped_count > 0`, append `⚠ 日志丢弃 K`.

## 7. Error handling matrix

| Scenario | Detection | Behavior | reason_code |
|---|---|---|---|
| Variational API non-2xx | content_script `response.status` | var_leg=failed; breaker +1 | `var_http_{status}` |
| Variational API timeout | `AbortController` at `var_order_timeout_ms` | same | `var_timeout` |
| Extension WS disconnects | background `onclose`; broker resolves pending as failed | all pending cycles fail; breaker trips | `extension_disconnected` |
| Lighter sign error | SDK returns non-None err | lighter_leg=failed; breaker +1 | `lighter_sign_error` |
| Lighter no fill within `leg_settle_timeout_sec` | timer | cycle closes as partial | `lighter_timeout` |
| Partial fill (either leg) | filled_qty < requested after settle timeout | status=partial; `fill_ratio < 1` | `var_depth_shortfall` / `lighter_depth_shortfall` |
| Signal flipped before fire | re-read quotes just before `gather` | skip, no breaker impact | `signal_flipped` |
| Position imbalance exceeded | check `|net_position| > max_net_imbalance` after each close | breaker trips | `position_imbalance` |
| Heartbeat stale | existing monitor logic | suspend new triggers; auto-resume on recovery (not a breaker) | — |

Breaker only **resets on process restart** (or `SIGUSR1` if we include the reset hook). This is deliberate — flapping auto-reset is worse than human-in-the-loop.

## 8. Security

- content_script trusts only `chrome.runtime.onMessage` from the extension itself; ignores `window.postMessage`.
- `CommandBroker` accepts connections only on `127.0.0.1`. (Already the bind, but make it enforced, not incidental.)
- Variational API bodies may contain auth tokens — **not** written to `order_events.jsonl` by default. `var_api_request` stores `payload_sha256`; full body only if `--debug-var-payload` is set. runtime.log never gets them.
- `LIGHTER_PRIVATE_KEY` must never be logged. (Current code does not log it; keep it that way.)

## 9. CLI surface (final)

| Flag | Default | Required | Notes |
|---|---|---|---|
| `--qty` | — | **yes** | Per-cycle base asset qty |
| `--throttle-seconds` | 3 | no | Min interval between same-direction fires |
| `--max-trades-per-day` | 200 | no | Global UTC-day cap |
| `--var-fee-bps` | 0 | no | Used only for plan.expected_net_pct |
| `--lighter-fee-bps` | 2 | no | Same |
| `--max-net-imbalance` | `2 × qty` | no | Position-imbalance breaker threshold |
| `--signal-strict` | false | no | If set, `any(5m,30m,1h)` → `max(5m,30m,1h)` |
| `--var-order-timeout-ms` | 5000 | no | Extension fetch timeout |
| `--leg-settle-timeout-sec` | 10 | no | Per-leg fill deadline |
| `--debug-var-payload` | false | no | If set, full Variational request/response body goes to `order_events.jsonl` |
| `--lang` | zh | no | existing |

Removed from the old CLI: `--no-hedge` (the new flow is spread-trading by design — there is no separate hedge toggle; stop by sending SIGINT). No `--auto-trade` either (auto-trade is the only mode).

## 10. Implementation milestones

Four independently-mergeable units, each manually verifiable.

### M1 · Command channel wired end-to-end

- New: `chrome_extension/content_script.js`
- Modified: `chrome_extension/background.js` (add `CommandSocket`), `manifest.json` (scripting permission + content_scripts)
- Modified: `main.py` — instantiate `CommandBroker` and keep it alive alongside existing receivers
- New: `variational/command_client.py`
- **Verify**: Python REPL → `await cmd.place_order(side="buy", qty=min, ...)` → see a real Variational order appear in UI. Use far-off-market limit price so the order can be cancelled manually before filling.

### M2 · SignalEngine extraction + signal_events.jsonl

- New: `variational/signal.py`
- New: `variational/journal.py` (EventJournal)
- Modified: `main.py` — dashboard reads `SignalEngine.get_state()` instead of computing inline
- **Verify**: run side-by-side with a prior commit; dashboard colors must match tick-for-tick. `signal_events.jsonl` should show exactly the transitions the dashboard color reflects.
- **Risk**: lowest — pure refactor + additive logging, no trading behavior change.

### M3 · AutoTrader + cycle_pnl.jsonl

- New: `variational/auto_trader.py`
- Modified: `main.py` — replace passive `place_lighter_order` branching with AutoTrader wiring; wire CLI flags
- **Verify**: real run with `--qty <tiny> --max-trades-per-day 5` for 15 minutes; manually reconcile each `cycle_pnl.jsonl` line against UI fills and Lighter account history.

### M4 · Dashboard stats row + CLAUDE.md update

- Modified: `main.py` dashboard — add bottom aggregate row
- Modified: `CLAUDE.md` — new components and log files in architecture section
- **Verify**: kill Chrome extension mid-run → see dashboard turn red `FROZEN[extension_disconnected]`, `cycle_pnl.jsonl` contains a failed-status row, no further cycles fire.

## 11. Out-of-scope / deferred

- Orderbook-depth-aware dynamic qty
- Automatic residual-position flattening after breaker
- Test harness (pytest / fixtures)
- External alerting (Slack/webhook)
- Cross-restart breaker state persistence
- Multi-asset simultaneous trading (current design is single-market at a time, tied to `current_quote_asset`)
