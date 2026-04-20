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
    max_net_imbalance: Decimal = Decimal("0")
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
    status: str = "opening"
    quote_drift_ms: int | None = None
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


# ---------- Adapter Protocols + their result types ----------

from typing import Awaitable, Callable, Protocol


class LighterAdapter(Protocol):
    async def best_bid_ask(self) -> tuple[Decimal | None, Decimal | None]: ...
    async def place_order(
        self,
        side: str,
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
        side: str,
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


# ---------- AutoTrader class skeleton (with gates; _fire stubbed) ----------


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
            # Consecutive failures / frozen do NOT reset on day rollover — user must restart.

    # ---------- fire ----------

    async def _fire(self, direction: str, state: SignalState) -> None:
        # Body implemented in Task 3.3.
        raise NotImplementedError
