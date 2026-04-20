# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Environment setup (Python 3.10+ required due to `slots=True` / PEP 604 types):

```bash
python3 -m venv env
source env/bin/activate
pip install -r requirements.txt
```

Run the main runtime (auto-trade, Chinese dashboard):

```bash
python main.py --qty 0.01                  # required; units = base asset
python main.py --qty 0.01 --lang en        # English dashboard
python main.py --qty 0.01 --signal-strict  # stricter green gate: max of medians instead of any
```

`--qty` is required. Other flags default to safe values; see `python main.py --help` or the design spec. There is no longer a `--no-hedge` flag — send SIGINT (Ctrl+C) to stop.

Run only the forwarder receiver without hedging logic (dev/debug):

```bash
python -m variational --output-dir ./log   # flags: --ws-port, --rest-port, --command-port, --quiet, --no-monitor, --snapshot-file
```

Lint (only config is line length):

```bash
flake8 .   # max-line-length = 129
```

No test suite exists in this repo.

Required `.env` for live hedging:

```
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=...
LIGHTER_PRIVATE_KEY=...
# Optional: LIGHTER_WS_SERVER_PINGS=true to force legacy app-level ping/pong
# Optional: API_KEY_PRIVATE_KEY overrides LIGHTER_PRIVATE_KEY
VARIATIONAL_ORDER_URL=https://omni.variational.io/api/orders
VARIATIONAL_ORDER_METHOD=POST
VARIATIONAL_ORDER_BODY_TEMPLATE={"instrument":"{asset}","side":"{side}","qty":"{qty}","type":"market"}
```

Real `VARIATIONAL_ORDER_*` values must be captured from browser DevTools for the logged-in user.

## Architecture

The system is a **three-process pipeline**: Chrome extension → local WebSocket receivers → runtime that mirrors Variational fills onto Lighter.

### 1. Chrome extension (`chrome_extension/`)

A CDP-based forwarder (`manifest_version: 3`, background service worker). It attaches the Chrome debugger to a Variational tab and intercepts:

- REST responses matching `https://omni.variational.io/api/quotes/indicative`
- WS frames on `wss://omni-ws-server.prod.ap-northeast-1.variational.io/events` and `/portfolio`

Frames are forwarded as JSON envelopes to two local WebSocket servers:

- `ws://127.0.0.1:8766` — WS frames
- `ws://127.0.0.1:8767` — REST responses

The Python runtime MUST be running before the extension is started — the extension will queue (bounded) and retry if the receivers are down. The extension is activated from its popup (`popup.html` → Start button).

### 2. Local receivers + monitor (`variational/listener.py`)

- `run_receiver_server` spins up a `websockets.serve` endpoint per channel.
- `EventSink.handle` decodes CDP envelopes and routes to `VariationalMonitor`.
- `VariationalMonitor` is the single source of truth for Variational state. It parses:
  - `/api/quotes/indicative` REST body → `quotes[asset]` with bid/ask/mark and sets `current_quote_asset` (this is how the active asset is auto-detected — no manual ticker input).
  - `/events` WS → heartbeats, trades (assigned a monotonic `event_seq` used as a cursor by consumers), and other typed events.
  - `/portfolio` WS → positions and pool balance/pnl/margin.
- `CommandBroker` runs on port `8768` and is a generic fetch-proxy router (EXECUTE_FETCH / FETCH_RESULT) between an external requester and an extension-registered socket. Not wired into `main.py`'s runtime — it's only used when running `python -m variational` standalone.
- All monitor state reads/writes go through `monitor._lock` (asyncio). Consumers that reach into `monitor.quotes` / `monitor.current_quote_asset` directly (as `main.py` does) must acquire that lock.

### 3. Runtime orchestrator (`main.py`)

`VariationalToLighterRuntime` is a single class that owns the whole live system. Key architectural points:

