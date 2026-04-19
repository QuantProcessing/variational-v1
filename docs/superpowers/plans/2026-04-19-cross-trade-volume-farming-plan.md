# Cross-Trade Volume Farming Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn `variational-v1` from passive-hedge into a self-triggering cross-venue spread trader that farms Variational volume at near-zero cost, with complete per-cycle P&L attribution.

**Architecture:** A Chrome extension content script acts as a "dumb fetch proxy" against variational.io so Python can place orders inheriting the browser session. A `SignalEngine` (green-light detector lifted out of the dashboard) feeds an `AutoTrader` that fires Variational + Lighter legs in parallel on every green edge. Two async-queue-backed `EventJournal` instances stream fine-grained events and per-cycle attribution summaries to disk without ever blocking the trade path.

**Tech Stack:** Python 3.10+ (asyncio, websockets, lighter-sdk, rich), Chrome MV3 extension (JS). flake8 max-line-length = 129.

**Testing Approach:** No pytest (per spec §10 — user-approved YAGNI). Pure/semi-pure modules (`EventJournal`, `SignalEngine`, attribution math) ship with `if __name__ == "__main__"` self-test blocks that assert deterministic outputs; run them via `python -m variational.<module>`. Integration / network paths (content_script, Lighter orders, real API calls) verified manually per task with explicit REPL scripts and expected outputs documented inline.

**Prerequisite:** The exact Variational order API URL / body shape is a known unknown. M1 uses a read-only dummy endpoint (any existing variational.io GET) to validate the channel. M3 Task 3.7 requires the user to capture the real POST from DevTools and paste it into `.env` before going live.

---

## File Structure

| Path | Kind | Responsibility |
|---|---|---|
| `variational/journal.py` | new | Async-queue-backed JSONL writer. Never blocks caller. |
| `variational/signal.py` | new | `SignalEngine`, `SignalState`, `DirectionState`; median + green-light math. |
| `variational/command_client.py` | new | Python WS client for CommandBroker; request/response correlation. |
| `variational/auto_trader.py` | new | Trade cycle orchestrator. Gates, parallel fire, settlement, attribution, breaker. |
| `variational/listener.py` | modify | Update `CommandBroker` message schema to a generic fetch-proxy contract. |
| `main.py` | modify | Replace passive-hedge branching; wire SignalEngine + AutoTrader + CmdClient + journals; new CLI flags; dashboard reads from SignalEngine / AutoTrader; stats row; breaker banner. |
| `chrome_extension/manifest.json` | modify | Add `scripting` permission + `content_scripts` block. |
| `chrome_extension/background.js` | modify | Add `CommandSocket` class (connect :8768, relay to content_script, respond). |
| `chrome_extension/content_script.js` | new | Inject into variational.io; execute `EXECUTE_FETCH` messages; never trust page postMessage. |
| `CLAUDE.md` | modify | Document new components + log files in architecture section. |
| `docs/superpowers/plans/...` | this file | — |

---

## Milestone 1 — Command Channel End-to-End

Goal: a Python script can `await cmd.execute_fetch(...)` and cause Chrome to perform a real `fetch()` on variational.io, returning the HTTP status and body.

### Task 1.1 — Generalize CommandBroker to a fetch-proxy contract

The existing `CommandBroker` has a `PLACE_ORDER` handler with hard-coded `side/amount/market` fields. Replace with a generic `EXECUTE_FETCH` schema so all Variational-specific knowledge stays in Python.

**Files:**
- Modify: `variational/listener.py:519-670` (`CommandBroker.handle_raw_message` and `_handle_place_order`)

- [ ] **Step 1: Replace `_handle_place_order` with `_handle_execute_fetch`**

Edit `variational/listener.py`, replace the entire `_handle_place_order` method (starting at line ~589) with:

```python
async def _handle_execute_fetch(self, websocket: websockets.ServerConnection, payload: dict[str, Any]) -> None:
    request_id = str(payload.get("requestId") or uuid.uuid4())
    fetch = payload.get("fetch")
    if not isinstance(fetch, dict) or not fetch.get("url"):
        await self._send(
            websocket,
            {
                "type": "FETCH_RESULT",
                "requestId": request_id,
                "ok": False,
                "error": "fetch.url is required.",
                "timestamp": utc_now(),
            },
        )
        return

    async with self._lock:
        extension = self._extension
        if extension is None:
            await self._send(
                websocket,
                {
                    "type": "FETCH_RESULT",
                    "requestId": request_id,
                    "ok": False,
                    "error": "no_attached_extension",
                    "timestamp": utc_now(),
                },
            )
            return

        self._pending_requests[request_id] = websocket
        forward_payload = {
            "type": "EXECUTE_FETCH",
            "requestId": request_id,
            "fetch": {
                "url": fetch.get("url"),
                "method": fetch.get("method", "GET"),
                "headers": fetch.get("headers") or {},
                "body": fetch.get("body"),
            },
            "timeoutMs": int(payload.get("timeoutMs") or 5000),
            "timestamp": utc_now(),
        }
        await self._send(extension, forward_payload)
```

- [ ] **Step 2: Update the dispatch in `handle_raw_message`**

In `variational/listener.py`, find the `handle_raw_message` block (lines ~546-558) and replace the `PLACE_ORDER`/`ORDER_RESULT` branches:

```python
if msg_type == "EXECUTE_FETCH":
    await self._handle_execute_fetch(websocket, payload)
    return
if msg_type == "FETCH_RESULT":
    await self._handle_fetch_result(payload)
    return
```

- [ ] **Step 3: Rename `_handle_order_result` → `_handle_fetch_result` and the on-disconnect failure payload**

In `variational/listener.py`:

```python
async def _handle_fetch_result(self, payload: dict[str, Any]) -> None:
    request_id = str(payload.get("requestId", "")).strip()
    if not request_id:
        return
    async with self._lock:
        requester = self._pending_requests.pop(request_id, None)

    if requester is not None:
        await self._send(requester, payload)
        if not self.quiet:
            print(f"[COMMAND] fetch_result requestId={request_id} ok={payload.get('ok')}", flush=True)
```

And in `on_disconnect` (line ~500), change `ORDER_RESULT` to `FETCH_RESULT` and the error to `"extension_disconnected"`.

- [ ] **Step 4: Syntax check**

```bash
python -c "from variational.listener import CommandBroker; print('ok')"
```

Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add variational/listener.py
git commit -m "$(cat <<'EOF'
Generalize CommandBroker to fetch-proxy contract

Replace PLACE_ORDER/ORDER_RESULT with EXECUTE_FETCH/FETCH_RESULT so the
extension becomes a dumb proxy and all Variational API knowledge lives
in Python.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.2 — `VariationalCmdClient`

Python-side client for the broker. Connects, registers as `requester`, correlates requests by ID, auto-reconnects.

**Files:**
- Create: `variational/command_client.py`

- [ ] **Step 1: Write the module**

Create `variational/command_client.py`:

```python
"""Python client for variational/listener.py CommandBroker."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

import websockets


@dataclass(slots=True)
class FetchResult:
    ok: bool
    status: int | None = None
    body: str | None = None
    latency_ms: int | None = None
    error: str | None = None
    request_id: str | None = None


class VariationalCmdClient:
    def __init__(self, host: str, port: int, logger: logging.Logger) -> None:
        self.url = f"ws://{host}:{port}"
        self.logger = logger
        self._ws: websockets.ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[FetchResult]] = {}
        self._run_task: asyncio.Task[None] | None = None
        self._stop = False
        self._connected = asyncio.Event()

    async def start(self) -> None:
        self._run_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        self._stop = True
        if self._ws is not None:
            await self._ws.close()
        if self._run_task is not None:
            self._run_task.cancel()
            try:
                await self._run_task
            except asyncio.CancelledError:
                pass

    async def wait_ready(self, timeout: float = 10.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout=timeout)

    async def execute_fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout_ms: int = 5000,
    ) -> FetchResult:
        if self._ws is None:
            return FetchResult(ok=False, error="not_connected")

        request_id = uuid.uuid4().hex
        fut: asyncio.Future[FetchResult] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = fut

        payload = {
            "type": "EXECUTE_FETCH",
            "requestId": request_id,
            "fetch": {"url": url, "method": method, "headers": headers or {}, "body": body},
            "timeoutMs": timeout_ms,
        }
        try:
            await self._ws.send(json.dumps(payload))
        except Exception as exc:
            self._pending.pop(request_id, None)
            return FetchResult(ok=False, error=f"send_failed: {exc}", request_id=request_id)

        try:
            return await asyncio.wait_for(fut, timeout=(timeout_ms / 1000.0) + 2.0)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return FetchResult(ok=False, error="client_timeout", request_id=request_id)

    async def _run_forever(self) -> None:
        backoff = 1.0
        while not self._stop:
            try:
                async with websockets.connect(self.url, ping_interval=20, ping_timeout=20) as ws:
                    self._ws = ws
                    await ws.send(json.dumps({"type": "REGISTER", "role": "requester"}))
                    self._connected.set()
                    backoff = 1.0
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        await self._on_message(raw)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.logger.warning("CmdClient reconnect after error: %s", exc)
            finally:
                self._ws = None
                self._connected.clear()
                self._fail_all_pending("connection_lost")
            if self._stop:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2.0, 10.0)

    async def _on_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(msg, dict):
            return
        if str(msg.get("type", "")).upper() != "FETCH_RESULT":
            return
        request_id = str(msg.get("requestId", ""))
        fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return
        fut.set_result(
            FetchResult(
                ok=bool(msg.get("ok", False)),
                status=msg.get("status"),
                body=msg.get("body"),
                latency_ms=msg.get("latencyMs"),
                error=msg.get("error"),
                request_id=request_id,
            )
        )

    def _fail_all_pending(self, reason: str) -> None:
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(FetchResult(ok=False, error=reason, request_id=rid))
            self._pending.pop(rid, None)
```

- [ ] **Step 2: Import-smoke**

```bash
python -c "from variational.command_client import VariationalCmdClient, FetchResult; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add variational/command_client.py
git commit -m "$(cat <<'EOF'
Add VariationalCmdClient for CommandBroker

Async WS client with request/response correlation by requestId,
auto-reconnect with exponential backoff, and connection_lost
fallback that fails all pending futures.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.3 — Chrome extension: manifest + content_script

**Files:**
- Modify: `chrome_extension/manifest.json`
- Create: `chrome_extension/content_script.js`

- [ ] **Step 1: Update manifest.json**

Replace the file contents with:

```json
{
  "manifest_version": 3,
  "name": "Variational CDP Forwarder",
  "version": "1.1.0",
  "description": "Capture Variational REST responses and WebSocket frames via CDP and forward to local WebSocket ports. Also proxies fetch() commands from local Python into variational.io.",
  "permissions": ["debugger", "tabs", "storage", "activeTab", "scripting"],
  "host_permissions": ["<all_urls>"],
  "action": {
    "default_title": "Variational Forwarder",
    "default_popup": "popup.html"
  },
  "background": {
    "service_worker": "background.js",
    "type": "module"
  },
  "content_scripts": [
    {
      "matches": ["https://omni.variational.io/*"],
      "js": ["content_script.js"],
      "run_at": "document_idle",
      "all_frames": false
    }
  ],
  "content_security_policy": {
    "extension_pages": "script-src 'self'; object-src 'self'; connect-src ws://127.0.0.1:*"
  }
}
```

- [ ] **Step 2: Create content_script.js**

Create `chrome_extension/content_script.js`:

```javascript
// content_script.js — runs in variational.io page context.
// Only trusts messages from the extension's background worker.
// Executes fetch() and returns (status, body, latencyMs) verbatim.

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || msg.type !== "EXECUTE_FETCH") {
    return false;
  }
  if (!sender || sender.id !== chrome.runtime.id) {
    sendResponse({ ok: false, error: "untrusted_sender" });
    return false;
  }

  const fetchSpec = msg.fetch || {};
  const timeoutMs = Math.max(500, Number(msg.timeoutMs) || 5000);

  (async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    const t0 = performance.now();
    try {
      const response = await fetch(fetchSpec.url, {
        method: fetchSpec.method || "GET",
        headers: fetchSpec.headers || {},
        body: fetchSpec.body !== undefined ? fetchSpec.body : undefined,
        credentials: "include",
        signal: controller.signal,
      });
      const text = await response.text();
      sendResponse({
        ok: true,
        status: response.status,
        body: text,
        latencyMs: Math.round(performance.now() - t0),
      });
    } catch (err) {
      const isAbort = err && err.name === "AbortError";
      sendResponse({
        ok: false,
        error: isAbort ? "timeout" : String(err && err.message ? err.message : err),
        latencyMs: Math.round(performance.now() - t0),
      });
    } finally {
      clearTimeout(timer);
    }
  })();

  return true; // keep sendResponse channel open for async work
});

