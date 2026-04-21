import argparse
import asyncio
import contextlib
import csv
import json
import logging
import os
import signal
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Any

import requests
import websockets
from dotenv import load_dotenv
from lighter.signer_client import SignerClient
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from variational.auto_trader import (
    AutoTrader, AutoTraderConfig, LighterAdapter, LighterPlaceResult,
    VariationalPlacer, VarPlaceResult,
)
from variational.command_client import VariationalCmdClient
from variational.journal import EventJournal
from variational.listener import (
    HEARTBEAT_STALE_SECONDS,
    CommandBroker,
    EventSink,
    VariationalMonitor,
    run_command_server,
    run_receiver_server,
)
from variational.signal import SignalEngine

VARIATIONAL_TICKER_OVERRIDES = {
    "LIT": "LIGHTER",
}
VARIATIONAL_ASSET_TO_LIGHTER_TICKER = {v: k for k, v in VARIATIONAL_TICKER_OVERRIDES.items()}

FORWARDER_HOST = "127.0.0.1"
FORWARDER_WS_PORT = 8766
FORWARDER_REST_PORT = 8767
FORWARDER_COMMAND_PORT = 8768
VAR_QUOTE_URL = "https://omni.variational.io/api/quotes/indicative"
VAR_NEW_MARKET_URL = "https://omni.variational.io/api/orders/new/market"
VAR_QUOTE_ACCEPT_URL = "https://omni.variational.io/api/quotes/accept"
# Variational perpetual futures on omni.variational.io are all hourly-funded,
# USDC-settled. If Variational adds products outside that shape, make these
# overrides per-asset.
VAR_FUNDING_INTERVAL_S = 3600
VAR_SETTLEMENT_ASSET = "USDC"
VAR_INSTRUMENT_TYPE = "perpetual_future"
LOG_DIR = Path("./log")
OUTPUT_DIR = LOG_DIR
APP_LOG_FILE = LOG_DIR / "runtime.log"
TRADE_RECORDS_CSV_FILE = LOG_DIR / "trade_records.csv"
READY_TIMEOUT_SECONDS = 60.0
POLL_INTERVAL_SECONDS = 0.05
HEDGE_SLIPPAGE_BPS = 100.0
DASHBOARD_REFRESH_SECONDS = 1.0
DASHBOARD_ORDERS = 8
ASSET_SWITCH_CONFIRM_TICKS = 3
LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
LIGHTER_WS_PING_INTERVAL_SECONDS = 30
LIGHTER_WS_PING_TIMEOUT_SECONDS = 30


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def decimal_to_str(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value, "f")


def resolve_variational_ticker(ticker: str) -> str:
    return VARIATIONAL_TICKER_OVERRIDES.get(ticker.upper(), ticker.upper())


def resolve_lighter_ticker(variational_asset: str) -> str:
    asset = variational_asset.upper()
    return VARIATIONAL_ASSET_TO_LIGHTER_TICKER.get(asset, asset)


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set")
    return value


def required_int_env(name: str) -> int:
    value = required_env(name)
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got: {value}") from exc


def env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def spread_value(aggressive_buy_ask: Decimal | None, aggressive_sell_bid: Decimal | None) -> Decimal | None:
    if aggressive_buy_ask is None or aggressive_sell_bid is None:
        return None
    return aggressive_sell_bid - aggressive_buy_ask


def spread_percent(diff: Decimal | None, denominator: Decimal | None) -> Decimal | None:
    if diff is None or denominator is None or denominator == 0:
        return None
    return (diff / denominator) * Decimal("100")


def book_spread_percent(bid: Decimal | None, ask: Decimal | None) -> Decimal | None:
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / Decimal("2")
    if mid == 0:
        return None
    return ((ask - bid) / mid) * Decimal("100")


def normalize_variational_status(status: str) -> str:
    lowered = status.strip().lower()
    if lowered == "confirmed":
        return "filled"
    return lowered


@dataclass(slots=True)
class OrderLifecycle:
    trade_key: str
    trade_id: str
    side: str
    qty: Decimal
    asset: str
    auto_hedge_enabled: bool
    last_variational_status: str

    var_fill_price: Decimal | None = None
    var_fill_ts_iso: str | None = None

    lighter_side: str | None = None
    lighter_client_order_id: int | None = None
    lighter_fill_price: Decimal | None = None
    lighter_fill_ts_iso: str | None = None
    lighter_tx_hash: str | None = None
    hedge_error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "trade_key": self.trade_key,
            "trade_id": self.trade_id,
            "side": self.side,
            "qty": decimal_to_str(self.qty),
            "asset": self.asset,
            "variational_filled_price": decimal_to_str(self.var_fill_price),
            "variational_filled_at": self.var_fill_ts_iso,
            "lighter_order_side": self.lighter_side,
            "lighter_client_order_id": self.lighter_client_order_id,
            "lighter_filled_price": decimal_to_str(self.lighter_fill_price),
            "lighter_filled_at": self.lighter_fill_ts_iso,
            "auto_hedge_enabled": self.auto_hedge_enabled,
            "hedge_error": self.hedge_error,
            "last_variational_status": self.last_variational_status,
        }


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


class VariationalPlacerImpl:
    """Two-step RFQ flow: /api/quotes/indicative → /api/orders/new/market.

    Variational wants a quote_id before accepting a market order, so we request
    an indicative quote for the exact (asset, qty), then pass its quote_id to
    the order endpoint with side + max_slippage. The response's rfq_id becomes
    the correlation key for matching fills streamed over /events WS.
    """

    def __init__(
        self,
        cmd: VariationalCmdClient,
        max_slippage: float,
        logger: logging.Logger,
    ) -> None:
        self.cmd = cmd
        self.max_slippage = max_slippage
        self.logger = logger

    async def _rfq_then_submit(
        self, side: str, qty: Decimal, asset: str, timeout_ms: int,
        submit_url: str, is_reduce_only: bool,
    ) -> VarPlaceResult:
        import json as _json

        qty_str = format(qty, "f")
        indicative_body = _json.dumps({
            "instrument": {
                "underlying": asset,
                "funding_interval_s": VAR_FUNDING_INTERVAL_S,
                "settlement_asset": VAR_SETTLEMENT_ASSET,
                "instrument_type": VAR_INSTRUMENT_TYPE,
            },
            "qty": qty_str,
        })
        quote_res = await self.cmd.execute_fetch(
            url=VAR_QUOTE_URL, method="POST",
            headers={"content-type": "application/json"},
            body=indicative_body, timeout_ms=timeout_ms,
        )
        if not quote_res.ok or (quote_res.status is not None and quote_res.status >= 400):
            return VarPlaceResult(
                ok=False, raw_status=quote_res.status, raw_body=quote_res.body,
                latency_ms=quote_res.latency_ms,
                error=quote_res.error or f"quote_http_{quote_res.status}",
                request_id=quote_res.request_id,
            )
        try:
            quote_parsed = _json.loads(quote_res.body or "{}")
        except Exception as exc:
            return VarPlaceResult(ok=False, error=f"quote_parse_failed: {exc}", raw_body=quote_res.body)
        quote_id = quote_parsed.get("quote_id") if isinstance(quote_parsed, dict) else None
        if not quote_id:
            return VarPlaceResult(ok=False, error="quote_missing_id", raw_body=quote_res.body)

        order_body = _json.dumps({
            "quote_id": quote_id,
            "side": side,
            "max_slippage": self.max_slippage,
            "is_reduce_only": is_reduce_only,
        })
        order_res = await self.cmd.execute_fetch(
            url=submit_url, method="POST",
            headers={"content-type": "application/json"},
            body=order_body, timeout_ms=timeout_ms,
        )
        if not order_res.ok or (order_res.status is not None and order_res.status >= 400):
            return VarPlaceResult(
                ok=False, raw_status=order_res.status, raw_body=order_res.body,
                latency_ms=order_res.latency_ms,
                error=order_res.error or f"order_http_{order_res.status}",
                request_id=order_res.request_id,
            )
        rfq_id: str | None = None
        try:
            order_parsed = _json.loads(order_res.body or "{}")
            if isinstance(order_parsed, dict):
                raw_rfq = order_parsed.get("rfq_id")
                rfq_id = str(raw_rfq) if raw_rfq else None
        except Exception:
            pass
        if not rfq_id:
            return VarPlaceResult(
                ok=False, raw_status=order_res.status, raw_body=order_res.body,
                latency_ms=order_res.latency_ms, error="order_missing_rfq_id",
                request_id=order_res.request_id,
            )
        # trade_id is the correlation handle AutoTrader matches against fill
        # events. Variational's /events WS reports fills with rfq_id, not the
        # fill-level trade_id — see variational/listener.py._update_trade_event.
        return VarPlaceResult(
            ok=True, raw_status=order_res.status, raw_body=order_res.body,
            latency_ms=order_res.latency_ms, trade_id=rfq_id, request_id=order_res.request_id,
        )

    async def place_order(self, side: str, qty: Decimal, asset: str, timeout_ms: int) -> VarPlaceResult:
        """Open a new position via /api/orders/new/market (is_reduce_only: false)."""
        return await self._rfq_then_submit(
            side, qty, asset, timeout_ms,
            submit_url=VAR_NEW_MARKET_URL, is_reduce_only=False,
        )

    async def place_close_order(self, side: str, qty: Decimal, asset: str, timeout_ms: int) -> VarPlaceResult:
        """Reduce an existing position via /api/quotes/accept (is_reduce_only: true)."""
        return await self._rfq_then_submit(
            side, qty, asset, timeout_ms,
            submit_url=VAR_QUOTE_ACCEPT_URL, is_reduce_only=True,
        )