- **Auto-detected ticker.** The runtime never takes a ticker flag. It reads `monitor.current_quote_asset` from quote messages, applies `VARIATIONAL_TICKER_OVERRIDES` (e.g. Variational `LIT` ↔ Lighter `LIGHTER`), then calls `get_lighter_market_config` (REST `GET /api/v1/orderBooks`) to resolve `market_id` and the decimal multipliers.
- **Debounced asset switching.** `trade_loop` polls the monitor; when a new asset is seen for `ASSET_SWITCH_CONFIRM_TICKS` consecutive ticks, `activate_asset` tears down the Lighter WS task, clears order/record state and the cross-spread history, and spins up a new Lighter subscription for the new market.
- **Lighter order book WS.** `handle_lighter_ws` subscribes to `order_book/{market_index}` and `account_orders/{market_index}/{account_index}`. It maintains a local book with offset-based sequence validation — any regression in `offset` flags `lighter_order_book_sequence_gap` and triggers a resubscribe to get a fresh snapshot. Best bid/ask are recomputed on every delta. The authenticated channel requires a SignerClient auth token (see `create_auth_token_with_expiry`). WS URL honors `LIGHTER_WS_SERVER_PINGS` for legacy ping/pong mode.
- **Hedge path.** Variational trade events → `process_variational_trade_event` creates an `OrderLifecycle` the first time a `trade_key` is seen (trade_key = `id:<trade_id>` if present, else `seq:<event_seq>`). On creation (and only on creation), it immediately calls `place_lighter_order`, which inverts the side, computes a limit price from Lighter best bid/ask with `HEDGE_SLIPPAGE_BPS` (100 bps) of slippage, converts to integer base/price units via `base_amount_multiplier` / `price_multiplier`, and places a GTT limit via `SignerClient.create_order`. Fills flow back through `account_orders` WS → `handle_lighter_fill_update`.
- **Lock discipline.** Four async locks, each with a specific scope: `_record_lock` (records/record_order/lighter_client_order_to_trade_key), `lighter_order_book_lock` (book dict + best bid/ask + offset + ready flags), `_lighter_signer_lock` (wraps every SignerClient call), `_asset_switch_lock` (serializes `activate_asset`). Never hold more than one at a time.
- **Dashboard.** `dashboard_loop` uses `rich.Live` with `screen=True` (alternate screen buffer) — so regular `print()` from other code would corrupt rendering. Anything outside the dashboard must log via `self.logger` (writes to `./log/runtime.log`, never stdout). Dashboard color signals: cross-venue spread % turns green when the spread (after subtracting the mean of the two venues' book-spread baselines) exceeds any of the 5m/30m/1h medians of its own history.
- **Outputs** (all under `./log/`): `runtime.log` (text), `order_metrics.jsonl` (event-sourced: `variational_fill`, `lighter_fill`, `lighter_error`), `trade_records.csv` (snapshot overwritten atomically via `.tmp` + `os.replace` on each dashboard tick, skipped if the signature hasn't changed).

### Cross-module invariants

- The `variational/` package exposes `VariationalMonitor`, `EventSink`, `run_receiver_server`, and `HEARTBEAT_STALE_SECONDS` to `main.py`. `main.py` constructs its own `VariationalRuntime` wrapper around these rather than using `variational.listener.run` (which is the standalone entrypoint with its own CLI and CommandBroker).
- Ticker mapping is centralized in `VARIATIONAL_TICKER_OVERRIDES` in `main.py`. When adding a new asset whose symbol differs between venues, add it there — `resolve_variational_ticker` / `resolve_lighter_ticker` and `accepted_assets` all derive from it.
- Heartbeat staleness: the monitor considers the `/events` stream alive if a heartbeat was seen within `HEARTBEAT_STALE_SECONDS` (11s). `wait_for_variational_ready` blocks startup on this.

### 4. Auto-trade pipeline (added 2026-04-19)

The runtime is no longer passive. On every `SignalEngine` green-edge (same formula as the dashboard used to compute inline), `AutoTrader` fires Variational + Lighter legs in parallel via:

- `VariationalCmdClient` → `CommandBroker` (:8768) → Chrome extension `background.js CommandSocket` → `content_script.js` `fetch()` on variational.io (browser session, so auth/cookies flow automatically).
- `LighterAdapterImpl.place_order` (thin wrapper over the existing `SignerClient.create_order`).

Cycle IDs (`cyc-YYYYMMDDHHMMSS-8hex`) correlate every event back to a single row in `log/cycle_pnl.jsonl` — that file is the canonical replay table.

Logging is off the main path: both `order_events.jsonl` and `cycle_pnl.jsonl` go through `EventJournal`, an async-queue-backed writer that drops-and-counts on overflow rather than blocking. Signal edges go to `signal_events.jsonl` via the same mechanism.

Breaker state machine: 3 consecutive failures or 5 failures in a UTC day → `TraderStats.frozen = True` → dashboard panel turns red, no new cycles fire. Only a process restart clears it.

The Variational order API URL / body shape is **runtime config** (not code). Set `VARIATIONAL_ORDER_URL`, `VARIATIONAL_ORDER_METHOD`, and `VARIATIONAL_ORDER_BODY_TEMPLATE` in `.env`; the body template uses `{side}`, `{qty}`, `{asset}` placeholders.

Key files:
- `variational/signal.py` — `SignalEngine`, `SignalState`, `DirectionState`
- `variational/auto_trader.py` — `AutoTrader`, `TradeCycle`, attribution math
- `variational/command_client.py` — `VariationalCmdClient`
- `variational/journal.py` — `EventJournal`
- `chrome_extension/content_script.js` — fetch proxy inside variational.io