// Strong signal for debugging: show up in DevTools console once loaded.
console.log("[variational-forwarder] content_script loaded");
```

- [ ] **Step 3: Commit**

```bash
git add chrome_extension/manifest.json chrome_extension/content_script.js
git commit -m "$(cat <<'EOF'
Add content_script.js as dumb fetch proxy

Injects into omni.variational.io and executes EXECUTE_FETCH messages
from the extension background worker. Only trusts chrome.runtime
senders; ignores window.postMessage. Returns verbatim status/body/
latency.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.4 — Chrome extension: CommandSocket in background.js

Add a WS client that connects to `ws://127.0.0.1:8768`, registers as `role=extension`, receives `EXECUTE_FETCH`, relays to the content_script of the attached variational.io tab, and returns `FETCH_RESULT`.

**Files:**
- Modify: `chrome_extension/background.js` (append at the end + init hooks)

- [ ] **Step 1: Append CommandSocket class**

Add at the end of `chrome_extension/background.js`:

```javascript
// ==== CommandSocket: bridges Python CommandBroker (:8768) to the variational.io content_script ====
const COMMAND_ENDPOINT = "ws://127.0.0.1:8768";

class CommandSocket {
  constructor() {
    this.ws = null;
    this.retryTimer = null;
    this.status = "disconnected";
  }

  connect() {
    if (this.ws && (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CONNECTING)) {
      return;
    }
    try {
      const socket = new WebSocket(COMMAND_ENDPOINT);
      this.ws = socket;
      this.status = "connecting";

      socket.onopen = () => {
        if (this.ws !== socket) return;
        this.status = "connected";
        socket.send(JSON.stringify({ type: "REGISTER", role: "extension" }));
      };

      socket.onclose = () => {
        if (this.ws === socket) {
          this.ws = null;
          this.status = "disconnected";
          this.scheduleRetry();
        }
      };

      socket.onerror = () => {
        try { socket.close(); } catch (_) {}
      };

      socket.onmessage = async (event) => {
        let payload = null;
        try {
          payload = JSON.parse(event.data);
        } catch (_) { return; }
        if (!payload || typeof payload !== "object") return;
        const type = String(payload.type || "").toUpperCase();
        if (type === "EXECUTE_FETCH") {
          await this.handleExecuteFetch(payload);
        }
      };
    } catch (err) {
      this.scheduleRetry();
    }
  }

  scheduleRetry() {
    if (this.retryTimer) return;
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      if (state.active) this.connect();
    }, 2000);
  }

  async handleExecuteFetch(payload) {
    const requestId = payload.requestId;
    const targetTabId = state.attachedTabId;
    if (targetTabId == null) {
      this.sendResult({ type: "FETCH_RESULT", requestId, ok: false, error: "no_attached_tab" });
      return;
    }
    try {
      const response = await chrome.tabs.sendMessage(targetTabId, {
        type: "EXECUTE_FETCH",
        fetch: payload.fetch || {},
        timeoutMs: payload.timeoutMs || 5000,
      });
      this.sendResult({
        type: "FETCH_RESULT",
        requestId,
        ok: !!response?.ok,
        status: response?.status,
        body: response?.body,
        latencyMs: response?.latencyMs,
        error: response?.error,
      });
    } catch (err) {
      this.sendResult({
        type: "FETCH_RESULT",
        requestId,
        ok: false,
        error: `content_script_unreachable: ${err && err.message ? err.message : err}`,
      });
    }
  }

  sendResult(msg) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      try { this.ws.send(JSON.stringify(msg)); } catch (_) {}
    }
  }

  disconnect() {
    if (this.retryTimer) { clearTimeout(this.retryTimer); this.retryTimer = null; }
    if (this.ws) { try { this.ws.close(); } catch (_) {} this.ws = null; }
    this.status = "disconnected";
  }
}

const commandSocket = new CommandSocket();
```

- [ ] **Step 2: Hook lifecycle into existing start/stop**

In `chrome_extension/background.js`, find the function(s) that set `state.active = true` (on Start) and `state.active = false` (on Stop). Add to them respectively:

```javascript
// On start:
commandSocket.connect();

// On stop:
commandSocket.disconnect();
```

If you cannot find exact hooks, search for `state.active =` in the file — both assignments exist. Add the calls immediately after each.

- [ ] **Step 3: Reload extension and manual smoke**

