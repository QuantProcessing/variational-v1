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