class LighterAdapterImpl:
    def __init__(self, runtime: "VariationalToLighterRuntime") -> None:
        self.runtime = runtime

    async def best_bid_ask(self):
        return await self.runtime.get_lighter_best_bid_ask()

    async def place_order(
        self, side: str, qty: Decimal, limit_px: Decimal, client_order_id: int,
        reduce_only: bool = False,
    ) -> LighterPlaceResult:
        runtime = self.runtime
        base_amount = int(qty * runtime.base_amount_multiplier)
        if base_amount <= 0:
            return LighterPlaceResult(ok=False, client_order_id=client_order_id,
                                      error=f"qty_rounds_to_zero: {qty}")
        price_i = int(limit_px * runtime.price_multiplier)
        is_ask = (side == "SELL")
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
                    reduce_only=reduce_only,
                    trigger_price=0,
                )
            if error is not None:
                return LighterPlaceResult(ok=False, client_order_id=client_order_id, error=f"sign: {error}")
            return LighterPlaceResult(ok=True, client_order_id=client_order_id, tx_hash=tx_hash)
        except Exception as exc:
            return LighterPlaceResult(ok=False, client_order_id=client_order_id, error=f"exception: {exc}")


class VariationalToLighterRuntime:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.ticker: str | None = None
        self.variational_ticker: str | None = None
        self.accepted_assets: set[str] = set()

        self.stop_flag = False
        self.logger = logging.getLogger("var_lighter_runtime")
        self.logger.setLevel(logging.INFO)
        self.logger.handlers.clear()
        self.logger.propagate = False

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(APP_LOG_FILE, encoding="utf-8")
        file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(message)s"))
        self.logger.addHandler(file_handler)
        self.dashboard_console = Console()

        output_dir = OUTPUT_DIR.expanduser().resolve()
        self.runtime = VariationalRuntime(
            host=FORWARDER_HOST,
            ws_port=FORWARDER_WS_PORT,
            rest_port=FORWARDER_REST_PORT,
            command_port=FORWARDER_COMMAND_PORT,
            output_dir=None,
            quiet=True,
        )

        self.orders_file = output_dir / "order_metrics.jsonl" if output_dir else None
        self.trade_records_csv_file = output_dir / TRADE_RECORDS_CSV_FILE.name if output_dir else None
        self.signal_events_file = output_dir / "signal_events.jsonl" if output_dir else LOG_DIR / "signal_events.jsonl"
        self.signal_journal = EventJournal(self.signal_events_file)
        self.events_journal = EventJournal(LOG_DIR / "order_events.jsonl")
        self.cycles_journal = EventJournal(LOG_DIR / "cycle_pnl.jsonl")
        self.auto_trader = None    # constructed in run() once market is known (Task 3.6)
        self.cmd_client: VariationalCmdClient | None = None
        self._order_write_lock = asyncio.Lock()
        self._trade_csv_write_lock = asyncio.Lock()
        self._trade_records_snapshot_sig: str | None = None

        self.records: dict[str, OrderLifecycle] = {}
        self.record_order: deque[str] = deque(maxlen=500)
        self.lighter_client_order_to_trade_key: dict[int, str] = {}
        self._record_lock = asyncio.Lock()
        self.signal_engine = SignalEngine(strict=args.signal_strict)
        self._asset_switch_lock = asyncio.Lock()
        self._asset_switch_candidate: str | None = None
        self._asset_switch_candidate_hits = 0

        self.trade_event_cursor = 0

        self.lighter_base_url = "https://mainnet.zklighter.elliot.ai"
        self.account_index = required_int_env("LIGHTER_ACCOUNT_INDEX")
        self.api_key_index = required_int_env("LIGHTER_API_KEY_INDEX")
        self.lighter_client: SignerClient | None = None
        self._lighter_signer_lock = asyncio.Lock()

        self.lighter_market_index = 0
        self.base_amount_multiplier = 0
        self.price_multiplier = 0

        # Account equity tracking: both venues stream through WS. Variational
        # pushes via /portfolio (already subscribed); Lighter pushes via
        # user_stats/{account_index} on the same WS connection as order_book.
        self.startup_ts_iso: str | None = None
        self.startup_var_total: Decimal | None = None
        self.startup_lighter_total: Decimal | None = None
        self.current_lighter_balance: Decimal | None = None
        self.current_lighter_upnl: Decimal | None = None
        self._account_lock = asyncio.Lock()

        self.lighter_order_book = {"bids": {}, "asks": {}}
        self.lighter_best_bid: Decimal | None = None
        self.lighter_best_ask: Decimal | None = None
        self.lighter_best_bid_qty: Decimal | None = None
        self.lighter_best_ask_qty: Decimal | None = None
        self.lighter_order_book_offset = 0
        self.lighter_order_book_ready = False
        self.lighter_snapshot_loaded = False
        self.lighter_order_book_sequence_gap = False
        self.lighter_order_book_lock = asyncio.Lock()

        self.lighter_ws_task: asyncio.Task[None] | None = None
        self.trade_task: asyncio.Task[None] | None = None
        self.dashboard_task: asyncio.Task[None] | None = None

        qty_dec = Decimal(args.qty)
        position_limit_dec = Decimal(args.position_limit)
        self.auto_trader_config = AutoTraderConfig(
            qty=qty_dec,
            throttle_seconds=args.throttle_seconds,
            max_trades_per_day=args.max_trades_per_day,
            position_limit=position_limit_dec,
            leg_settle_timeout_sec=args.leg_settle_timeout_sec,
            var_order_timeout_ms=args.var_order_timeout_ms,
        )

    def print_startup_next_steps(self) -> None:
        is_zh = self.args.lang == "zh"
        if is_zh:
            lines = [
                "Python 脚本已就位，请回到 Chrome 加载并启动扩展。若 Chrome 插件已启动，请刷新网页。",
                "Use `python main.py --lang en` for the English dashboard.",
            ]
            title = "启动指引"
        else:
            lines = [
                "Python runtime is ready. Go back to Chrome and load/start the extension.",
                "If the Chrome extension has already started, please refresh the webpage."
            ]
            title = "Startup Guide"
        self.dashboard_console.print(Panel("\n".join(lines), title=title, border_style="yellow"))

    def setup_signal_handlers(self) -> None:
        signal.signal(signal.SIGINT, self.shutdown)
        signal.signal(signal.SIGTERM, self.shutdown)

    def shutdown(self, signum=None, frame=None) -> None:
        self.stop_flag = True

    def initialize_lighter_client(self) -> SignerClient:
        if self.lighter_client is None:
            api_key_private_key = os.getenv("API_KEY_PRIVATE_KEY", "").strip() or required_env("LIGHTER_PRIVATE_KEY")
            self.lighter_client = SignerClient(
                url=self.lighter_base_url,
                account_index=self.account_index,
                api_private_keys={self.api_key_index: api_key_private_key},
            )
            err = self.lighter_client.check_client()
            if err is not None:
                raise RuntimeError(f"CheckClient error: {err}")
        return self.lighter_client

    def get_lighter_market_config(self) -> tuple[int, int, int]:
        if not self.ticker:
            raise RuntimeError("Ticker is not resolved yet")
        response = requests.get(
            f"{self.lighter_base_url}/api/v1/orderBooks",
            headers={"accept": "application/json"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()

        for market in data.get("order_books", []):
            if market.get("symbol") == self.ticker:
                price_decimals = int(market["supported_price_decimals"])
                size_decimals = int(market["supported_size_decimals"])
                return int(market["market_id"]), pow(10, size_decimals), pow(10, price_decimals)

        raise RuntimeError(f"Ticker {self.ticker} not found in Lighter order books")

    async def detect_current_variational_asset(self) -> str | None:
        async with self.runtime.monitor._lock:
            if self.runtime.monitor.current_quote_asset:
                asset = str(self.runtime.monitor.current_quote_asset).strip().upper()
                quote = self.runtime.monitor.quotes.get(asset)
                if (
                    asset
                    and asset != "UNKNOWN"
                    and isinstance(quote, dict)
                    and to_decimal(quote.get("bid")) is not None
                    and to_decimal(quote.get("ask")) is not None
                ):
                    return asset

        return None

    async def wait_for_ticker_resolution(self) -> str:
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while not self.stop_flag and time.time() < deadline:
            asset = await self.detect_current_variational_asset()
            if asset:
                return asset
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

        raise RuntimeError("Timed out deriving ticker from Variational quote/trade messages")

    async def _reset_state_for_asset_switch(self) -> None:
        async with self._record_lock:
            self.records.clear()
            self.record_order.clear()
            self.lighter_client_order_to_trade_key.clear()
        self.signal_engine = SignalEngine(strict=self.args.signal_strict)
        async with self._trade_csv_write_lock:
            self._trade_records_snapshot_sig = None

    async def activate_asset(self, variational_asset: str, reason: str) -> None:
        asset = variational_asset.strip().upper()
        if not asset or asset == "UNKNOWN":
            return

        async with self._asset_switch_lock:
            next_ticker = resolve_lighter_ticker(asset)
            if self.variational_ticker == asset and self.ticker == next_ticker:
                return

            self.variational_ticker = asset
            self.ticker = next_ticker
            self.accepted_assets = {
                asset,
                next_ticker,
                resolve_variational_ticker(next_ticker),
            }

            self.lighter_market_index, self.base_amount_multiplier, self.price_multiplier = self.get_lighter_market_config()
            await self.reset_lighter_order_book()
            await self._reset_state_for_asset_switch()

            if self.lighter_ws_task and not self.lighter_ws_task.done():
                self.lighter_ws_task.cancel()
                await asyncio.gather(self.lighter_ws_task, return_exceptions=True)

            self.lighter_ws_task = asyncio.create_task(self.handle_lighter_ws())
            await self.wait_for_lighter_order_book_ready()
            self.logger.info(
                "Switched market (%s): variational_asset=%s -> lighter_ticker=%s market_id=%s",
                reason,
                self.variational_ticker,
                self.ticker,
                self.lighter_market_index,
            )

    async def wait_for_variational_ready(self) -> None:
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while not self.stop_flag and time.time() < deadline:
            state = await self.runtime.monitor.get_trading_state()
            hb_age = state.get("heartbeat_age")
            if hb_age is not None and hb_age <= HEARTBEAT_STALE_SECONDS:
                return
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
        raise RuntimeError("Timed out waiting for Variational events stream heartbeat")

    async def wait_for_lighter_order_book_ready(self) -> None:
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while not self.stop_flag and time.time() < deadline:
            if self.lighter_order_book_ready:
                return
            await asyncio.sleep(0.2)
        raise RuntimeError("Timed out waiting for Lighter order book")

    async def reset_lighter_order_book(self) -> None:
        async with self.lighter_order_book_lock:
            self.lighter_order_book["bids"].clear()
            self.lighter_order_book["asks"].clear()
            self.lighter_order_book_offset = 0
            self.lighter_order_book_ready = False
            self.lighter_snapshot_loaded = False
            self.lighter_order_book_sequence_gap = False
            self.lighter_best_bid = None
            self.lighter_best_ask = None

    def update_lighter_order_book(self, side: str, levels: list[Any]) -> None:
        for level in levels:
            if isinstance(level, list) and len(level) >= 2:
                price = Decimal(str(level[0]))
                size = Decimal(str(level[1]))
            elif isinstance(level, dict):
                price = Decimal(str(level.get("price", 0)))
                size = Decimal(str(level.get("size", 0)))
            else:
                continue

            if size > 0:
                self.lighter_order_book[side][price] = size
            else:
                self.lighter_order_book[side].pop(price, None)

    def validate_order_book_offset(self, new_offset: int) -> bool:
        return new_offset > self.lighter_order_book_offset

    def _refresh_lighter_best_locked(self) -> None:
        """Recompute best bid/ask + their top-of-book qty. Must be called
        under self.lighter_order_book_lock."""
        bids = self.lighter_order_book["bids"]
        asks = self.lighter_order_book["asks"]
        if bids:
            best_bid = max(bids.keys())
            self.lighter_best_bid = best_bid
            self.lighter_best_bid_qty = bids.get(best_bid)
        else:
            self.lighter_best_bid = None
            self.lighter_best_bid_qty = None
        if asks:
            best_ask = min(asks.keys())
            self.lighter_best_ask = best_ask
            self.lighter_best_ask_qty = asks.get(best_ask)
        else:
            self.lighter_best_ask = None
            self.lighter_best_ask_qty = None

    async def _trigger_close_on_book_tick(self) -> None:
        """After a Lighter book update, give AutoTrader a chance to fire a
        WS-driven reduce-only close. Uses cached Variational quote as an
        estimate; the actual close path requests a fresh RFQ."""
        if self.auto_trader is None:
            return
        var_bid, var_ask, _ = await self.get_variational_best_bid_ask(self.variational_ticker)
        async with self.lighter_order_book_lock:
            lb = self.lighter_best_bid
            la = self.lighter_best_ask
            lbq = self.lighter_best_bid_qty
            laq = self.lighter_best_ask_qty
        await self.auto_trader.try_close_on_book_tick(
            asset=self.variational_ticker or "",
            var_bid=var_bid, var_ask=var_ask,
            lighter_bid=lb, lighter_ask=la,
            lighter_bid_qty=lbq, lighter_ask_qty=laq,
        )

    async def request_fresh_snapshot(self, ws: Any) -> None:
        await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))

    async def handle_lighter_fill_update(self, order: dict[str, Any]) -> None:
        if order.get("status") != "filled":
            return

        client_order_id_raw = order.get("client_order_id")
        try:
            client_order_id = int(client_order_id_raw)
        except Exception:
            return

        fill_price: Decimal | None = None
        filled_quote = to_decimal(order.get("filled_quote_amount"))
        filled_base = to_decimal(order.get("filled_base_amount"))
        if filled_quote is not None and filled_base is not None and filled_base != 0:
            fill_price = filled_quote / filled_base

        # Route to AutoTrader FIRST; this path uses _lighter_order_to_cycle
        # which is populated by AutoTrader itself, independent of the legacy
        # OrderLifecycle bookkeeping below. Must run before any early return
        # that is gated on the legacy lookup (which fails under the new
        # AutoTrader-driven flow since place_lighter_order is gone).
        if self.auto_trader is not None and fill_price is not None and filled_base is not None:
            # Lighter order: is_ask True -> sell, False -> buy.
            side = "SELL" if order.get("is_ask") else "BUY"
            await self.auto_trader.on_lighter_fill(
                client_order_id, fill_price, filled_base, side=side,
            )

        now_iso = utc_now()

        async with self._record_lock:
            trade_key = self.lighter_client_order_to_trade_key.get(client_order_id)
            if not trade_key:
                return
            record = self.records.get(trade_key)
            if record is None:
                return
            if record.lighter_fill_ts_iso is not None:
                return

            record.lighter_fill_ts_iso = now_iso
            record.lighter_fill_price = fill_price
            payload = record.to_payload()

        await self.append_order_log("lighter_fill", payload)

    def build_lighter_ws_url(self) -> str:
        if env_flag("LIGHTER_WS_SERVER_PINGS"):
            return f"{LIGHTER_WS_URL}?server_pings=true"
        return LIGHTER_WS_URL

    async def handle_lighter_ws(self) -> None:
        while not self.stop_flag:
            try:
                await self.reset_lighter_order_book()
                url = self.build_lighter_ws_url()
                async with websockets.connect(
                    url,
                    ping_interval=LIGHTER_WS_PING_INTERVAL_SECONDS,
                    ping_timeout=LIGHTER_WS_PING_TIMEOUT_SECONDS,
                ) as ws:
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"order_book/{self.lighter_market_index}"}))
                    # user_stats channel streams collateral + portfolio_value
                    # (unauth'd per Lighter WS docs).
                    await ws.send(json.dumps({"type": "subscribe", "channel": f"user_stats/{self.account_index}"}))

                    account_orders_channel = f"account_orders/{self.lighter_market_index}/{self.account_index}"
                    try:
                        async with self._lighter_signer_lock:
                            if not self.lighter_client:
                                self.initialize_lighter_client()
                            auth_token, err = self.lighter_client.create_auth_token_with_expiry(
                                api_key_index=self.api_key_index
                            )
                        if err is None:
                            await ws.send(
                                json.dumps(
                                    {
                                        "type": "subscribe",
                                        "channel": account_orders_channel,
                                        "auth": auth_token,
                                    }
                                )
                            )
                        else:
                            self.logger.warning("Failed to create Lighter WS auth token: %s", err)
                    except Exception as exc:
                        self.logger.warning("Error creating Lighter WS auth token: %s", exc)

                    while not self.stop_flag:
                        raw = await ws.recv()
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", errors="replace")
                        data = json.loads(raw)
                        msg_type = data.get("type")

                        if msg_type == "subscribed/order_book":
                            async with self.lighter_order_book_lock:
                                self.lighter_order_book["bids"].clear()
                                self.lighter_order_book["asks"].clear()
                                order_book = data.get("order_book", {})
                                self.lighter_order_book_offset = int(order_book.get("offset", 0) or 0)
                                self.update_lighter_order_book("bids", order_book.get("bids", []))
                                self.update_lighter_order_book("asks", order_book.get("asks", []))
                                self.lighter_snapshot_loaded = True
                                self.lighter_order_book_ready = True
                                self._refresh_lighter_best_locked()
                            await self._trigger_close_on_book_tick()

                        elif msg_type == "update/order_book" and self.lighter_snapshot_loaded:
                            order_book = data.get("order_book", {})
                            if "offset" not in order_book:
                                continue
                            new_offset = int(order_book["offset"])
                            async with self.lighter_order_book_lock:
                                if not self.validate_order_book_offset(new_offset):
                                    self.lighter_order_book_sequence_gap = True
                                else:
                                    self.update_lighter_order_book("bids", order_book.get("bids", []))
                                    self.update_lighter_order_book("asks", order_book.get("asks", []))
                                    self.lighter_order_book_offset = new_offset
                                    self._refresh_lighter_best_locked()
                            await self._trigger_close_on_book_tick()

                        elif msg_type == "update/account_orders":
                            orders = data.get("orders", {}).get(str(self.lighter_market_index), [])
                            for order in orders:
                                await self.handle_lighter_fill_update(order)

                        elif msg_type in ("update/user_stats", "subscribed/user_stats"):
                            # Lighter sends `subscribed/user_stats` as the initial
                            # snapshot, then `update/user_stats` on subsequent
                            # account changes. Both have the same `stats` shape.
                            stats = data.get("stats") or {}
                            coll = to_decimal(stats.get("collateral"))
                            port = to_decimal(stats.get("portfolio_value"))
                            # upnl = portfolio_value - collateral (Lighter's own definition).
                            if coll is not None:
                                async with self._account_lock:
                                    self.current_lighter_balance = coll
                                    if port is not None:
                                        self.current_lighter_upnl = port - coll

                        if self.lighter_order_book_sequence_gap:
                            await self.request_fresh_snapshot(ws)
                            self.lighter_order_book_sequence_gap = False

                        if msg_type == "ping":
                            await ws.send(json.dumps({"type": "pong"}))

            except asyncio.CancelledError:
                return
            except Exception as exc:
                self.logger.warning(
                    "Lighter websocket reconnect after error: %s (url=%s)",
                    exc,
                    self.build_lighter_ws_url(),
                )
                await asyncio.sleep(1)

    async def get_lighter_best_bid_ask(self) -> tuple[Decimal | None, Decimal | None]:
        async with self.lighter_order_book_lock:
            return self.lighter_best_bid, self.lighter_best_ask

    async def read_variational_account_snapshot(self) -> tuple[Decimal | None, Decimal | None]:
        """Read balance / upnl from the /portfolio WS cache. Returns (balance, upnl)."""
        async with self.runtime.monitor._lock:
            pf = self.runtime.monitor.portfolio_summary or {}
            return to_decimal(pf.get("balance")), to_decimal(pf.get("upnl"))

    async def read_lighter_account_snapshot(self) -> tuple[Decimal | None, Decimal | None]:
        async with self._account_lock:
            return self.current_lighter_balance, self.current_lighter_upnl

    async def _capture_startup_account_snapshot(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for both venues' account streams to
        push first data, then anchor startup_*_total for delta display."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            var_bal, var_upnl = await self.read_variational_account_snapshot()
            lig_bal, lig_upnl = await self.read_lighter_account_snapshot()
            var_ok = var_bal is not None and var_upnl is not None
            lig_ok = lig_bal is not None and lig_upnl is not None
            if var_ok and lig_ok:
                self.startup_var_total = var_bal + var_upnl
                self.startup_lighter_total = lig_bal + lig_upnl
                self.startup_ts_iso = utc_now()
                self.logger.info(
                    "Startup equity: var_total=%s lighter_total=%s grand=%s",
                    self.startup_var_total, self.startup_lighter_total,
                    self.startup_var_total + self.startup_lighter_total,
                )
                return
            await asyncio.sleep(0.2)
        # Partial anchoring if either side arrived; otherwise leave as None
        # and the dashboard panel will show "-" until WS pushes land.
        var_bal, var_upnl = await self.read_variational_account_snapshot()
        lig_bal, lig_upnl = await self.read_lighter_account_snapshot()
        if var_bal is not None and var_upnl is not None:
            self.startup_var_total = var_bal + var_upnl
        if lig_bal is not None and lig_upnl is not None:
            self.startup_lighter_total = lig_bal + lig_upnl
        if self.startup_var_total is not None or self.startup_lighter_total is not None:
            self.startup_ts_iso = utc_now()
        self.logger.warning(
            "Startup account snapshot incomplete after %.1fs: var=%s lighter=%s",
            timeout, self.startup_var_total, self.startup_lighter_total,
        )

    async def get_variational_best_bid_ask(self, preferred_asset: str | None):
        async with self.runtime.monitor._lock:
            quote = None
            if preferred_asset:
                quote = self.runtime.monitor.quotes.get(preferred_asset)
            if quote is None and self.variational_ticker:
                quote = self.runtime.monitor.quotes.get(self.variational_ticker)
            if quote is None and self.runtime.monitor.current_quote_asset:
                quote = self.runtime.monitor.quotes.get(self.runtime.monitor.current_quote_asset)

            if quote is None:
                return None, None, None
            return to_decimal(quote.get("bid")), to_decimal(quote.get("ask")), str(quote.get("asset", ""))

    @staticmethod
    def trade_key(event: dict[str, Any]) -> str:
        trade_id = str(event.get("trade_id", "")).strip()
        if trade_id:
            return f"id:{trade_id}"
        event_seq = str(event.get("event_seq", "")).strip()
        return f"seq:{event_seq}"

    async def append_order_log(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.orders_file is None:
            return
        row = {
            "event": event_type,
            "logged_at": utc_now(),
            **payload,
        }
        line = json.dumps(row, ensure_ascii=True) + "\n"
        async with self._order_write_lock:
            await asyncio.to_thread(self.orders_file.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(self._append_line, self.orders_file, line)

    @staticmethod
    def _append_line(path: Path, line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)

    def should_track_variational_event(self, event: dict[str, Any]) -> bool:
        side = str(event.get("side", "")).strip().lower()
        if side not in {"buy", "sell"}:
            return False

        qty = to_decimal(event.get("qty"))
        if qty is None or qty <= 0:
            return False

        asset = str(event.get("asset", "")).strip().upper()
        if not asset:
            return False
        return asset in self.accepted_assets

    async def process_variational_trade_event(self, event: dict[str, Any]) -> None:
        if not self.should_track_variational_event(event):
            return

        key = self.trade_key(event)
        side = str(event.get("side", "")).strip().lower()
        qty = to_decimal(event.get("qty"))
        if qty is None:
            return

        status = normalize_variational_status(str(event.get("status", "")))
        asset = str(event.get("asset", "")).strip().upper() or self.variational_ticker
        trade_id = str(event.get("trade_id", "")).strip()

        now_iso = utc_now()
        fill_iso = str(event.get("timestamp") or now_iso)

        created = False
        created_record: OrderLifecycle | None = None

        async with self._record_lock:
            record = self.records.get(key)
            if record is None:
                record = OrderLifecycle(
                    trade_key=key,
                    trade_id=trade_id,
                    side=side,
                    qty=qty,
                    asset=asset if asset else "UNKNOWN",
                    auto_hedge_enabled=True,
                    last_variational_status=status,
                )
                self.records[key] = record
                self.record_order.append(key)
                created = True
                created_record = record
            else:
                previous_status = record.last_variational_status
                record.last_variational_status = status

            if created:
                previous_status = ""

            should_set_fill = False
            if status == "filled":
                if record.var_fill_ts_iso is None:
                    should_set_fill = True
                elif previous_status != "filled":
                    should_set_fill = True

            if should_set_fill:
                record.var_fill_ts_iso = fill_iso
                record.var_fill_price = to_decimal(event.get("price"))
                filled_payload = record.to_payload()
            else:
                filled_payload = None

        if filled_payload is not None:
            await self.append_order_log("variational_fill", filled_payload)

        if self.auto_trader is not None and filled_payload is not None:
            fill_px = to_decimal(event.get("price"))
            fill_qty_new = to_decimal(event.get("qty"))
            # AutoTrader stores rfq_id (returned by /api/orders/new/market) on
            # var_leg.trade_ids; match by rfq_id, not by the fill-level trade_id.
            # listener normalizes rfq_id to str-or-None already.
            rfq_id = event.get("rfq_id")
            correlation_key = rfq_id or str(event.get("trade_id", "")).strip() or None
            if fill_px is not None and fill_qty_new is not None and correlation_key:
                var_side = str(event.get("side", "")).strip().lower() or None
                await self.auto_trader.on_variational_fill(
                    correlation_key, fill_px, fill_qty_new, side=var_side,
                )

            # Bridge AutoTrader's cycle state into the legacy OrderLifecycle so the
            # dashboard's "recent orders" table and trade_records.csv can show
            # the Lighter leg. Without this link, handle_lighter_fill_update has
            # no trade_key to update because place_lighter_order (which used to
            # populate lighter_client_order_to_trade_key) was removed.
            if rfq_id and created_record is not None:
                co_id, tx_hash, lighter_avg_px, lighter_filled_at = self.auto_trader.peek_lighter_info(rfq_id)
                if co_id is not None:
                    async with self._record_lock:
                        created_record.lighter_side = "SELL" if side == "buy" else "BUY"
                        created_record.lighter_client_order_id = co_id
                        created_record.lighter_tx_hash = tx_hash
                        self.lighter_client_order_to_trade_key[co_id] = key
                        if lighter_avg_px is not None:
                            created_record.lighter_fill_price = lighter_avg_px
                        if lighter_filled_at is not None:
                            created_record.lighter_fill_ts_iso = lighter_filled_at

    async def trade_loop(self) -> None:
        while not self.stop_flag:
            current_asset = await self.detect_current_variational_asset()
            if current_asset:
                if current_asset == self.variational_ticker:
                    self._asset_switch_candidate = None
                    self._asset_switch_candidate_hits = 0
                else:
                    if current_asset == self._asset_switch_candidate:
                        self._asset_switch_candidate_hits += 1
                    else:
                        self._asset_switch_candidate = current_asset
                        self._asset_switch_candidate_hits = 1

                    if self._asset_switch_candidate_hits >= ASSET_SWITCH_CONFIRM_TICKS:
                        await self.activate_asset(current_asset, reason="quote_stream_debounced")
                        self._asset_switch_candidate = None
                        self._asset_switch_candidate_hits = 0
            else:
                self._asset_switch_candidate = None
                self._asset_switch_candidate_hits = 0

            events = await self.runtime.monitor.get_trade_events_since(self.trade_event_cursor, limit=500)
            for event in events:
                self.trade_event_cursor = max(self.trade_event_cursor, int(event.get("event_seq", 0) or 0))
                await self.process_variational_trade_event(event)
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    def _fmt_price(self, value: Decimal | None) -> str:
        if value is None:
            return "-"
        return format(value, "f")

    @staticmethod
    def _direction_labels(side: str) -> tuple[str, str]:
        side_n = side.strip().lower()
        if side_n == "buy":
            return "做多 Var / 做空 Lighter", "Long Var / Short Lighter"
        if side_n == "sell":
            return "做空 Var / 做多 Lighter", "Short Var / Long Lighter"
        side_u = side_n.upper() if side_n else "-"
        return side_u, side_u

    def _fmt_pct(self, value: Decimal | None) -> str:
        if value is None:
            return "-"
        return f"{value:.4f}%"

    def _fmt_signal_pct(
        self,
        current: Decimal | None,
        book_spread_baseline: Decimal | None,
        median_5m: float | None,
        median_30m: float | None,
        median_1h: float | None,
    ) -> str:
        if current is None:
            return "-"
        if book_spread_baseline is None:
            color = "red"
            return f"[{color}]{self._fmt_pct(current)}[/{color}]"

        adjusted = current - book_spread_baseline
        adjusted_f = float(adjusted)
        thresholds = [v for v in (median_5m, median_30m, median_1h) if v is not None]
        is_green = any(adjusted_f > threshold for threshold in thresholds)
        color = "green" if is_green else "red"
        return f"[{color}]{self._fmt_pct(current)}[/{color}]"

    @staticmethod
    def _fill_diff_by_direction(
        side: str,
        var_fill_price: Decimal | None,
        lighter_fill_price: Decimal | None,
    ) -> tuple[Decimal | None, Decimal | None]:
        side_n = side.strip().lower()
        if side_n == "buy":
            # Long Var / Short Lighter: lighter_fill - var_fill
            diff = spread_value(var_fill_price, lighter_fill_price)
            pct = spread_percent(diff, var_fill_price)
            return diff, pct
        if side_n == "sell":
            # Short Var / Long Lighter: var_fill - lighter_fill
            diff = spread_value(lighter_fill_price, var_fill_price)
            pct = spread_percent(diff, lighter_fill_price)
            return diff, pct
        diff = spread_value(lighter_fill_price, var_fill_price)
        pct = spread_percent(diff, var_fill_price)
        return diff, pct

    @staticmethod
    def _decimal_as_float(value: Decimal | None) -> float | None:
        if value is None:
            return None
        return float(value)

    @staticmethod
    def _fmt_median_pct(value: float | None) -> str:
        if value is None:
            return "-"
        return f"{value:.4f}%"

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
                "triggered_cycle_id": None,
                "skip_reason": None,
            }
        )

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
        # Edges consumed here so _prev_*_green advance; journaling/dispatch added in later tasks.
        for direction, event in self.signal_engine.detect_edges():
            self._emit_signal_edge(direction, state, event=event)
            if event == "signal_turned_green" and self.auto_trader is not None:
                asyncio.create_task(self.auto_trader.on_green_edge(direction, state))

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

        is_zh = self.args.lang == "zh"
        header_title = "Variational <-> Lighter"
        auto_hedge_label = "自动对冲" if is_zh else "auto_hedge"
        auto_hedge_on = "开" if is_zh else "ON"
        auto_hedge_off = "关" if is_zh else "OFF"
        quote_title = "最优买一 / 卖一" if is_zh else "Best Bid / Ask"
        col_exchange = "交易所" if is_zh else "Exchange"
        col_bid = "买一" if is_zh else "Bid"
        col_ask = "卖一" if is_zh else "Ask"
        col_book_spread = "买卖价差" if is_zh else "Bid/Ask Spread"
        col_book_spread_pct = "买卖价差%" if is_zh else "Bid/Ask Spread %"
        spread_title = "价差" if is_zh else "Spreads"
        col_metric = "指标" if is_zh else "Metric"
        col_formula = "公式" if is_zh else "Formula"
        col_value_pct = "当前值%" if is_zh else "Value %"
        col_median_5m_pct = "5分钟中位数%" if is_zh else "Median 5m %"
        col_median_30m_pct = "30分钟中位数%" if is_zh else "Median 30m %"
        col_median_1h_pct = "1小时中位数%" if is_zh else "Median 1h %"
        metric_long_short = "做多 Var / 做空 Lighter" if is_zh else "Long Var / Short Lighter"
        metric_short_long = "做空 Var / 做多 Lighter" if is_zh else "Short Var / Long Lighter"
        orders_title = "最近订单（最新在前）" if is_zh else "Recent Orders (latest first)"
        col_trade_id = "订单ID" if is_zh else "Trade ID"
        col_side = "方向" if is_zh else "Side"
        col_qty = "数量" if is_zh else "Qty"
        col_var_fill_px = "Var 成交价" if is_zh else "Var Fill Px"
        col_lighter_fill_px = "Lighter 成交价" if is_zh else "Lighter Fill Px"
        col_fill_diff = "成交价差(按方向)" if is_zh else "Fill Diff (Directional)"
        col_fill_diff_pct = "成交价差%(按方向)" if is_zh else "Fill Diff % (Directional)"
        no_orders_text = "（暂无订单）" if is_zh else "(no tracked orders yet)"
        variational_label = "Variational"
        lighter_label = "Lighter"
        hedge_color = "green" if True else "red"
        hedge_text = auto_hedge_on if True else auto_hedge_off

        header = Panel(
            f"[bold]{header_title}[/bold] | [bold]{self.ticker}[/bold] | "
            f"[bold {hedge_color}]{auto_hedge_label}={hedge_text}[/] | {utc_now()}",
            border_style="cyan",
        )

        quote_table = Table(title=quote_title, show_header=True, expand=True)
        quote_table.add_column(col_exchange, style="bold")
        quote_table.add_column(col_bid, justify="right")
        quote_table.add_column(col_ask, justify="right")
        quote_table.add_column(col_book_spread, justify="right")
        quote_table.add_column(col_book_spread_pct, justify="right")
        quote_table.add_row(
            f"{variational_label} ({quote_asset or self.variational_ticker})",
            self._fmt_price(var_bid),
            self._fmt_price(var_ask),
            self._fmt_price(var_book_spread),
            self._fmt_pct(var_book_spread_pct),
        )
        quote_table.add_row(
            lighter_label,
            self._fmt_price(lighter_bid),
            self._fmt_price(lighter_ask),
            self._fmt_price(lighter_book_spread),
            self._fmt_pct(lighter_book_spread_pct),
        )

        spread_table = Table(title=spread_title, show_header=True, expand=True)
        spread_table.add_column(col_metric, style="bold")
        spread_table.add_column(col_formula)
        spread_table.add_column(col_value_pct, justify="right")
        spread_table.add_column(col_median_5m_pct, justify="right")
        spread_table.add_column(col_median_30m_pct, justify="right")
        spread_table.add_column(col_median_1h_pct, justify="right")
        spread_table.add_row(
            metric_long_short,
            "lighter_bid - var_ask",
            self._fmt_signal_pct(
                long_var_short_lighter_pct,
                spread_color_baseline,
                long_pct_median_5m,
                long_pct_median_30m,
                long_pct_median_1h,
            ),
            self._fmt_median_pct(long_pct_median_5m),
            self._fmt_median_pct(long_pct_median_30m),
            self._fmt_median_pct(long_pct_median_1h),
        )
        spread_table.add_row(
            metric_short_long,
            "var_bid - lighter_ask",
            self._fmt_signal_pct(
                short_var_long_lighter_pct,
                spread_color_baseline,
                short_pct_median_5m,
                short_pct_median_30m,
                short_pct_median_1h,
            ),
            self._fmt_median_pct(short_pct_median_5m),
            self._fmt_median_pct(short_pct_median_30m),
            self._fmt_median_pct(short_pct_median_1h),
        )

        orders_table = Table(title=orders_title, show_header=True, expand=True)
        orders_table.add_column(col_trade_id)
        orders_table.add_column(col_side)
        orders_table.add_column(col_qty, justify="right")
        orders_table.add_column(col_var_fill_px, justify="right")
        orders_table.add_column(col_lighter_fill_px, justify="right")
        orders_table.add_column(col_fill_diff, justify="right")
        orders_table.add_column(col_fill_diff_pct, justify="right")

        if not rows:
            orders_table.add_row(
                no_orders_text,
                "-",
                "-",
                "-",
                "-",
                "-",
                "-",
            )
        else:
            for row in rows:
                payload = row.to_payload()
                trade_display = row.trade_id[:10] if row.trade_id else row.trade_key[:10]
                fill_diff, fill_diff_pct = self._fill_diff_by_direction(
                    row.side,
                    row.var_fill_price,
                    row.lighter_fill_price,
                )
                side_zh, side_en = self._direction_labels(row.side)
                side_display = side_zh if is_zh else side_en
                orders_table.add_row(
                    trade_display,
                    side_display,
                    self._fmt_price(row.qty),
                    payload["variational_filled_price"] or "-",
                    payload["lighter_filled_price"] or "-",
                    self._fmt_price(fill_diff),
                    self._fmt_pct(fill_diff_pct),
                )

        stats = self.auto_trader.snapshot() if self.auto_trader is not None else None
        if stats is not None:
            dropped = (
                self.signal_journal.dropped_count
                + self.events_journal.dropped_count
                + self.cycles_journal.dropped_count
            )
            frozen_suffix = "" if not stats.frozen else f"  \u26a0 FROZEN[{stats.frozen_reason}]"
            if dropped == 0:
                drop_suffix = ""
            else:
                drop_suffix = f"  \u26a0 \u65e5\u5fd7\u4e22\u5f03 {dropped}" if is_zh else f"  \u26a0 journal drops {dropped}"
            if is_zh:
                stats_line = (
                    f"\u4eca\u65e5 {stats.trades_today} \u7b14 | \u5931\u8d25 {stats.failures_today} | "
                    f"\u7d2f\u8ba1\u51c0\u5229(\u540d\u4e49) {format(stats.cumulative_realized_net_notional, 'f')} | "
                    f"\u5747\u6ed1\u70b9 bps var={stats.avg_var_slippage_bps:.1f} lighter={stats.avg_lighter_slippage_bps:.1f} | "
                    f"\u7194\u65ad: {'OK' if not stats.frozen else 'FROZEN'}{frozen_suffix}{drop_suffix}"
                )
            else:
                stats_line = (
                    f"today {stats.trades_today} trades | fail {stats.failures_today} | "
                    f"cum net (notional) {format(stats.cumulative_realized_net_notional, 'f')} | "
                    f"avg slip bps var={stats.avg_var_slippage_bps:.1f} lighter={stats.avg_lighter_slippage_bps:.1f} | "
                    f"breaker: {'OK' if not stats.frozen else 'FROZEN'}{frozen_suffix}{drop_suffix}"
                )
            stats_panel = Panel(stats_line, border_style=("red" if stats.frozen else "green"))
            account_panel = await self._render_account_panel(is_zh)
            if account_panel is not None:
                return Group(header, quote_table, spread_table, orders_table, stats_panel, account_panel)
            return Group(header, quote_table, spread_table, orders_table, stats_panel)
        return Group(header, quote_table, spread_table, orders_table)

    async def _render_account_panel(self, is_zh: bool):
        var_bal, var_upnl_ws = await self.read_variational_account_snapshot()
        lig_bal, lig_upnl_ws = await self.read_lighter_account_snapshot()

        # Local uPnL estimate: qty_signed × (current_mark - avg_entry).
        # Preferred over WS upnl because WS only pushes on account
        # changes — if market moves without a trade, WS upnl goes stale.
        # WS upnl is used as fallback when position accounting isn't ready.
        var_upnl: Decimal | None = var_upnl_ws
        lig_upnl: Decimal | None = lig_upnl_ws
        if self.auto_trader is not None:
            positions = self.auto_trader.get_positions()
            var_bid, var_ask, _ = await self.get_variational_best_bid_ask(self.variational_ticker)
            async with self.lighter_order_book_lock:
                lb, la = self.lighter_best_bid, self.lighter_best_ask
            var_qty, var_avg = positions.get("var", (Decimal("0"), Decimal("0")))
            lig_qty, lig_avg = positions.get("lighter", (Decimal("0"), Decimal("0")))
            if var_qty != 0 and var_avg != 0 and var_bid is not None and var_ask is not None:
                var_mark = (var_bid + var_ask) / Decimal("2")
                var_upnl = var_qty * (var_mark - var_avg)
            elif var_qty == 0:
                var_upnl = Decimal("0")
            if lig_qty != 0 and lig_avg != 0 and lb is not None and la is not None:
                lig_mark = (lb + la) / Decimal("2")
                lig_upnl = lig_qty * (lig_mark - lig_avg)
            elif lig_qty == 0:
                lig_upnl = Decimal("0")

        def total(bal: Decimal | None, upnl: Decimal | None) -> Decimal | None:
            if bal is None or upnl is None:
                return None
            return bal + upnl

        def fmt_money(v: Decimal | None) -> str:
            if v is None:
                return "-"
            return f"{v:.4f}"

        def fmt_delta(v: Decimal | None) -> str:
            if v is None:
                return "-"
            sign = "+" if v >= 0 else ""
            return f"{sign}{v:.4f}"

        var_total = total(var_bal, var_upnl)
        lig_total = total(lig_bal, lig_upnl)
        var_delta = (var_total - self.startup_var_total) if var_total is not None and self.startup_var_total is not None else None
        lig_delta = (lig_total - self.startup_lighter_total) if lig_total is not None and self.startup_lighter_total is not None else None
        grand_total = (var_total + lig_total) if var_total is not None and lig_total is not None else None
        startup_grand = (
            (self.startup_var_total + self.startup_lighter_total)
            if self.startup_var_total is not None and self.startup_lighter_total is not None
            else None
        )
        grand_delta = (
            (grand_total - startup_grand)
            if grand_total is not None and startup_grand is not None
            else None
        )

        # Nothing to show yet
        if var_total is None and lig_total is None and grand_total is None:
            return None

        # Cumulative per-venue traded notional from TraderStats.
        stats = self.auto_trader.snapshot() if self.auto_trader is not None else None
        var_vol = stats.var_volume_usd if stats is not None else None
        lig_vol = stats.lighter_volume_usd if stats is not None else None
        grand_vol = (
            (var_vol + lig_vol)
            if var_vol is not None and lig_vol is not None
            else None
        )

        if is_zh:
            title = f"账户(启动 {self.startup_ts_iso or '-'})"
            col_venue = "平台"; col_bal = "余额"; col_upnl = "浮盈"
            col_total = "总值"; col_delta = "Δ vs 启动"; col_vol = "累计交易量"
            row_grand = "合计"
        else:
            title = f"Account (anchored at {self.startup_ts_iso or '-'})"
            col_venue = "Venue"; col_bal = "Balance"; col_upnl = "uPnL"
            col_total = "Total"; col_delta = "Δ vs start"; col_vol = "Cum. Volume"
            row_grand = "Grand Total"

        tbl = Table(title=title, show_header=True, expand=True)
        tbl.add_column(col_venue, style="bold")
        tbl.add_column(col_bal, justify="right")
        tbl.add_column(col_upnl, justify="right")
        tbl.add_column(col_total, justify="right")
        tbl.add_column(col_delta, justify="right")
        tbl.add_column(col_vol, justify="right")
        tbl.add_row("Variational", fmt_money(var_bal), fmt_money(var_upnl),
                    fmt_money(var_total), fmt_delta(var_delta), fmt_money(var_vol))
        tbl.add_row("Lighter", fmt_money(lig_bal), fmt_money(lig_upnl),
                    fmt_money(lig_total), fmt_delta(lig_delta), fmt_money(lig_vol))
        tbl.add_row(row_grand, "", "", fmt_money(grand_total), fmt_delta(grand_delta), fmt_money(grand_vol))

        border = "green" if (grand_delta is not None and grand_delta >= 0) else ("red" if grand_delta is not None else "cyan")
        return Panel(tbl, border_style=border)

    async def export_trade_records_csv(self) -> None:
        if self.trade_records_csv_file is None:
            return

        async with self._record_lock:
            keys = list(self.record_order)
            rows: list[dict[str, Any]] = []
            for key in keys:
                record = self.records.get(key)
                if record is None:
                    continue
                payload = record.to_payload()
                fill_diff, fill_diff_pct = self._fill_diff_by_direction(
                    record.side,
                    record.var_fill_price,
                    record.lighter_fill_price,
                )
                side_zh, side_en = self._direction_labels(record.side)
                rows.append(
                    {
                        "trade_key": record.trade_key,
                        "trade_id": record.trade_id,
                        "asset": record.asset,
                        "side_raw": record.side,
                        "direction_zh": side_zh,
                        "direction_en": side_en,
                        "qty": decimal_to_str(record.qty),
                        "variational_filled_price": payload["variational_filled_price"],
                        "variational_filled_at": payload["variational_filled_at"],
                        "lighter_order_side": payload["lighter_order_side"],
                        "lighter_client_order_id": payload["lighter_client_order_id"],
                        "lighter_filled_price": payload["lighter_filled_price"],
                        "lighter_filled_at": payload["lighter_filled_at"],
                        "fill_diff_var_minus_lighter": decimal_to_str(fill_diff),
                        "fill_diff_pct_vs_var": decimal_to_str(fill_diff_pct),
                        "auto_hedge_enabled": payload["auto_hedge_enabled"],
                        "hedge_error": payload["hedge_error"],
                        "last_variational_status": payload["last_variational_status"],
                    }
                )

        snapshot_sig = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        if snapshot_sig == self._trade_records_snapshot_sig:
            return

        fieldnames = [
            "trade_key",
            "trade_id",
            "asset",
            "side_raw",
            "direction_zh",
            "direction_en",
            "qty",
            "variational_filled_price",
            "variational_filled_at",
            "lighter_order_side",
            "lighter_client_order_id",
            "lighter_filled_price",
            "lighter_filled_at",
            "fill_diff_var_minus_lighter",
            "fill_diff_pct_vs_var",
            "auto_hedge_enabled",
            "hedge_error",
            "last_variational_status",
        ]
        async with self._trade_csv_write_lock:
            if snapshot_sig == self._trade_records_snapshot_sig:
                return
            await asyncio.to_thread(self._write_csv_rows, self.trade_records_csv_file, fieldnames, rows)
            self._trade_records_snapshot_sig = snapshot_sig

    @staticmethod
    def _write_csv_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.tmp")
        with tmp_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp_path, path)

    async def dashboard_loop(self) -> None:
        refresh_interval = DASHBOARD_REFRESH_SECONDS
        refresh_per_second = max(1, int(round(1.0 / refresh_interval)))
        initial_render = await self.render_dashboard()
        await self.export_trade_records_csv()
        with Live(
            initial_render,
            console=self.dashboard_console,
            refresh_per_second=refresh_per_second,
            screen=True,
        ) as live:
            while not self.stop_flag:
                await asyncio.sleep(refresh_interval)
                live.update(await self.render_dashboard())
                await self.export_trade_records_csv()

    async def run(self) -> None:
        self.logger.info(
            "Startup config: qty=%s throttle_s=%.1f max_trades/day=%d "
            "signal_strict=%s leg_timeout_s=%.1f var_order_timeout_ms=%d lang=%s",
            self.auto_trader_config.qty, self.auto_trader_config.throttle_seconds,
            self.auto_trader_config.max_trades_per_day,
            self.args.signal_strict, self.auto_trader_config.leg_settle_timeout_sec,
            self.auto_trader_config.var_order_timeout_ms, self.args.lang,
        )
        self.setup_signal_handlers()
        await self.runtime.start()
        await self.signal_journal.start()
        await self.events_journal.start()
        await self.cycles_journal.start()
        self.print_startup_next_steps()
        self.logger.info(
            "Listening for Variational forwarder events on ws://%s:%s and ws://%s:%s",
            FORWARDER_HOST,
            FORWARDER_WS_PORT,
            FORWARDER_HOST,
            FORWARDER_REST_PORT,
        )

        await self.wait_for_variational_ready()
        self.logger.info("Variational heartbeat is live")
        self.initialize_lighter_client()
        initial_asset = await self.wait_for_ticker_resolution()
        await self.activate_asset(initial_asset, reason="startup")

        self.cmd_client = VariationalCmdClient(FORWARDER_HOST, FORWARDER_COMMAND_PORT, self.logger)
        await self.cmd_client.start()
        try:
            await self.cmd_client.wait_ready(timeout=10)
        except asyncio.TimeoutError:
            self.logger.warning("CmdClient did not connect within 10s; orders will fail until the broker is up.")

        var_placer = VariationalPlacerImpl(
            self.cmd_client,
            max_slippage=self.args.var_max_slippage_bps / 10000.0,
            logger=self.logger,
        )
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
        self.logger.info(
            "AutoTrader initialized: qty=%s throttle=%.1fs max/day=%d position_limit=%s",
            self.auto_trader_config.qty, self.auto_trader_config.throttle_seconds,
            self.auto_trader_config.max_trades_per_day,
            self.auto_trader_config.position_limit,
        )

        self.trade_event_cursor = await self.runtime.monitor.get_latest_trade_event_seq()
        self.logger.info("Tracking new Variational trade events from seq>%s", self.trade_event_cursor)

        # Wait briefly for both account streams to push their first data so we
        # can anchor a startup equity snapshot. Each WS pushes an initial
        # message within 1-2s; give it 5s and accept None if either is slow.
        await self._capture_startup_account_snapshot(timeout=5.0)

        self.trade_task = asyncio.create_task(self.trade_loop())
        self.dashboard_task = asyncio.create_task(self.dashboard_loop())

        while not self.stop_flag:
            await asyncio.sleep(0.25)

    async def close(self) -> None:
        self.stop_flag = True

        if self.dashboard_task and not self.dashboard_task.done():
            self.dashboard_task.cancel()
            await asyncio.gather(self.dashboard_task, return_exceptions=True)

        if self.trade_task and not self.trade_task.done():
            self.trade_task.cancel()
            await asyncio.gather(self.trade_task, return_exceptions=True)

        if self.lighter_ws_task and not self.lighter_ws_task.done():
            self.lighter_ws_task.cancel()
            await asyncio.gather(self.lighter_ws_task, return_exceptions=True)

        await self.signal_journal.stop()
        await self.events_journal.stop()
        await self.cycles_journal.stop()
        if self.cmd_client is not None:
            await self.cmd_client.stop()

        if self.lighter_client is not None:
            close_method = getattr(self.lighter_client, "close", None)
            if callable(close_method):
                with contextlib.suppress(Exception):
                    close_result = close_method()
                    if asyncio.iscoroutine(close_result):
                        await close_result

        await self.runtime.stop()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Cross-venue spread trader. Auto-triggers on green-light signal; writes per-cycle P&L attribution."
    )
    parser.add_argument("--qty", required=True, type=str, help="Per-cycle base asset qty (required). Example: 0.01")
    parser.add_argument("--throttle-seconds", type=float, default=3.0)
    parser.add_argument("--max-trades-per-day", type=int, default=200)
    parser.add_argument("--position-limit", type=str, default="0",
                        help="Net directional position ceiling (signed qty). At |net|>=limit "
                             "we enter reduce-only mode until |net| drops to 50%% of limit. "
                             "0 means auto-derive as 2x qty.")
    parser.add_argument("--signal-strict", action="store_true",
                        help="Require adjusted > max(5m,30m,1h) instead of any().")
    parser.add_argument("--var-order-timeout-ms", type=int, default=5000)
    parser.add_argument("--leg-settle-timeout-sec", type=float, default=10.0)
    parser.add_argument("--debug-var-payload", action="store_true",
                        help="Write full Variational request/response bodies to order_events.jsonl.")
    parser.add_argument("--var-max-slippage-bps", type=float, default=100.0,
                        help="Variational market order max_slippage in bps (1%% = 100). Passed to /api/orders/new/market.")
    parser.add_argument("--lang", choices=["zh", "en"], default="zh")
    return parser.parse_args()


async def _amain() -> None:
    load_dotenv()
    args = parse_args()
    runtime = VariationalToLighterRuntime(args)
    try:
        await runtime.run()
    finally:
        await runtime.close()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