1. Open `chrome://extensions`, click reload on "Variational CDP Forwarder"
2. Open variational.io in a tab, open its DevTools console → expect `[variational-forwarder] content_script loaded`
3. Click the extension popup → Start
4. Open the service-worker console (chrome://extensions → inspect service worker) → no errors expected

- [ ] **Step 4: Commit**

```bash
git add chrome_extension/background.js
git commit -m "$(cat <<'EOF'
Add CommandSocket to background.js

Bridges Python CommandBroker on :8768 into the variational.io content
script. Relays EXECUTE_FETCH messages, returns verbatim FETCH_RESULT.
Retries with 2s backoff on disconnect; released on Stop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1.5 — Wire CommandBroker into main runtime + end-to-end manual verify

Currently `CommandBroker` is only instantiated by the standalone `python -m variational` entrypoint. Bring it into `main.py`'s `VariationalRuntime` so `main.py` can talk to the extension.

**Files:**
- Modify: `main.py` (imports, `VariationalRuntime`)

- [ ] **Step 1: Extend imports**

In `main.py`, update the `from variational.listener import ...` block (around line 27) to also import `CommandBroker` and add a constant for the port:

```python
from variational.listener import (
    HEARTBEAT_STALE_SECONDS,
    CommandBroker,
    EventSink,
    VariationalMonitor,
    run_command_server,
    run_receiver_server,
)
```

And in the constants block near the top (around line 40):

```python
FORWARDER_COMMAND_PORT = 8768
```

- [ ] **Step 2: Expose `run_command_server` if not already exported**

Verify `variational/listener.py` already defines `run_command_server` (line ~780) — it does. No change needed there.

- [ ] **Step 3: Instantiate CommandBroker in VariationalRuntime**

In `main.py`, find `class VariationalRuntime` (line ~173). Modify `__init__` and `start`/`stop`:

```python
class VariationalRuntime:
    def __init__(
        self,
        host: str,
        ws_port: int,
        rest_port: int,
        command_port: int,
        output_dir: Path | None,
        quiet: bool,
    ) -> None:
        self.monitor = VariationalMonitor(trade_limit=500, snapshot_file=None)
        self.sink = EventSink(output_dir=output_dir, quiet=quiet, monitor=self.monitor)
        self.command_broker = CommandBroker(quiet=quiet)
        self.host = host
        self.ws_port = ws_port
        self.rest_port = rest_port
        self.command_port = command_port
        self.ws_server = None
        self.rest_server = None
        self.command_server = None

    async def start(self) -> None:
        self.ws_server = await run_receiver_server("ws", self.host, self.ws_port, self.sink)
        self.rest_server = await run_receiver_server("rest", self.host, self.rest_port, self.sink)
        self.command_server = await run_command_server(self.host, self.command_port, self.command_broker)

    async def stop(self) -> None:
        for server in (self.ws_server, self.rest_server, self.command_server):
            if server is not None:
                server.close()
                await server.wait_closed()
```

- [ ] **Step 4: Update caller to pass `command_port`**

In `VariationalToLighterRuntime.__init__` (around line 223), pass the new port:

```python
self.runtime = VariationalRuntime(
    host=FORWARDER_HOST,
    ws_port=FORWARDER_WS_PORT,
    rest_port=FORWARDER_REST_PORT,
    command_port=FORWARDER_COMMAND_PORT,
    output_dir=None,
    quiet=True,
)
```

- [ ] **Step 5: End-to-end manual verify script**

Run the runtime in one terminal:

```bash
python main.py --no-hedge
```

(Note: `--no-hedge` still exists at this milestone; it's removed in Task 3.7.)

In a second terminal, with the extension running and a variational.io tab open:

```bash
python - <<'PY'
import asyncio, logging
from variational.command_client import VariationalCmdClient

async def main():
    logging.basicConfig(level=logging.INFO)
    client = VariationalCmdClient("127.0.0.1", 8768, logging.getLogger("cmd"))
    await client.start()
    await client.wait_ready(timeout=5)
    # Use any harmless variational.io GET endpoint as smoke test.
    # The quotes/indicative endpoint is already being fetched by the page, safe to GET again.
    r = await client.execute_fetch(
        url="https://omni.variational.io/api/quotes/indicative",
        method="GET",
        timeout_ms=5000,
    )
    print("ok=", r.ok, "status=", r.status, "latency_ms=", r.latency_ms, "error=", r.error)
    print("body[:200]=", (r.body or "")[:200])
    await client.stop()

asyncio.run(main())
PY
```

Expected: `ok= True status= 200 latency_ms= <small int> error= None` and a JSON prefix in `body[:200]`.

If `ok=False error=no_attached_tab`: click the extension popup → Start, then rerun.

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
Wire CommandBroker into VariationalRuntime

Starts command server on :8768 alongside the WS/REST receivers so the
main runtime can talk to the Chrome extension via fetch-proxy. Routing
remains opt-in (nothing consumes it yet) but the channel is live.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 2 — SignalEngine + Async Logging Infrastructure

Goal: extract the green-light detector from `main.py` into its own module so dashboard and the future AutoTrader share state, and land the non-blocking journal that will absorb all trade-path logging.

### Task 2.1 — `EventJournal`

**Files:**
- Create: `variational/journal.py`

- [ ] **Step 1: Write the module with an inline self-test**

Create `variational/journal.py`:

```python
"""Async-queue-backed JSONL writer. Never blocks the caller.

Trade-path callers use `emit()` which is sync and O(1). Serialization
and disk writes happen on a background drainer task. On queue overflow
events are dropped and counted; they are never dropped silently without
a counter.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class EventJournal:
    def __init__(
        self,
        path: Path,
        max_queue: int = 10000,
        batch_ms: int = 50,
        batch_size: int = 100,
    ) -> None:
        self.path = path
        self.max_queue = max_queue
        self.batch_ms = batch_ms
        self.batch_size = batch_size
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._drainer: asyncio.Task[None] | None = None
        self._stop = False
        self._dropped = 0
        self._written = 0

    @property
    def dropped_count(self) -> int:
        return self._dropped

    @property
    def written_count(self) -> int:
        return self._written

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._queue = asyncio.Queue(maxsize=self.max_queue)
        self._stop = False
        self._drainer = asyncio.create_task(self._drain_forever())

    async def stop(self) -> None:
        self._stop = True
        if self._drainer is not None:
            try:
                await asyncio.wait_for(self._drainer, timeout=5.0)
            except asyncio.TimeoutError:
                self._drainer.cancel()

    def emit(self, event: dict[str, Any]) -> None:
        if self._queue is None:
            self._dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self._dropped += 1

    async def _drain_forever(self) -> None:
        assert self._queue is not None
        batch: list[dict[str, Any]] = []
        deadline = time.monotonic() + (self.batch_ms / 1000.0)
        while not (self._stop and self._queue.empty() and not batch):
            timeout = max(0.0, deadline - time.monotonic())
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=timeout) if timeout > 0 else self._queue.get_nowait()
                batch.append(event)
            except (asyncio.TimeoutError, asyncio.QueueEmpty):
                pass

            now = time.monotonic()
            if batch and (len(batch) >= self.batch_size or now >= deadline or self._stop):
                await self._flush(batch)
                batch = []
                deadline = now + (self.batch_ms / 1000.0)

            if self._stop and self._queue.empty() and not batch:
                return

    async def _flush(self, batch: list[dict[str, Any]]) -> None:
        lines = "".join(json.dumps(ev, ensure_ascii=True, default=str) + "\n" for ev in batch)
        await asyncio.to_thread(self._append_text, self.path, lines)
        self._written += len(batch)

    @staticmethod
    def _append_text(path: Path, text: str) -> None:
        with path.open("a", encoding="utf-8") as f:
            f.write(text)


async def _self_test() -> None:
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "journal_test.jsonl"
    journal = EventJournal(tmp, max_queue=16, batch_ms=10, batch_size=4)
    await journal.start()

    for i in range(10):
        journal.emit({"i": i, "msg": f"hello {i}"})
    # Overflow test: fill past max_queue without awaiting drainer.
    for i in range(10, 100):
        journal.emit({"i": i})
    await asyncio.sleep(0.2)
    await journal.stop()

    lines = tmp.read_text(encoding="utf-8").splitlines()
    assert journal.written_count == len(lines), f"written={journal.written_count} lines={len(lines)}"
    assert journal.written_count + journal.dropped_count >= 100
    assert lines[0].startswith('{"i": 0'), lines[0]
    print(f"EventJournal OK: written={journal.written_count} dropped={journal.dropped_count} path={tmp}")


if __name__ == "__main__":
    asyncio.run(_self_test())
```

- [ ] **Step 2: Run the self-test**

```bash
python -m variational.journal
```

Expected: `EventJournal OK: written=<N> dropped=<M> path=/tmp/...` with `N+M >= 100`.

- [ ] **Step 3: Commit**

```bash
git add variational/journal.py
git commit -m "$(cat <<'EOF'
Add EventJournal async JSONL writer

emit() is sync and O(1); serialization and disk writes run on a
background drainer. Queue overflow drops + counts, never blocks.
Ships with a self-test runnable via python -m variational.journal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.2 — `SignalEngine` — types + median logic

Lift the cross-spread history, median computations, and green-light check out of `main.py`'s dashboard code into a dedicated module.

**Files:**
- Create: `variational/signal.py`

- [ ] **Step 1: Write the module**

Create `variational/signal.py`:

```python
"""SignalEngine: cross-venue green-light detector.

Reads Variational quotes and Lighter best bid/ask, maintains a rolling
history of directional cross-spreads (adjusted for book-spread cost),
and flips a green/red state per direction when adjusted > any (or max,
when strict) of the 5m / 30m / 1h medians.

Dashboard and AutoTrader both read from the same SignalEngine instance
so their green-light decisions cannot drift.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Awaitable, Callable, Optional

SPREAD_HISTORY_SECONDS = 3600.0


def _to_float(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value)


def _spread_value(a: Decimal | None, b: Decimal | None) -> Decimal | None:
    if a is None or b is None:
        return None
    return b - a


def _spread_percent(diff: Decimal | None, denom: Decimal | None) -> Decimal | None:
    if diff is None or denom is None or denom == 0:
        return None
    return (diff / denom) * Decimal("100")


def _book_spread_percent(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / Decimal("2")
    if mid == 0:
        return None
    return ((ask - bid) / mid) * Decimal("100")


@dataclass(frozen=True, slots=True)
class DirectionState:
    name: str                                   # "long_var_short_lighter" or "short_var_long_lighter"
    cross_spread_pct: Decimal | None            # raw cross-venue spread %
    adjusted_pct: Decimal | None                # cross - book_spread_baseline
    median_5m_pct: float | None
    median_30m_pct: float | None
    median_1h_pct: float | None
    is_green: bool


@dataclass(frozen=True, slots=True)
class SignalState:
    ts_monotonic: float
    asset: str | None
    var_bid: Decimal | None
    var_ask: Decimal | None
    lighter_bid: Decimal | None
    lighter_ask: Decimal | None
    var_book_spread_pct: Decimal | None
    lighter_book_spread_pct: Decimal | None
    book_spread_baseline_pct: Decimal | None
    long_direction: DirectionState
    short_direction: DirectionState


EdgeCallback = Callable[[str, SignalState], Awaitable[None]]


@dataclass(slots=True)
class SignalEngine:
    strict: bool = False
    _history: deque[tuple[float, float | None, float | None]] = field(default_factory=deque)
    _state: SignalState | None = None
    _prev_long_green: bool = False
    _prev_short_green: bool = False
    _edge_subscribers: list[EdgeCallback] = field(default_factory=list)

    def subscribe_edge(self, callback: EdgeCallback) -> None:
        self._edge_subscribers.append(callback)

    def get_state(self) -> SignalState | None:
        return self._state

    def record(
        self,
        *,
        asset: str | None,
        var_bid: Decimal | None,
        var_ask: Decimal | None,
        lighter_bid: Decimal | None,
        lighter_ask: Decimal | None,
    ) -> SignalState:
        now = time.monotonic()
        var_book_spread_pct = _book_spread_percent(var_bid, var_ask)
        lighter_book_spread_pct = _book_spread_percent(lighter_bid, lighter_ask)
        baseline: Decimal | None = None
        if var_book_spread_pct is not None and lighter_book_spread_pct is not None:
            baseline = (var_book_spread_pct + lighter_book_spread_pct) / Decimal("2")

        long_cross_pct = _spread_percent(_spread_value(var_ask, lighter_bid), var_ask)
        short_cross_pct = _spread_percent(_spread_value(lighter_ask, var_bid), lighter_ask)

        self._history.append((now, _to_float(long_cross_pct), _to_float(short_cross_pct)))
        cutoff = now - SPREAD_HISTORY_SECONDS
        while self._history and self._history[0][0] < cutoff:
            self._history.popleft()

        long_state = self._build_direction(
            name="long_var_short_lighter",
            cross_pct=long_cross_pct,
            baseline=baseline,
            long_side=True,
            now=now,
        )
        short_state = self._build_direction(
            name="short_var_long_lighter",
            cross_pct=short_cross_pct,
            baseline=baseline,
            long_side=False,
            now=now,
        )

        self._state = SignalState(
            ts_monotonic=now,
            asset=asset,
            var_bid=var_bid,
            var_ask=var_ask,
            lighter_bid=lighter_bid,
            lighter_ask=lighter_ask,
            var_book_spread_pct=var_book_spread_pct,
            lighter_book_spread_pct=lighter_book_spread_pct,
            book_spread_baseline_pct=baseline,
            long_direction=long_state,
            short_direction=short_state,
        )
        return self._state

    def detect_edges(self) -> list[str]:
        """Return list of directions that just flipped to green this tick."""
        if self._state is None:
            return []
        edges: list[str] = []
        now_long = self._state.long_direction.is_green
        now_short = self._state.short_direction.is_green
        if now_long and not self._prev_long_green:
            edges.append("long_var_short_lighter")
        if now_short and not self._prev_short_green:
            edges.append("short_var_long_lighter")
        self._prev_long_green = now_long
        self._prev_short_green = now_short
        return edges

    def _build_direction(
        self,
        *,
        name: str,
        cross_pct: Decimal | None,
        baseline: Decimal | None,
        long_side: bool,
        now: float,
    ) -> DirectionState:
        m5 = self._median_window(5 * 60, long_side, now)
        m30 = self._median_window(30 * 60, long_side, now)
        m1h = self._median_window(60 * 60, long_side, now)

        adjusted: Decimal | None = None
        if cross_pct is not None and baseline is not None:
            adjusted = cross_pct - baseline

        is_green = False
        if adjusted is not None:
            thresholds = [v for v in (m5, m30, m1h) if v is not None]
            if thresholds:
                adj_f = float(adjusted)
                if self.strict:
                    is_green = adj_f > max(thresholds)
                else:
                    is_green = any(adj_f > t for t in thresholds)

        return DirectionState(
            name=name,
            cross_spread_pct=cross_pct,
            adjusted_pct=adjusted,
            median_5m_pct=m5,
            median_30m_pct=m30,
            median_1h_pct=m1h,
            is_green=is_green,
        )

    def _median_window(self, window_seconds: float, long_side: bool, now: float) -> float | None:
        cutoff = now - window_seconds
        idx = 1 if long_side else 2
        values = [row[idx] for row in self._history if row[0] >= cutoff and row[idx] is not None]
        if not values:
            return None
        return float(median(values))


def _self_test() -> None:
    eng = SignalEngine(strict=False)

    # Feed 30 quiet ticks at 0.1s apart where cross spread is always 0.01%.
    # baseline ~ 0.01%, so adjusted ~ 0 → not green.
    now0 = time.monotonic()
    for i in range(30):
        eng._history.append((now0 - (30 - i) * 0.1, 0.01, 0.01))

    # Current tick: lighter_bid well above var_ask → wide cross spread.
    s = eng.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.5"),
        lighter_ask=Decimal("100.6"),
    )
    assert s.long_direction.cross_spread_pct is not None
    assert s.long_direction.adjusted_pct is not None
    assert float(s.long_direction.adjusted_pct) > 0, s.long_direction.adjusted_pct
    # With historical median ≈ 0.01 for long_side, large current adjusted should flip green.
    assert s.long_direction.is_green, s.long_direction

    # Detect edge transitions.
    edges = eng.detect_edges()
    assert "long_var_short_lighter" in edges, edges

    # Re-run; still green but no new edge.
    eng.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.5"),
        lighter_ask=Decimal("100.6"),
    )
    edges = eng.detect_edges()
    assert edges == [], edges

    # Strict mode should gate on max(median_5m, median_30m, median_1h).
    eng_strict = SignalEngine(strict=True)
    now0 = time.monotonic()
    # Fill a wide history so max is large.
    for i in range(30):
        eng_strict._history.append((now0 - (30 - i) * 0.1, 5.0, 5.0))
    s = eng_strict.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.2"),
        lighter_ask=Decimal("100.3"),
    )
    assert not s.long_direction.is_green, "strict mode should reject when adjusted < max-median"

    print("SignalEngine OK")


if __name__ == "__main__":
    _self_test()
```

- [ ] **Step 2: Run self-test**

```bash
python -m variational.signal
```

Expected: `SignalEngine OK`

- [ ] **Step 3: Commit**

```bash
git add variational/signal.py
git commit -m "$(cat <<'EOF'
Add SignalEngine with deterministic median-based edge detection

Lifts cross-spread history + median + green-light logic out of the
dashboard into a dedicated module so dashboard and future AutoTrader
share one source of truth. Supports strict vs any-median gating.
Ships with self-test runnable via python -m variational.signal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.3 — Rewire dashboard to read from `SignalEngine`

**Files:**
- Modify: `main.py` (`render_dashboard`, `_record_cross_spreads`, `_median_cross_spread`, and related helpers)

- [ ] **Step 1: Add SignalEngine instance to `VariationalToLighterRuntime.__init__`**

In `main.py`, inside `VariationalToLighterRuntime.__init__` (line ~204), add after initializing the logger:

```python
from variational.signal import SignalEngine  # local import at top of file
...
# In __init__ body, alongside cross_spread_history init:
self.signal_engine = SignalEngine(strict=False)
```

Move the `from variational.signal import SignalEngine` to the top-of-file imports block.

Remove these fields from `__init__`:

```python
self.cross_spread_history: deque[tuple[float, float | None, float | None]] = deque()
```

- [ ] **Step 2: Delete obsolete helpers**

Delete these methods entirely from `VariationalToLighterRuntime` (their logic now lives in SignalEngine):

- `_record_cross_spreads` (line ~886)
- `_median_cross_spread` (line ~903)

- [ ] **Step 3: Refactor `render_dashboard` to consume `SignalState`**

Replace the top of `render_dashboard` (line ~916) with:

