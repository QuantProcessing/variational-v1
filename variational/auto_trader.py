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
        import time
        cycle_id = _new_cycle_id()
        now_iso = _utc_now_iso()
        now_mono = time.monotonic()

        ds = state.long_direction if direction == "long_var_short_lighter" else state.short_direction
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
            expected_net_pct = expected_gross_pct - (total_fee_bps / 100.0)

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

        lighter_bid, lighter_ask = await self.lighter.best_bid_ask()
        if lighter_bid is None or lighter_ask is None:
            cycle.lighter_leg.error = "lighter_book_empty"
            cycle.lighter_leg.terminal = True
            cycle.reason_codes.append("lighter_book_empty")
            self._register_failure("lighter_book_empty")
            self.events.emit({
                "ts": _utc_now_iso(), "event": "cycle_error",
                "cycle_id": cycle.cycle_id, "side": "lighter",
                "error_msg": "lighter_book_empty",
            })
            limit_px = None
        else:
            slippage = Decimal(str(self.config.hedge_slippage_bps)) / Decimal("10000")
            if lighter_side == "BUY":
                limit_px = lighter_ask * (Decimal("1") + slippage)
            else:
                limit_px = lighter_bid * (Decimal("1") - slippage)
        cycle.lighter_leg.limit_px = limit_px

        tasks = [asyncio.create_task(self._fire_var_leg(cycle, var_side))]
        if not cycle.lighter_leg.terminal:
            tasks.append(asyncio.create_task(self._fire_lighter_leg(cycle, lighter_side)))
        await asyncio.gather(*tasks, return_exceptions=True)

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

    async def on_variational_fill(self, trade_id: str, fill_px: Decimal, fill_qty: Decimal) -> None:
        """Route a Variational fill (from monitor) to an open cycle by trade_id."""
        async with self._lock:
            target: TradeCycle | None = None
            for cycle in self._open_cycles.values():
                if trade_id in cycle.var_leg.trade_ids:
                    target = cycle
                    break
        if target is None:
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
        prior_filled = leg.filled_qty
        leg.filled_qty += fill_qty
        if leg.avg_fill_px is None:
            leg.avg_fill_px = fill_px
        else:
            leg.avg_fill_px = ((leg.avg_fill_px * prior_filled) + (fill_px * fill_qty)) / leg.filled_qty
        leg.partial_fill_count += 1
        leg.filled_at = _utc_now_iso()
        self.events.emit({
            "ts": leg.filled_at, "event": "var_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })
        if leg.filled_qty >= leg.requested_qty:
            leg.terminal = True

    def _apply_lighter_fill(self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal) -> None:
        leg = cycle.lighter_leg
        prior_filled = leg.filled_qty
        leg.filled_qty += fill_qty
        if leg.avg_fill_px is None:
            leg.avg_fill_px = fill_px
        else:
            leg.avg_fill_px = ((leg.avg_fill_px * prior_filled) + (fill_px * fill_qty)) / leg.filled_qty
        leg.partial_fill_count += 1
        leg.filled_at = _utc_now_iso()
        self.events.emit({
            "ts": leg.filled_at, "event": "lighter_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })
        if leg.filled_qty >= leg.requested_qty:
            leg.terminal = True

    async def _settle_cycle_when_done(self, cycle: TradeCycle) -> None:
        deadline = asyncio.get_running_loop().time() + self.config.leg_settle_timeout_sec
        while asyncio.get_running_loop().time() < deadline:
            if cycle.var_leg.terminal and cycle.lighter_leg.terminal:
                break
            await asyncio.sleep(0.2)

        var_ok = cycle.var_leg.filled_qty >= cycle.var_leg.requested_qty
        lighter_ok = cycle.lighter_leg.filled_qty >= cycle.lighter_leg.requested_qty

        if var_ok and lighter_ok:
            cycle.status = "fully_filled"
            self.stats.consecutive_failures = 0
        elif not cycle.var_leg.error and not cycle.lighter_leg.error and (
            cycle.var_leg.filled_qty > 0 or cycle.lighter_leg.filled_qty > 0
        ):
            cycle.status = "partial"
            if not var_ok:
                cycle.reason_codes.append(
                    "var_depth_shortfall" if cycle.var_leg.filled_qty > 0 else "var_timeout"
                )
            if not lighter_ok:
                cycle.reason_codes.append(
                    "lighter_depth_shortfall" if cycle.lighter_leg.filled_qty > 0 else "lighter_timeout"
                )
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
        if s is not None:
            direction_state = s.long_direction if cycle.direction == "long_var_short_lighter" else s.short_direction
            signal_block = {
                "adjusted_pct": _dec_str(direction_state.adjusted_pct),
                "baseline_pct": _dec_str(s.book_spread_baseline_pct),
                "median_5m_pct": direction_state.median_5m_pct,
                "median_30m_pct": direction_state.median_30m_pct,
                "median_1h_pct": direction_state.median_1h_pct,
                "var_bid": _dec_str(s.var_bid),
                "var_ask": _dec_str(s.var_ask),
                "lighter_bid": _dec_str(s.lighter_bid),
                "lighter_ask": _dec_str(s.lighter_ask),
            }
        else:
            signal_block = {
                "adjusted_pct": None, "baseline_pct": None,
                "median_5m_pct": None, "median_30m_pct": None, "median_1h_pct": None,
                "var_bid": None, "var_ask": None, "lighter_bid": None, "lighter_ask": None,
            }
        return {
            "cycle_id": cycle.cycle_id,
            "triggered_at": cycle.opened_at,
            "closed_at": cycle.closed_at,
            "duration_ms": duration_ms,
            "asset": cycle.asset,
            "direction": cycle.direction,
            "status": cycle.status,
            "signal": signal_block,
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