```python
async def render_dashboard(self) -> Group:
    var_bid, var_ask, quote_asset = await self.get_variational_best_bid_ask(self.variational_ticker)
    lighter_bid, lighter_ask = await self.get_lighter_best_bid_ask()

    state = self.signal_engine.record(
        asset=quote_asset or self.variational_ticker,
        var_bid=var_bid,
        var_ask=var_ask,
        lighter_bid=lighter_bid,
        lighter_ask=lighter_ask,
    )
    # Edges will be consumed in Task 2.4 for journaling + Task 3.x for AutoTrader.
    self.signal_engine.detect_edges()

    var_book_spread = spread_value(var_bid, var_ask)
    lighter_book_spread = spread_value(lighter_bid, lighter_ask)
    var_book_spread_pct = state.var_book_spread_pct
    lighter_book_spread_pct = state.lighter_book_spread_pct
    spread_color_baseline = state.book_spread_baseline_pct

    long_var_short_lighter_pct = state.long_direction.cross_spread_pct
    short_var_long_lighter_pct = state.short_direction.cross_spread_pct

    long_pct_median_5m = state.long_direction.median_5m_pct
    long_pct_median_30m = state.long_direction.median_30m_pct
    long_pct_median_1h = state.long_direction.median_1h_pct
    short_pct_median_5m = state.short_direction.median_5m_pct
    short_pct_median_30m = state.short_direction.median_30m_pct
    short_pct_median_1h = state.short_direction.median_1h_pct

    async with self._record_lock:
        recent_keys = list(self.record_order)[-DASHBOARD_ORDERS:]
        rows = [self.records[key] for key in reversed(recent_keys) if key in self.records]

    # ... rest of method unchanged below this line
```

Leave everything below that point unchanged (the Rich table composition reads these locals as before).

- [ ] **Step 4: Verify colors still match previous behavior**

Checkout the previous commit in a separate worktree (or git stash this task's changes), run it for ~2 min alongside the new version side by side on the same variational.io session. Colors for both directions in the "价差" / "Spreads" table must match tick-for-tick.

If no side-by-side possible, a lighter spot check: make sure green appears at all in both versions during a noisy market period.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
Dashboard reads from SignalEngine

Moves cross-spread history, median windows and green/red decision
into SignalEngine. Dashboard now consumes SignalState and drops its
inline helpers. Pure refactor; dashboard colors unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2.4 — Wire SignalEngine edges → `signal_events.jsonl`

**Files:**
- Modify: `main.py` (add journal + emit signal edges)

- [ ] **Step 1: Instantiate the signal journal**

In `main.py`, add to the top-of-file imports:

```python
from variational.journal import EventJournal
```

In `VariationalToLighterRuntime.__init__` (around line 230), after `self.trade_records_csv_file = ...`, add:

```python
self.signal_events_file = output_dir / "signal_events.jsonl" if output_dir else LOG_DIR / "signal_events.jsonl"
self.signal_journal = EventJournal(self.signal_events_file)
```

- [ ] **Step 2: Start and stop the journal alongside the runtime**

In `VariationalToLighterRuntime.run()` (line ~1181), right after `await self.runtime.start()`:

```python
await self.signal_journal.start()
```

In `VariationalToLighterRuntime.close()` (line ~1208), after the existing cancels:

```python
await self.signal_journal.stop()
```

- [ ] **Step 3: Emit an event on every edge**

Replace the `self.signal_engine.detect_edges()` line inside `render_dashboard` with an emit loop:

```python
for direction in self.signal_engine.detect_edges():
    self._emit_signal_edge(direction, state, event="signal_turned_green")
# Also track red edges (transition green → red). Extend SignalEngine briefly:
# (see Step 3a below for the engine-side change)
```

- [ ] **Step 3a: Extend `SignalEngine.detect_edges` to return both directions**

In `variational/signal.py`, replace `detect_edges` with:

```python
def detect_edges(self) -> list[tuple[str, str]]:
    """Return list of (direction, event) pairs that flipped this tick.

    event ∈ {"signal_turned_green", "signal_turned_red"}.
    """
    if self._state is None:
        return []
    out: list[tuple[str, str]] = []
    now_long = self._state.long_direction.is_green
    now_short = self._state.short_direction.is_green
    if now_long != self._prev_long_green:
        out.append(("long_var_short_lighter", "signal_turned_green" if now_long else "signal_turned_red"))
    if now_short != self._prev_short_green:
        out.append(("short_var_long_lighter", "signal_turned_green" if now_short else "signal_turned_red"))
    self._prev_long_green = now_long
    self._prev_short_green = now_short
    return out
```

Rerun self-test:

```bash
python -m variational.signal
```

The existing test uses `"long_var_short_lighter" in edges` which must continue to work — update the test to use a tuple check. Update the two relevant lines in `_self_test`:

```python
edges = eng.detect_edges()
assert ("long_var_short_lighter", "signal_turned_green") in edges, edges
...
edges = eng.detect_edges()
assert edges == [], edges
```

- [ ] **Step 3b: Use tuple in `main.py` loop**

```python
for direction, event in self.signal_engine.detect_edges():
    self._emit_signal_edge(direction, state, event=event)
```

- [ ] **Step 4: Implement `_emit_signal_edge`**

Add to `VariationalToLighterRuntime` (anywhere convenient, e.g., right above `render_dashboard`):

```python
def _emit_signal_edge(self, direction: str, state, event: str) -> None:
    direction_state = state.long_direction if direction == "long_var_short_lighter" else state.short_direction
    self.signal_journal.emit(
        {
            "ts": utc_now(),
            "event": event,
            "direction": direction,
            "asset": state.asset,
            "adjusted_pct": decimal_to_str(direction_state.adjusted_pct),
            "cross_spread_pct": decimal_to_str(direction_state.cross_spread_pct),
            "book_spread_baseline_pct": decimal_to_str(state.book_spread_baseline_pct),
            "median_5m_pct": direction_state.median_5m_pct,
            "median_30m_pct": direction_state.median_30m_pct,
            "median_1h_pct": direction_state.median_1h_pct,
            "quotes": {
                "var_bid": decimal_to_str(state.var_bid),
                "var_ask": decimal_to_str(state.var_ask),
                "lighter_bid": decimal_to_str(state.lighter_bid),
                "lighter_ask": decimal_to_str(state.lighter_ask),
            },
            # triggered_cycle_id / skip_reason get filled by AutoTrader in M3;
            # for M2 they default to null.
            "triggered_cycle_id": None,
            "skip_reason": None,
        }
    )
```

- [ ] **Step 5: Live verify**

Run `python main.py --no-hedge` and wait for a green/red flip (may take several minutes during volatile markets). Then:

```bash
tail -n 5 log/signal_events.jsonl
```

Expected: JSON lines, one per flip, with `event` ∈ {`signal_turned_green`, `signal_turned_red`}.

- [ ] **Step 6: Commit**

```bash
git add main.py variational/signal.py
git commit -m "$(cat <<'EOF'
Log signal edges to signal_events.jsonl via EventJournal

Every green↔red transition produces a full-context JSONL record
containing the direction, adjusted/baseline/medians, and the four
bid/ask quotes. Writing is off the main path via EventJournal.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Milestone 3 — AutoTrader + cycle_pnl.jsonl

Goal: signal-driven, parallel two-leg order placement with complete per-cycle attribution and a failure breaker.

### Task 3.1 — AutoTrader types and config

**Files:**
- Create: `variational/auto_trader.py`

- [ ] **Step 1: Write the types-only skeleton (everything else in later tasks)**

Create `variational/auto_trader.py`:

```python
"""AutoTrader: signal-driven cross-venue trade orchestration.

Each green-edge event from SignalEngine becomes a TradeCycle: two legs
(Variational via CmdClient, Lighter via SignerClient) fire in parallel.
Fills stream back asynchronously. At settlement, the cycle computes
realized vs expected P&L attribution and writes one line to cycle_pnl.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from variational.journal import EventJournal
from variational.signal import SignalEngine, SignalState


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dec_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


@dataclass(slots=True)
class AutoTraderConfig:
    qty: Decimal
    throttle_seconds: float = 3.0
    max_trades_per_day: int = 200
    var_fee_bps: float = 0.0
    lighter_fee_bps: float = 2.0
    max_net_imbalance: Decimal = Decimal("0")      # 0 → derive as 2*qty at wire time
    leg_settle_timeout_sec: float = 10.0
    var_order_timeout_ms: int = 5000
    hedge_slippage_bps: float = 100.0
    breaker_consecutive_threshold: int = 3
    breaker_daily_threshold: int = 5


@dataclass(slots=True)
class TradePlan:
    qty_target: Decimal
    expected_var_fill_px: Decimal | None
    expected_lighter_fill_px: Decimal | None
    expected_gross_pct: float | None
    expected_net_pct: float | None


@dataclass(slots=True)
class LegState:
    placed_at: str | None = None
    filled_at: str | None = None
    requested_qty: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    avg_fill_px: Decimal | None = None
    partial_fill_count: int = 0
    error: str | None = None
    terminal: bool = False


@dataclass(slots=True)
class VarLegState(LegState):
    api_latency_ms: int | None = None
    trade_ids: list[str] = field(default_factory=list)
    request_id: str | None = None


@dataclass(slots=True)
class LighterLegState(LegState):
    client_order_id: int | None = None
    limit_px: Decimal | None = None
    tx_hash: str | None = None


@dataclass(slots=True)
class TradeCycle:
    cycle_id: str
    direction: str
    asset: str
    opened_at: str
    closed_at: str | None = None
    signal_snapshot: SignalState | None = None
    plan: TradePlan | None = None
    var_leg: VarLegState = field(default_factory=VarLegState)
    lighter_leg: LighterLegState = field(default_factory=LighterLegState)
    status: str = "opening"                        # opening|settling|closed|failed
    quote_drift_ms: int | None = None              # signal→var request-sent delta
    reason_codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TraderStats:
    trades_today: int = 0
    failures_today: int = 0
    consecutive_failures: int = 0
    cumulative_realized_net_notional: Decimal = Decimal("0")
    avg_var_slippage_bps: float = 0.0
    avg_lighter_slippage_bps: float = 0.0
    frozen: bool = False
    frozen_reason: str | None = None
    _day_key: str = ""


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _new_cycle_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"cyc-{ts}-{uuid.uuid4().hex[:8]}"
```

- [ ] **Step 2: Import smoke**

```bash
python -c "from variational.auto_trader import AutoTraderConfig, TradeCycle, TraderStats; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add variational/auto_trader.py
git commit -m "$(cat <<'EOF'
Scaffold AutoTrader types and config

Dataclasses for AutoTraderConfig, TradePlan, per-leg state,
TradeCycle, and TraderStats. No behavior yet.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.2 — AutoTrader core: gates + parallel fire

Implement the class with pre-fire gates and parallel dispatch. Fill / settlement / attribution come in 3.3–3.5.

**Files:**
- Modify: `variational/auto_trader.py` (append)

- [ ] **Step 1: Define the adapter protocols**

Append to `variational/auto_trader.py`:

```python
from typing import Awaitable, Callable, Protocol


class LighterAdapter(Protocol):
    async def best_bid_ask(self) -> tuple[Decimal | None, Decimal | None]: ...
    async def place_order(
        self,
        side: str,                 # "BUY" | "SELL"
        qty: Decimal,
        limit_px: Decimal,
    ) -> "LighterPlaceResult": ...


@dataclass(slots=True)
class LighterPlaceResult:
    ok: bool
    client_order_id: int | None = None
    tx_hash: str | None = None
    error: str | None = None


class VariationalPlacer(Protocol):
    async def place_order(
        self,
        side: str,                 # "buy" | "sell" in Variational casing
        qty: Decimal,
        asset: str,
        timeout_ms: int,
    ) -> "VarPlaceResult": ...


@dataclass(slots=True)
class VarPlaceResult:
    ok: bool
    trade_id: str | None = None
    request_id: str | None = None
    raw_status: int | None = None
    raw_body: str | None = None
    latency_ms: int | None = None
    error: str | None = None
```

- [ ] **Step 2: Add the AutoTrader class skeleton with gates**

Append:

```python
class AutoTrader:
    def __init__(
        self,
        *,
        signal: SignalEngine,
        var_placer: VariationalPlacer,
        lighter: LighterAdapter,
        config: AutoTraderConfig,
        events_journal: EventJournal,
        cycles_journal: EventJournal,
        logger: logging.Logger,
    ) -> None:
        self.signal = signal
        self.var_placer = var_placer
        self.lighter = lighter
        self.config = config
        self.events = events_journal
        self.cycles = cycles_journal
        self.logger = logger

        self.stats = TraderStats(_day_key=_today_key())
        self._last_fire_monotonic: dict[str, float] = {}
        self._open_cycles: dict[str, TradeCycle] = {}
        self._lighter_order_to_cycle: dict[int, str] = {}
        self._lock = asyncio.Lock()

        if self.config.max_net_imbalance == 0:
            self.config.max_net_imbalance = self.config.qty * Decimal("2")

    # ---------- public API ----------

    async def on_green_edge(self, direction: str, state: SignalState) -> None:
        reason = self._gate_check(direction, state)
        if reason is not None:
            self.events.emit({
                "ts": _utc_now_iso(),
                "event": "signal_decision",
                "direction": direction,
                "decision": "skip",
                "reason": reason,
            })
            return
        await self._fire(direction, state)

    def snapshot(self) -> TraderStats:
        self._maybe_rollover()
        return self.stats

    # ---------- gates ----------

    def _gate_check(self, direction: str, state: SignalState) -> str | None:
        self._maybe_rollover()
        if self.stats.frozen:
            return "frozen"
        last = self._last_fire_monotonic.get(direction)
        import time
        now_mono = time.monotonic()
        if last is not None and (now_mono - last) < self.config.throttle_seconds:
            return "throttled"
        if self.stats.trades_today >= self.config.max_trades_per_day:
            return "day_limit"

        # Re-check direction symbol: signal must still be positive at fire time.
        ds = state.long_direction if direction == "long_var_short_lighter" else state.short_direction
        if ds.adjusted_pct is None or float(ds.adjusted_pct) <= 0:
            return "signal_flipped"
        return None

    def _maybe_rollover(self) -> None:
        today = _today_key()
        if self.stats._day_key != today:
            self.stats._day_key = today
            self.stats.trades_today = 0
            self.stats.failures_today = 0
            # Consecutive failures and frozen state do NOT reset on day rollover — user must restart.

    # ---------- fire ----------

    async def _fire(self, direction: str, state: SignalState) -> None:
        # Details in Task 3.3.
        raise NotImplementedError
```

- [ ] **Step 3: Import smoke**

```bash
python -c "from variational.auto_trader import AutoTrader, LighterAdapter, VariationalPlacer; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add variational/auto_trader.py
git commit -m "$(cat <<'EOF'
AutoTrader skeleton with gate checks

Gate chain (frozen → throttle → day_limit → signal_flipped) and
UTC day rollover. Fire path stubbed; implementation in next task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.3 — AutoTrader: parallel fire + cycle bookkeeping

**Files:**
- Modify: `variational/auto_trader.py` (replace `_fire` stub)

- [ ] **Step 1: Implement `_fire` and its helpers**

Replace the `_fire` NotImplementedError with:

```python
    async def _fire(self, direction: str, state: SignalState) -> None:
        import time
        cycle_id = _new_cycle_id()
        now_iso = _utc_now_iso()
        now_mono = time.monotonic()

        ds = state.long_direction if direction == "long_var_short_lighter" else state.short_direction
        # Plan — expected fills based on current quotes at signal time.
        if direction == "long_var_short_lighter":
            expected_var_fill = state.var_ask
            expected_lighter_fill = state.lighter_bid
            var_side = "buy"
            lighter_side = "SELL"
        else:
            expected_var_fill = state.var_bid
            expected_lighter_fill = state.lighter_ask
            var_side = "sell"
            lighter_side = "BUY"

        expected_gross_pct = float(ds.cross_spread_pct) if ds.cross_spread_pct is not None else None
        total_fee_bps = self.config.var_fee_bps + self.config.lighter_fee_bps
        expected_net_pct = None
        if expected_gross_pct is not None:
            expected_net_pct = expected_gross_pct - (total_fee_bps / 100.0)  # bps → pct

        plan = TradePlan(
            qty_target=self.config.qty,
            expected_var_fill_px=expected_var_fill,
            expected_lighter_fill_px=expected_lighter_fill,
            expected_gross_pct=expected_gross_pct,
            expected_net_pct=expected_net_pct,
        )

        cycle = TradeCycle(
            cycle_id=cycle_id,
            direction=direction,
            asset=state.asset or "UNKNOWN",
            opened_at=now_iso,
            signal_snapshot=state,
            plan=plan,
        )
        cycle.var_leg.requested_qty = self.config.qty
        cycle.lighter_leg.requested_qty = self.config.qty

        async with self._lock:
            self._open_cycles[cycle_id] = cycle
            self._last_fire_monotonic[direction] = now_mono
            self.stats.trades_today += 1

        self.events.emit({
            "ts": now_iso, "event": "cycle_opened", "cycle_id": cycle_id,
            "direction": direction, "asset": cycle.asset,
            "qty_target": _dec_str(self.config.qty),
            "plan": {
                "expected_var_fill_px": _dec_str(expected_var_fill),
                "expected_lighter_fill_px": _dec_str(expected_lighter_fill),
                "expected_gross_pct": expected_gross_pct,
                "expected_net_pct": expected_net_pct,
            },
        })

        # Compute Lighter limit price with slippage buffer.
        lighter_bid, lighter_ask = await self.lighter.best_bid_ask()
        if lighter_bid is None or lighter_ask is None:
            cycle.lighter_leg.error = "lighter_book_empty"
            cycle.lighter_leg.terminal = True
        slippage = Decimal(str(self.config.hedge_slippage_bps)) / Decimal("10000")
        if lighter_side == "BUY":
            limit_px = (lighter_ask * (Decimal("1") + slippage)) if lighter_ask else None
        else:
            limit_px = (lighter_bid * (Decimal("1") - slippage)) if lighter_bid else None
        cycle.lighter_leg.limit_px = limit_px

        # Fire both legs in parallel. return_exceptions so one leg's failure doesn't cancel the other.
        var_task = asyncio.create_task(self._fire_var_leg(cycle, var_side))
        lighter_task = asyncio.create_task(self._fire_lighter_leg(cycle, lighter_side))
        await asyncio.gather(var_task, lighter_task, return_exceptions=True)

        # Settlement loop lives in another task to not block this dispatch.
        asyncio.create_task(self._settle_cycle_when_done(cycle))

    async def _fire_var_leg(self, cycle: TradeCycle, side: str) -> None:
        import time
        t0 = time.monotonic()
        cycle.var_leg.placed_at = _utc_now_iso()
        self.events.emit({
            "ts": cycle.var_leg.placed_at, "event": "var_place_attempt",
            "cycle_id": cycle.cycle_id, "side": side,
            "qty": _dec_str(cycle.var_leg.requested_qty),
        })
        try:
            res = await self.var_placer.place_order(
                side=side,
                qty=cycle.var_leg.requested_qty,
                asset=cycle.asset,
                timeout_ms=self.config.var_order_timeout_ms,
            )
            cycle.var_leg.api_latency_ms = res.latency_ms
            cycle.var_leg.request_id = res.request_id
            cycle.quote_drift_ms = int((time.monotonic() - t0) * 1000)
            self.events.emit({
                "ts": _utc_now_iso(), "event": "var_place_ack",
                "cycle_id": cycle.cycle_id, "ok": res.ok,
                "status": res.raw_status, "latency_ms": res.latency_ms,
                "error": res.error,
            })
            if not res.ok:
                cycle.var_leg.error = res.error or f"http_{res.raw_status}"
                cycle.var_leg.terminal = True
                self._register_failure(f"var_http_{res.raw_status}" if res.raw_status else "var_error")
                cycle.reason_codes.append(cycle.var_leg.error)
            else:
                if res.trade_id:
                    cycle.var_leg.trade_ids.append(res.trade_id)
        except Exception as exc:
            cycle.var_leg.error = f"exception: {exc}"
            cycle.var_leg.terminal = True
            self._register_failure("var_exception")
            cycle.reason_codes.append(cycle.var_leg.error)
            self.events.emit({
                "ts": _utc_now_iso(), "event": "cycle_error",
                "cycle_id": cycle.cycle_id, "side": "var", "error_msg": str(exc),
            })

    async def _fire_lighter_leg(self, cycle: TradeCycle, side: str) -> None:
        if cycle.lighter_leg.limit_px is None:
            cycle.lighter_leg.error = "no_limit_px"
            cycle.lighter_leg.terminal = True
            self._register_failure("lighter_book_empty")
            cycle.reason_codes.append("lighter_book_empty")
            return
        cycle.lighter_leg.placed_at = _utc_now_iso()
        self.events.emit({
            "ts": cycle.lighter_leg.placed_at, "event": "lighter_place_attempt",
            "cycle_id": cycle.cycle_id, "side": side,
            "qty": _dec_str(cycle.lighter_leg.requested_qty),
            "limit_px": _dec_str(cycle.lighter_leg.limit_px),
        })
        try:
            res = await self.lighter.place_order(
                side=side,
                qty=cycle.lighter_leg.requested_qty,
                limit_px=cycle.lighter_leg.limit_px,
            )
            self.events.emit({
                "ts": _utc_now_iso(), "event": "lighter_place_ack",
                "cycle_id": cycle.cycle_id, "ok": res.ok,
                "client_order_id": res.client_order_id,
                "tx_hash": res.tx_hash, "error": res.error,
            })
            if not res.ok:
                cycle.lighter_leg.error = res.error or "unknown"
                cycle.lighter_leg.terminal = True
                self._register_failure("lighter_sign_error")
                cycle.reason_codes.append(cycle.lighter_leg.error)
                return
            cycle.lighter_leg.client_order_id = res.client_order_id
            cycle.lighter_leg.tx_hash = res.tx_hash
            if res.client_order_id is not None:
                async with self._lock:
                    self._lighter_order_to_cycle[res.client_order_id] = cycle.cycle_id
        except Exception as exc:
            cycle.lighter_leg.error = f"exception: {exc}"
            cycle.lighter_leg.terminal = True
            self._register_failure("lighter_exception")
            cycle.reason_codes.append(cycle.lighter_leg.error)
            self.events.emit({
                "ts": _utc_now_iso(), "event": "cycle_error",
                "cycle_id": cycle.cycle_id, "side": "lighter", "error_msg": str(exc),
            })

    def _register_failure(self, reason: str) -> None:
        self.stats.consecutive_failures += 1
        self.stats.failures_today += 1
        if (
            self.stats.consecutive_failures >= self.config.breaker_consecutive_threshold
            or self.stats.failures_today >= self.config.breaker_daily_threshold
        ):
            self._trip_breaker(reason)

    def _trip_breaker(self, reason: str) -> None:
        if self.stats.frozen:
            return
        self.stats.frozen = True
        self.stats.frozen_reason = reason
        self.events.emit({
            "ts": _utc_now_iso(), "event": "breaker_tripped",
            "reason": reason,
            "consecutive_failures": self.stats.consecutive_failures,
            "daily_failures": self.stats.failures_today,
        })
        self.logger.warning("Breaker tripped: %s (consecutive=%d daily=%d)",
                            reason, self.stats.consecutive_failures, self.stats.failures_today)

    async def _settle_cycle_when_done(self, cycle: TradeCycle) -> None:
        # Body in Task 3.4.
        raise NotImplementedError
```

- [ ] **Step 2: Import smoke**

```bash
python -c "import variational.auto_trader as a; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add variational/auto_trader.py
git commit -m "$(cat <<'EOF'
AutoTrader parallel fire + breaker

Both legs dispatched via asyncio.gather with return_exceptions.
Events journaled at every state transition (cycle_opened,
*_place_attempt/_place_ack, cycle_error, breaker_tripped).
Settlement body still stubbed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.4 — Settlement + attribution

**Files:**
- Modify: `variational/auto_trader.py` (replace settlement stub)

- [ ] **Step 1: Implement cycle settlement and fill routing**

Replace `_settle_cycle_when_done` with:

```python
    async def on_variational_fill(self, trade_id: str, fill_px: Decimal, fill_qty: Decimal) -> None:
        """Route a Variational fill event (from monitor) to an open cycle.

        We match by trade_id list stored on the var_leg when the API returns.
        If no match, emit but ignore (could be a pre-AutoTrader trade).
        """
        async with self._lock:
            for cycle in self._open_cycles.values():
                if trade_id in cycle.var_leg.trade_ids:
                    target = cycle
                    break
            else:
                return
        self._apply_var_fill(target, fill_px, fill_qty)

    async def on_lighter_fill(self, client_order_id: int, fill_px: Decimal, fill_qty: Decimal) -> None:
        async with self._lock:
            cycle_id = self._lighter_order_to_cycle.get(client_order_id)
            cycle = self._open_cycles.get(cycle_id) if cycle_id else None
        if cycle is None:
            return
        self._apply_lighter_fill(cycle, fill_px, fill_qty)

    def _apply_var_fill(self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal) -> None:
        leg = cycle.var_leg
        leg.filled_qty += fill_qty
        leg.avg_fill_px = (
            fill_px if leg.avg_fill_px is None
            else ((leg.avg_fill_px * (leg.filled_qty - fill_qty)) + (fill_px * fill_qty)) / leg.filled_qty
        )
        leg.partial_fill_count += 1
        leg.filled_at = _utc_now_iso()
        self.events.emit({
            "ts": leg.filled_at, "event": "var_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })
        if leg.filled_qty >= leg.requested_qty:
            leg.terminal = True
            self._maybe_reset_consecutive_on_success()

    def _apply_lighter_fill(self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal) -> None:
        leg = cycle.lighter_leg
        leg.filled_qty += fill_qty
        leg.avg_fill_px = (
            fill_px if leg.avg_fill_px is None
            else ((leg.avg_fill_px * (leg.filled_qty - fill_qty)) + (fill_px * fill_qty)) / leg.filled_qty
        )
        leg.partial_fill_count += 1
        leg.filled_at = _utc_now_iso()
        self.events.emit({
            "ts": leg.filled_at, "event": "lighter_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })
        if leg.filled_qty >= leg.requested_qty:
            leg.terminal = True
            self._maybe_reset_consecutive_on_success()

    def _maybe_reset_consecutive_on_success(self) -> None:
        # Only reset consecutive on full success of *both* legs; handled in settle.
        pass

    async def _settle_cycle_when_done(self, cycle: TradeCycle) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.leg_settle_timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if cycle.var_leg.terminal and cycle.lighter_leg.terminal:
                break
            await asyncio.sleep(0.2)
        # Decide final status.
        both_terminal = cycle.var_leg.terminal and cycle.lighter_leg.terminal
        var_ok = cycle.var_leg.filled_qty >= cycle.var_leg.requested_qty
        lighter_ok = cycle.lighter_leg.filled_qty >= cycle.lighter_leg.requested_qty

        if var_ok and lighter_ok:
            cycle.status = "fully_filled"
            self.stats.consecutive_failures = 0
        elif not cycle.var_leg.error and not cycle.lighter_leg.error and (cycle.var_leg.filled_qty > 0 or cycle.lighter_leg.filled_qty > 0):
            cycle.status = "partial"
            if not var_ok:
                cycle.reason_codes.append("var_depth_shortfall" if cycle.var_leg.filled_qty > 0 else "var_timeout")
            if not lighter_ok:
                cycle.reason_codes.append("lighter_depth_shortfall" if cycle.lighter_leg.filled_qty > 0 else "lighter_timeout")
        else:
            var_failed = not var_ok
            lighter_failed = not lighter_ok
            if var_failed and lighter_failed:
                cycle.status = "both_failed"
            else:
                cycle.status = "one_leg_failed"

        cycle.closed_at = _utc_now_iso()
        async with self._lock:
            self._open_cycles.pop(cycle.cycle_id, None)
            if cycle.lighter_leg.client_order_id is not None:
                self._lighter_order_to_cycle.pop(cycle.lighter_leg.client_order_id, None)

        attribution = self._compute_attribution(cycle)
        cycles_row = self._cycle_to_row(cycle, attribution)
        self.cycles.emit(cycles_row)
        self.events.emit({
            "ts": cycle.closed_at, "event": "cycle_closed",
            "cycle_id": cycle.cycle_id, "status": cycle.status,
        })

        # Update rolling stats (moving averages).
        self._update_stats_on_close(cycle, attribution)

    def _compute_attribution(self, cycle: TradeCycle) -> dict[str, Any]:
        plan = cycle.plan or TradePlan(
            qty_target=cycle.var_leg.requested_qty,
            expected_var_fill_px=None,
            expected_lighter_fill_px=None,
            expected_gross_pct=None,
            expected_net_pct=None,
        )
        var_avg = cycle.var_leg.avg_fill_px
        lig_avg = cycle.lighter_leg.avg_fill_px

        realized_net_pct: float | None = None
        var_slip_pct: float | None = None
        lig_slip_pct: float | None = None
        fee_pct = (self.config.var_fee_bps + self.config.lighter_fee_bps) / 100.0

        if cycle.direction == "long_var_short_lighter" and var_avg is not None and lig_avg is not None and var_avg != 0:
            realized_gross = float((lig_avg - var_avg) / var_avg) * 100.0
            realized_net_pct = realized_gross - fee_pct
        elif cycle.direction == "short_var_long_lighter" and var_avg is not None and lig_avg is not None and lig_avg != 0:
            realized_gross = float((var_avg - lig_avg) / lig_avg) * 100.0
            realized_net_pct = realized_gross - fee_pct

        if plan.expected_var_fill_px is not None and var_avg is not None and plan.expected_var_fill_px != 0:
            # Sign convention: positive = adverse.
            if cycle.direction == "long_var_short_lighter":
                var_slip_pct = float((var_avg - plan.expected_var_fill_px) / plan.expected_var_fill_px) * 100.0
            else:
                var_slip_pct = float((plan.expected_var_fill_px - var_avg) / plan.expected_var_fill_px) * 100.0
        if plan.expected_lighter_fill_px is not None and lig_avg is not None and plan.expected_lighter_fill_px != 0:
            if cycle.direction == "long_var_short_lighter":
                lig_slip_pct = float((plan.expected_lighter_fill_px - lig_avg) / plan.expected_lighter_fill_px) * 100.0
            else:
                lig_slip_pct = float((lig_avg - plan.expected_lighter_fill_px) / plan.expected_lighter_fill_px) * 100.0

        qty_t = cycle.var_leg.requested_qty
        fill_ratio = min(
            float(cycle.var_leg.filled_qty / qty_t) if qty_t else 0.0,
            float(cycle.lighter_leg.filled_qty / qty_t) if qty_t else 0.0,
        )

        vs_expected = None
        if realized_net_pct is not None and plan.expected_net_pct is not None:
            vs_expected = realized_net_pct - plan.expected_net_pct

        return {
            "realized_net_pct": realized_net_pct,
            "vs_expected_pct_delta": vs_expected,
            "components": {
                "var_slippage_pct": var_slip_pct,
                "lighter_slippage_pct": lig_slip_pct,
                "fee_pct": fee_pct,
                "fill_ratio": fill_ratio,
                "quote_drift_ms": cycle.quote_drift_ms,
                "reason_codes": list(cycle.reason_codes),
            },
        }

    def _cycle_to_row(self, cycle: TradeCycle, attribution: dict[str, Any]) -> dict[str, Any]:
        s = cycle.signal_snapshot
        plan = cycle.plan or TradePlan(cycle.var_leg.requested_qty, None, None, None, None)
        duration_ms = None
        if cycle.opened_at and cycle.closed_at:
            try:
                t0 = datetime.fromisoformat(cycle.opened_at.replace("Z", "+00:00"))
                t1 = datetime.fromisoformat(cycle.closed_at.replace("Z", "+00:00"))
                duration_ms = int((t1 - t0).total_seconds() * 1000)
            except Exception:
                duration_ms = None
        return {
            "cycle_id": cycle.cycle_id,
            "triggered_at": cycle.opened_at,
            "closed_at": cycle.closed_at,
            "duration_ms": duration_ms,
            "asset": cycle.asset,
            "direction": cycle.direction,
            "status": cycle.status,
            "signal": {
                "adjusted_pct": _dec_str(
                    (s.long_direction.adjusted_pct if cycle.direction == "long_var_short_lighter"
                     else s.short_direction.adjusted_pct) if s else None
                ),
                "baseline_pct": _dec_str(s.book_spread_baseline_pct) if s else None,
                "median_5m_pct": (s.long_direction.median_5m_pct if s and cycle.direction == "long_var_short_lighter" else
                                  s.short_direction.median_5m_pct if s else None),
                "median_30m_pct": (s.long_direction.median_30m_pct if s and cycle.direction == "long_var_short_lighter" else
                                   s.short_direction.median_30m_pct if s else None),
                "median_1h_pct": (s.long_direction.median_1h_pct if s and cycle.direction == "long_var_short_lighter" else
                                  s.short_direction.median_1h_pct if s else None),
                "var_bid": _dec_str(s.var_bid) if s else None,
                "var_ask": _dec_str(s.var_ask) if s else None,
                "lighter_bid": _dec_str(s.lighter_bid) if s else None,
                "lighter_ask": _dec_str(s.lighter_ask) if s else None,
            },
            "plan": {
                "qty_target": _dec_str(plan.qty_target),
                "expected_var_fill_px": _dec_str(plan.expected_var_fill_px),
                "expected_lighter_fill_px": _dec_str(plan.expected_lighter_fill_px),
                "expected_gross_pct": plan.expected_gross_pct,
                "expected_net_pct": plan.expected_net_pct,
            },
            "var_leg": {
                "placed_at": cycle.var_leg.placed_at,
                "filled_at": cycle.var_leg.filled_at,
                "requested_qty": _dec_str(cycle.var_leg.requested_qty),
                "filled_qty": _dec_str(cycle.var_leg.filled_qty),
                "avg_fill_px": _dec_str(cycle.var_leg.avg_fill_px),
                "api_latency_ms": cycle.var_leg.api_latency_ms,
                "partial_fill_count": cycle.var_leg.partial_fill_count,
                "error": cycle.var_leg.error,
            },
            "lighter_leg": {
                "placed_at": cycle.lighter_leg.placed_at,
                "filled_at": cycle.lighter_leg.filled_at,
                "client_order_id": cycle.lighter_leg.client_order_id,
                "requested_qty": _dec_str(cycle.lighter_leg.requested_qty),
                "filled_qty": _dec_str(cycle.lighter_leg.filled_qty),
                "avg_fill_px": _dec_str(cycle.lighter_leg.avg_fill_px),
                "limit_px": _dec_str(cycle.lighter_leg.limit_px),
                "tx_hash": cycle.lighter_leg.tx_hash,
                "error": cycle.lighter_leg.error,
            },
            "attribution": attribution,
        }

    def _update_stats_on_close(self, cycle: TradeCycle, attribution: dict[str, Any]) -> None:
        if cycle.status == "fully_filled":
            rn = attribution.get("realized_net_pct")
            if rn is not None:
                # accumulate as "net pct of notional" × qty × price; notional ≈ qty * var_avg
                notional: Decimal = Decimal("0")
                if cycle.var_leg.avg_fill_px is not None:
                    notional = cycle.var_leg.filled_qty * cycle.var_leg.avg_fill_px
                self.stats.cumulative_realized_net_notional += notional * Decimal(str(rn)) / Decimal("100")

        comps = attribution.get("components", {})
        alpha = 0.1
        if comps.get("var_slippage_pct") is not None:
            self.stats.avg_var_slippage_bps = (
                (1 - alpha) * self.stats.avg_var_slippage_bps + alpha * float(comps["var_slippage_pct"]) * 100.0
            )
        if comps.get("lighter_slippage_pct") is not None:
            self.stats.avg_lighter_slippage_bps = (
                (1 - alpha) * self.stats.avg_lighter_slippage_bps + alpha * float(comps["lighter_slippage_pct"]) * 100.0
            )
```

- [ ] **Step 2: Import smoke**

```bash
python -c "from variational.auto_trader import AutoTrader; print('ok')"
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add variational/auto_trader.py
git commit -m "$(cat <<'EOF'
AutoTrader cycle settlement + attribution

Routes var/lighter fills to open cycles, settles after leg_timeout,
computes realized vs expected net pct and per-leg slippage,
assembles the cycle_pnl row, and writes it via the cycles journal.
Updates a rolling TraderStats snapshot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.5 — Wire AutoTrader adapters (VariationalPlacer, LighterAdapter) and update main.py

**Files:**
- Modify: `main.py` (large: remove passive hedge branch, add adapters, wire AutoTrader, CLI flags, remove `--no-hedge`)

- [ ] **Step 1: Add Variational order-template config**

The Variational order URL / body is unknown until the user supplies it. Read it from `.env` so the user can paste one captured DevTools request without editing code.

At the top of `main.py`:

```python
VAR_ORDER_URL_ENV = "VARIATIONAL_ORDER_URL"
VAR_ORDER_METHOD_ENV = "VARIATIONAL_ORDER_METHOD"      # default POST
VAR_ORDER_BODY_TEMPLATE_ENV = "VARIATIONAL_ORDER_BODY_TEMPLATE"
```

Body template is a Python `str.format`-style string with `{side}`, `{qty}`, `{asset}` placeholders. User pastes the captured body with these three fields replaced by the placeholders.

- [ ] **Step 2: Implement `VariationalPlacerImpl`**

In `main.py`, add (anywhere above `VariationalToLighterRuntime`):

```python
from variational.auto_trader import (
    AutoTrader, AutoTraderConfig, LighterAdapter, LighterPlaceResult,
    VariationalPlacer, VarPlaceResult,
)
from variational.command_client import VariationalCmdClient


class VariationalPlacerImpl:
    def __init__(self, cmd: VariationalCmdClient, url: str, method: str, body_template: str, logger: logging.Logger) -> None:
        self.cmd = cmd
        self.url = url
        self.method = method
        self.body_template = body_template
        self.logger = logger

    async def place_order(self, side: str, qty: Decimal, asset: str, timeout_ms: int) -> VarPlaceResult:
        try:
            body = self.body_template.format(side=side, qty=format(qty, "f"), asset=asset)
        except KeyError as exc:
            return VarPlaceResult(ok=False, error=f"body_template_missing_key: {exc}")
        res = await self.cmd.execute_fetch(
            url=self.url,
            method=self.method,
            headers={"content-type": "application/json"},
            body=body,
            timeout_ms=timeout_ms,
        )
        if not res.ok or (res.status and res.status >= 400):
            return VarPlaceResult(
                ok=False,
                raw_status=res.status, raw_body=res.body,
                latency_ms=res.latency_ms, error=res.error or f"http_{res.status}",
                request_id=res.request_id,
            )
        # trade_id extraction is venue-specific; user fills this in when API shape is known.
        trade_id = None
        try:
            import json as _json
            parsed = _json.loads(res.body or "{}")
            if isinstance(parsed, dict):
                trade_id = str(parsed.get("id") or parsed.get("trade_id") or parsed.get("order_id") or "") or None
        except Exception:
            pass
        return VarPlaceResult(
            ok=True, raw_status=res.status, raw_body=res.body,
            latency_ms=res.latency_ms, trade_id=trade_id, request_id=res.request_id,
        )
```

- [ ] **Step 3: Implement `LighterAdapterImpl` as a thin wrapper**

Extract the existing Lighter order-placement logic in `place_lighter_order` (line ~639) into an adapter:

```python
class LighterAdapterImpl:
    def __init__(self, runtime: "VariationalToLighterRuntime") -> None:
        self.runtime = runtime

    async def best_bid_ask(self):
        return await self.runtime.get_lighter_best_bid_ask()

    async def place_order(self, side: str, qty: Decimal, limit_px: Decimal) -> LighterPlaceResult:
        runtime = self.runtime
        base_amount = int(qty * runtime.base_amount_multiplier)
        if base_amount <= 0:
            return LighterPlaceResult(ok=False, error=f"qty_rounds_to_zero: {qty}")
        price_i = int(limit_px * runtime.price_multiplier)
        is_ask = (side == "SELL")
        client_order_id = int(time.time() * 1000)
        # Dedupe client_order_id
        while client_order_id in runtime.lighter_client_order_to_trade_key or client_order_id in getattr(runtime, "_auto_client_orders", set()):
            client_order_id += 1
        if not hasattr(runtime, "_auto_client_orders"):
            runtime._auto_client_orders = set()
        runtime._auto_client_orders.add(client_order_id)
        try:
            async with runtime._lighter_signer_lock:
                if not runtime.lighter_client:
                    runtime.initialize_lighter_client()
                _, tx_hash, error = await runtime.lighter_client.create_order(
                    market_index=runtime.lighter_market_index,
                    client_order_index=client_order_id,
                    base_amount=base_amount,
                    price=price_i,
                    is_ask=is_ask,
                    order_type=runtime.lighter_client.ORDER_TYPE_LIMIT,
                    time_in_force=runtime.lighter_client.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME,
                    reduce_only=False,
                    trigger_price=0,
                )
            if error is not None:
                return LighterPlaceResult(ok=False, client_order_id=client_order_id, error=f"sign: {error}")
            return LighterPlaceResult(ok=True, client_order_id=client_order_id, tx_hash=tx_hash)
        except Exception as exc:
            return LighterPlaceResult(ok=False, client_order_id=client_order_id, error=f"exception: {exc}")
```

- [ ] **Step 4: Replace passive `place_lighter_order`**

Delete the entire `place_lighter_order` method (line ~639) and the call to it inside `process_variational_trade_event` (line ~781-782: `if created and created_record is not None and self.args.auto_hedge: await self.place_lighter_order(created_record)`).

Replace the trigger inside `process_variational_trade_event` with a dispatch to AutoTrader's fill router: when a Variational fill comes in, notify AutoTrader so it can correlate back to a cycle:

```python
# Inside process_variational_trade_event, after filled_payload is emitted:
if self.auto_trader is not None and filled_payload is not None:
    fill_px = to_decimal(event.get("price"))
    fill_qty = to_decimal(event.get("qty"))
    trade_id = str(event.get("trade_id", "")).strip()
    if fill_px is not None and fill_qty is not None and trade_id:
        await self.auto_trader.on_variational_fill(trade_id, fill_px, fill_qty)
```

Remove from `OrderLifecycle.to_payload` and `OrderLifecycle` init any `auto_hedge_enabled` reference that assumes a `--no-hedge` flag. Keep the field but always set it to `True` for now (historical schema compat); Task 3.7 will clean it up.

- [ ] **Step 5: Hook Lighter fill WS → AutoTrader**

In `handle_lighter_fill_update` (line ~448), add at the bottom:

```python
if self.auto_trader is not None:
    client_order_id_raw = order.get("client_order_id")
    try:
        client_order_id_int = int(client_order_id_raw)
    except Exception:
        client_order_id_int = None
    if client_order_id_int is not None and fill_price is not None:
        filled_base = to_decimal(order.get("filled_base_amount"))
        if filled_base is not None:
            await self.auto_trader.on_lighter_fill(client_order_id_int, fill_price, filled_base)
```

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
AutoTrader adapters + fill routing

VariationalPlacerImpl wraps the CmdClient with a body template from
env. LighterAdapterImpl wraps the existing SignerClient logic.
Variational/Lighter fills now route to AutoTrader.on_*_fill for
cycle correlation. Passive hedge branch removed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.6 — Wire AutoTrader lifecycle into runtime, add CLI, remove `--no-hedge`

**Files:**
- Modify: `main.py` (CLI, `parse_args`, runtime init/run/close)

- [ ] **Step 1: Replace `parse_args`**

Replace the entire `parse_args()` function:

```python
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cross-venue spread trader. Auto-triggers on green-light signal; writes per-cycle P&L attribution."
    )
    parser.add_argument("--qty", required=True, type=str, help="Per-cycle base asset qty (required). Example: 0.01")
    parser.add_argument("--throttle-seconds", type=float, default=3.0)
    parser.add_argument("--max-trades-per-day", type=int, default=200)
    parser.add_argument("--var-fee-bps", type=float, default=0.0)
    parser.add_argument("--lighter-fee-bps", type=float, default=2.0)
    parser.add_argument("--max-net-imbalance", type=str, default="0",
                        help="Position imbalance breaker threshold; 0 means 2x qty.")
    parser.add_argument("--signal-strict", action="store_true",
                        help="Require adjusted > max(5m,30m,1h) instead of any().")
    parser.add_argument("--var-order-timeout-ms", type=int, default=5000)
    parser.add_argument("--leg-settle-timeout-sec", type=float, default=10.0)
    parser.add_argument("--debug-var-payload", action="store_true",
                        help="Write full Variational request/response bodies to order_events.jsonl.")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh")
    return parser.parse_args()
```

- [ ] **Step 2: Add AutoTrader + journals to `VariationalToLighterRuntime.__init__`**

In `__init__`, after setting up `signal_journal`:

```python
self.events_journal = EventJournal(LOG_DIR / "order_events.jsonl")
self.cycles_journal = EventJournal(LOG_DIR / "cycle_pnl.jsonl")
self.auto_trader: AutoTrader | None = None   # built after activate_asset, when market is known
```

And update the `SignalEngine` construction with strict flag:

```python
self.signal_engine = SignalEngine(strict=args.signal_strict)
```

Also, in the top of `__init__` where `self.args` is already stored:

```python
qty_dec = Decimal(args.qty)
imbalance_dec = Decimal(args.max_net_imbalance)
self.auto_trader_config = AutoTraderConfig(
    qty=qty_dec,
    throttle_seconds=args.throttle_seconds,
    max_trades_per_day=args.max_trades_per_day,
    var_fee_bps=args.var_fee_bps,
    lighter_fee_bps=args.lighter_fee_bps,
    max_net_imbalance=imbalance_dec,
    leg_settle_timeout_sec=args.leg_settle_timeout_sec,
    var_order_timeout_ms=args.var_order_timeout_ms,
)
```

- [ ] **Step 3: Create CmdClient and wire AutoTrader in `run()`**

In `VariationalToLighterRuntime.run()`, after `await self.runtime.start()` and after `await self.signal_journal.start()`:

```python
await self.events_journal.start()
await self.cycles_journal.start()

var_order_url = os.getenv(VAR_ORDER_URL_ENV, "").strip()
var_order_method = os.getenv(VAR_ORDER_METHOD_ENV, "POST").strip().upper()
var_order_body_template = os.getenv(VAR_ORDER_BODY_TEMPLATE_ENV, "").strip()
if not var_order_url or not var_order_body_template:
    raise RuntimeError(
        f"{VAR_ORDER_URL_ENV} and {VAR_ORDER_BODY_TEMPLATE_ENV} must be set in .env "
        "with the real Variational order API (captured from browser DevTools)."
    )

self.cmd_client = VariationalCmdClient(FORWARDER_HOST, FORWARDER_COMMAND_PORT, self.logger)
await self.cmd_client.start()
await self.cmd_client.wait_ready(timeout=10)

var_placer = VariationalPlacerImpl(self.cmd_client, var_order_url, var_order_method, var_order_body_template, self.logger)
lighter_adapter = LighterAdapterImpl(self)
self.auto_trader = AutoTrader(
    signal=self.signal_engine,
    var_placer=var_placer,
    lighter=lighter_adapter,
    config=self.auto_trader_config,
    events_journal=self.events_journal,
    cycles_journal=self.cycles_journal,
    logger=self.logger,
)
self.logger.info("AutoTrader initialized: qty=%s throttle=%.1fs max/day=%d fees(var/lighter bps)=%.1f/%.1f",
                 self.auto_trader_config.qty, self.auto_trader_config.throttle_seconds,
                 self.auto_trader_config.max_trades_per_day,
                 self.auto_trader_config.var_fee_bps, self.auto_trader_config.lighter_fee_bps)
```

- [ ] **Step 4: Route signal edges to AutoTrader**

Replace the in-dashboard edge journaling with a unified dispatch. In `render_dashboard`, replace the edge loop with:

```python
for direction, event in self.signal_engine.detect_edges():
    self._emit_signal_edge(direction, state, event=event)
    if event == "signal_turned_green" and self.auto_trader is not None:
        asyncio.create_task(self.auto_trader.on_green_edge(direction, state))
```

- [ ] **Step 5: Shut down journals and CmdClient**

In `VariationalToLighterRuntime.close()`, after the dashboard task is cancelled:

```python
if self.cmd_client is not None:
    await self.cmd_client.stop()
await self.events_journal.stop()
await self.cycles_journal.stop()
await self.signal_journal.stop()
```

Initialize `self.cmd_client = None` in `__init__` so the close path is safe even if `run()` didn't get that far.

- [ ] **Step 6: Echo config at startup**

At the start of `run()`, add:

```python
self.logger.info(
    "Startup config: qty=%s throttle_s=%.1f max_trades/day=%d var_fee_bps=%.1f lighter_fee_bps=%.1f "
    "signal_strict=%s leg_timeout_s=%.1f var_order_timeout_ms=%d lang=%s",
    self.auto_trader_config.qty, self.auto_trader_config.throttle_seconds,
    self.auto_trader_config.max_trades_per_day,
    self.auto_trader_config.var_fee_bps, self.auto_trader_config.lighter_fee_bps,
    self.args.signal_strict, self.auto_trader_config.leg_settle_timeout_sec,
    self.auto_trader_config.var_order_timeout_ms, self.args.lang,
)
```

- [ ] **Step 7: Remove `--no-hedge` references**

Remove the line `parser.add_argument("--no-hedge", ...)` — already removed in Step 1.
Remove `self.args.auto_hedge` references throughout; substitute a hard-coded `True` (since we always auto-trade).

Search for the string `auto_hedge`:

```bash
grep -n "auto_hedge" main.py
```

Every occurrence in `main.py` should either:
- be removed (e.g., the `--no-hedge` argparse line), or
- be replaced with `True` (e.g., `OrderLifecycle(..., auto_hedge_enabled=True, ...)`)

Verify no occurrences remain.

- [ ] **Step 8: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
Wire AutoTrader lifecycle; remove --no-hedge

New CLI flags (--qty required; fee bps, throttle, day cap, strict,
timeouts, debug-var-payload). AutoTrader started after asset
activation, closed on shutdown, dispatched on every green-edge.
Startup banner echoes full config for log/replay alignment.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3.7 — Capture Variational order API and live smoke test (user action)

**Files:** none modified; `.env` updated by user.

- [ ] **Step 1: Capture the real Variational order request**

1. Open variational.io, open DevTools → Network → filter "Fetch/XHR".
2. Place a **very small, far-out-of-market** limit order manually via the UI so the request is visible but will not fill.
3. Find the POST request that was the order creation. Right-click → Copy → Copy as cURL.
4. Extract:
   - URL (e.g., `https://omni.variational.io/api/orders`)
   - Method (almost certainly `POST`)
   - Body JSON — identify the fields that correspond to side, qty, asset. Replace only those three with Python `str.format` placeholders `{side}`, `{qty}`, `{asset}`.

- [ ] **Step 2: Paste into `.env`**

```
VARIATIONAL_ORDER_URL=https://omni.variational.io/api/orders
VARIATIONAL_ORDER_METHOD=POST
VARIATIONAL_ORDER_BODY_TEMPLATE={"instrument":"{asset}","side":"{side}","qty":"{qty}","type":"market"}
```

(Actual body fields differ — paste the real captured body with placeholders substituted.)

- [ ] **Step 3: Live smoke with tiny qty**

```bash
python main.py --qty 0.001 --max-trades-per-day 3 --throttle-seconds 10
```

Monitor `log/order_events.jsonl` and `log/cycle_pnl.jsonl`. Expect:

- On first green edge: `cycle_opened` → `var_place_attempt` / `lighter_place_attempt` → acks → fills → `cycle_closed`.
- One line in `cycle_pnl.jsonl` with `status: "fully_filled"` and non-null `attribution.realized_net_pct`.
- Cross-check Variational UI and Lighter account: two fills appeared, same time, opposite sides.

If anything is wrong: `Ctrl+C` immediately, inspect `runtime.log` + `order_events.jsonl`, flatten residual positions manually in each UI, then fix.

- [ ] **Step 4: Commit (only if .env is committed — by default it is gitignored; skip otherwise)**

No git action needed unless you decided to commit a redacted example `.env.example`. Verify `.gitignore` already excludes `.env`:

```bash
grep -E '^\.env$' .gitignore || echo "MISSING"
```

Expected: `.env` appears. If `MISSING`, add it before anything else.

---

## Milestone 4 — Dashboard Stats + CLAUDE.md

### Task 4.1 — Aggregate stats row + breaker banner

**Files:**
- Modify: `main.py` (`render_dashboard`)

- [ ] **Step 1: Add a bottom panel**

In `render_dashboard`, at the end before `return Group(header, quote_table, spread_table, orders_table)`, build a stats block:

```python
stats = self.auto_trader.snapshot() if self.auto_trader is not None else None
if stats is not None:
    dropped = (self.signal_journal.dropped_count
               + self.events_journal.dropped_count
               + self.cycles_journal.dropped_count)
    frozen_suffix = "" if not stats.frozen else f"  ⚠ FROZEN[{stats.frozen_reason}]"
    drop_suffix = "" if dropped == 0 else f"  ⚠ 日志丢弃 {dropped}" if is_zh else f"  ⚠ journal drops {dropped}"
    if is_zh:
        stats_line = (
            f"今日 {stats.trades_today} 笔 | 失败 {stats.failures_today} | "
            f"累计净利(名义) {format(stats.cumulative_realized_net_notional, 'f')} | "
            f"均滑点 bps var={stats.avg_var_slippage_bps:.1f} lighter={stats.avg_lighter_slippage_bps:.1f} | "
            f"熔断: {'OK' if not stats.frozen else 'FROZEN'}{frozen_suffix}{drop_suffix}"
        )
    else:
        stats_line = (
            f"today {stats.trades_today} trades | fail {stats.failures_today} | "
            f"cum net (notional) {format(stats.cumulative_realized_net_notional, 'f')} | "
            f"avg slip bps var={stats.avg_var_slippage_bps:.1f} lighter={stats.avg_lighter_slippage_bps:.1f} | "
            f"breaker: {'OK' if not stats.frozen else 'FROZEN'}{frozen_suffix}{drop_suffix}"
        )
    stats_panel = Panel(stats_line, border_style=("red" if stats.frozen else "green"))
    return Group(header, quote_table, spread_table, orders_table, stats_panel)
return Group(header, quote_table, spread_table, orders_table)
```

- [ ] **Step 2: Manual verify**

Start the runtime, open dashboard, verify the new bottom row appears with 0 trades initially. Kill the Chrome extension (click Stop in popup); eventually after 3+ failed `cycle_opened` attempts or on extension-disconnected, the panel should turn red with `FROZEN[...]`.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "$(cat <<'EOF'
Dashboard stats row + breaker banner

Bottom panel reads in-memory TraderStats (never from disk): today's
trade and failure counts, cumulative realized net notional,
exponential-moving-average slippage bps per leg, breaker state,
and journal-drop count if any. Panel turns red on breaker trip.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4.2 — CLAUDE.md refresh

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Append new architecture section**

At the end of `CLAUDE.md`, under the existing "Architecture" section, append:

```markdown
### 4. Auto-trade pipeline (added 2026-04-19)

The runtime is no longer passive. On every `SignalEngine` green-edge (same formula as the dashboard used to compute inline), `AutoTrader` fires Variational + Lighter legs in parallel via:

- `VariationalCmdClient` → `CommandBroker` (:8768) → Chrome extension `background.js CommandSocket` → `content_script.js` fetch() on variational.io (browser session, so auth/cookies flow automatically).
- `LighterAdapterImpl.place_order` (thin wrapper over the existing `SignerClient.create_order`).

Cycle IDs (`cyc-YYYYMMDDHHMMSS-8hex`) correlate every event back to a single row in `log/cycle_pnl.jsonl` — that file is the canonical replay table.

Logging is off the main path: both `order_events.jsonl` and `cycle_pnl.jsonl` go through `EventJournal`, an async-queue-backed writer that drops-and-counts on overflow rather than block. Signal edges go to `signal_events.jsonl` via the same mechanism.

Breaker state machine: 3 consecutive failures or 5 failures in a UTC day → `TraderStats.frozen = True` → dashboard panel turns red, no new cycles fire. Only a process restart clears it.

The Variational order API URL / body shape is **runtime config** (not code). Set `VARIATIONAL_ORDER_URL`, `VARIATIONAL_ORDER_METHOD`, and `VARIATIONAL_ORDER_BODY_TEMPLATE` in `.env`; the body template uses `{side}`, `{qty}`, `{asset}` placeholders.

Key files:
- `variational/signal.py` — `SignalEngine`, `SignalState`, `DirectionState`
- `variational/auto_trader.py` — `AutoTrader`, `TradeCycle`, attribution math
- `variational/command_client.py` — `VariationalCmdClient`
- `variational/journal.py` — `EventJournal`
- `chrome_extension/content_script.js` — fetch proxy inside variational.io
```

- [ ] **Step 2: Update the commands section**

Replace the existing "Run the main runtime" block in `CLAUDE.md` (`## Commands`) with:

```markdown
Run the main runtime (auto-trade, Chinese dashboard):

```bash
python main.py --qty 0.01                  # required; units = base asset
python main.py --qty 0.01 --lang en        # English dashboard
python main.py --qty 0.01 --signal-strict  # stricter green gate: max of medians instead of any
```

`--qty` is required. Other flags default to safe values; see `python main.py --help` or the design spec. There is no longer a `--no-hedge` flag — send SIGINT (Ctrl+C) to stop.

Required `.env`:

```
LIGHTER_ACCOUNT_INDEX=...
LIGHTER_API_KEY_INDEX=...
LIGHTER_PRIVATE_KEY=...
VARIATIONAL_ORDER_URL=https://omni.variational.io/api/orders
VARIATIONAL_ORDER_METHOD=POST
VARIATIONAL_ORDER_BODY_TEMPLATE={"instrument":"{asset}","side":"{side}","qty":"{qty}","type":"market"}
```

Real `VARIATIONAL_ORDER_*` values must be captured from browser DevTools for the logged-in user.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
CLAUDE.md: document auto-trade pipeline

New section covering SignalEngine/AutoTrader/journals and the .env
keys needed for Variational order construction. Commands block
reflects --qty required, --no-hedge removed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Checklist

1. **Spec coverage**
   - §3 architecture → Tasks 1.x (channel) + 2.x (signal/journal) + 3.x (auto-trade) + 4.x (dashboard) ✅
   - §4.1 SignalEngine → Task 2.2 ✅
   - §4.2 EventJournal → Task 2.1 ✅
   - §4.3 VariationalCmdClient → Task 1.2 ✅
   - §4.4 AutoTrader → Tasks 3.1–3.4 ✅
   - §4.5 content_script → Task 1.3 ✅
   - §4.6 background.js CommandSocket → Task 1.4 ✅
   - §4.7 manifest changes → Task 1.3 ✅
   - §4.8 main.py changes → Tasks 3.5, 3.6, 4.1 ✅
   - §6 logging (3 files, non-blocking) → Tasks 2.1, 2.4, 3.3, 3.4 ✅
   - §7 error matrix → encoded in reason_codes / breaker logic in Task 3.4 ✅
   - §8 security (content_script sender check, 127.0.0.1, no key logging, payload sha256/redaction) → Tasks 1.3, 3.6 (`--debug-var-payload` flag). Note: payload_sha256 explicit logging is mentioned in spec but implemented here only as "full body only if `--debug-var-payload`" — if you want sha256 always, add it in `VariationalPlacerImpl`. ✅ (acceptable, user approved this abstraction level)
   - §9 CLI → Task 3.6 ✅
   - §10 milestones → mapped 1:1 ✅

2. **Placeholder scan**
   - No "TBD / TODO / implement later" in code blocks. Variational API shape is handled via env-config (runtime data, not code placeholder).
   - Only "fill this in" moment is Task 3.7 where the user pastes real API details. That is an intentional configuration step, not a plan failure.

3. **Type consistency**
   - `FetchResult` / `VarPlaceResult` / `LighterPlaceResult` / `TradeCycle` / `TraderStats` defined once, used with matching fields downstream.
   - `execute_fetch` signature identical in `VariationalCmdClient` (Task 1.2) and `VariationalPlacerImpl` (Task 3.5).
   - `on_green_edge(direction, state)`, `on_variational_fill(trade_id, fill_px, fill_qty)`, `on_lighter_fill(client_order_id, fill_px, fill_qty)` signatures matched between AutoTrader and main.py wire-up.

4. **Scope**
   - 4 milestones, 17 tasks. Each milestone ends with a runnable system. Suitable for subagent-driven execution.

---

**Plan complete and saved to `docs/superpowers/plans/2026-04-19-cross-trade-volume-farming-plan.md`.**
