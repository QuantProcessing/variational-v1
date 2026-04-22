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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Optional

from variational.journal import EventJournal
from variational.signal import SignalEngine, SignalState

# Fraction of Lighter top-of-book qty to consume per close attempt. Leaving
# 70% on the book means competitors eating the level ahead of us only eats
# our 30%, not pushing us to deeper/worse levels. Successive WS ticks will
# re-fire until position is fully drained.
CLOSE_BOOK_FRACTION = Decimal("0.3")

# Quantize order qty to 2 decimals (ROUND_DOWN) before dispatch. Both
# Variational and Lighter HYPE markets use 0.01 tick; identical quantization
# on both sides prevents the "3.87 vs 3.833" symmetry break where tracker
# asks for more than one venue can reduce_only close.
QTY_QUANTUM = Decimal("0.01")

# Lighter rejects below-min-notional orders (code 21706 "invalid order base
# or quote amount"). For HYPE at ~$40 the floor is ~0.2-0.3 qty. If a paired
# close leaves a sub-min residual on one venue we stop retrying — the
# exchange just keeps bouncing it — and exit close_mode so signal fires can
# resume. The dust stays on-exchange; user cleans up manually on the venue UI.
RESIDUAL_DUST_QTY = Decimal("0.5")


def quantize_qty(qty: Decimal) -> Decimal:
    return qty.quantize(QTY_QUANTUM, rounding=ROUND_DOWN)


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
    position_limit: Decimal = Decimal("0")
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
    # Expected cross-spread % at fire time. Both venues are zero-fee, so
    # expected_net == expected_gross — we only carry one field.
    expected_net_pct: float | None


@dataclass(slots=True)
class LegState:
    placed_at: str | None = None
    filled_at: str | None = None
    requested_qty: Decimal = Decimal("0")
    filled_qty: Decimal = Decimal("0")
    avg_fill_px: Decimal | None = None
    error: str | None = None
    terminal: bool = False


@dataclass(slots=True)
class VarLegState(LegState):
    api_latency_ms: int | None = None
    # rfq_id returned by /api/orders/new/market. This is the correlation key
    # that Var /events WS echoes back as `source_rfq`. Set AFTER the HTTP
    # response; fills that arrive before that are buffered and replayed.
    rfq_id: str | None = None
    # Variational's fill-level trade_id from /events WS. Populated on the
    # first Var fill for this cycle; purely for display/diagnostics.
    trade_id: str | None = None


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
    reason_codes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TraderStats:
    trades_today: int = 0
    failures_today: int = 0
    consecutive_failures: int = 0
    cumulative_realized_net_notional: Decimal = Decimal("0")
    avg_var_slippage_bps: float = 0.0
    avg_lighter_slippage_bps: float = 0.0
    # Cumulative notional (fill_px × fill_qty) per venue across all fills
    # since process start — useful for exchange volume-based reward tiers.
    # Not reset on UTC day rollover.
    var_volume_usd: Decimal = Decimal("0")
    lighter_volume_usd: Decimal = Decimal("0")
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
        client_order_id: int,
        reduce_only: bool = False,
    ) -> "LighterPlaceResult": ...
    async def get_position(self, symbol: str) -> Decimal | None:
        """Return signed Lighter position qty for `symbol` (+long, -short).
        None on fetch failure."""
        ...


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

    async def place_close_order(
        self,
        side: str,
        qty: Decimal,
        asset: str,
        timeout_ms: int,
    ) -> "VarPlaceResult": ...

    async def get_position(self, asset: str) -> Decimal | None:
        """Return signed Variational position qty for `asset` (+long, -short).
        None on fetch failure."""
        ...


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

        # Var WS can push fill events before /api/orders/new/market HTTP returns
        # the rfq_id we use to correlate. Buffer these unrouted fills keyed by
        # rfq_id; _fire_var_leg drains the matching entry once HTTP returns.
        # Each list entry carries (fill_px, fill_qty, trade_id, ts_monotonic)
        # so stale entries (close-order fills, failed cycles) can be GC'd.
        self._pending_var_fills: dict[str, list[tuple[Decimal, Decimal, str | None, float]]] = {}

        # Recent cycles (open + closed) for dashboard/CSV rendering. Kept as a
        # deque with bounded length so the dashboard can iterate newest-first
        # without querying _open_cycles (opens show here while in-flight and
        # remain after settlement).
        self._recent_cycles: deque[TradeCycle] = deque(maxlen=500)

        # Hysteresis: once |net_qty| reaches position_limit we enter close mode
        # (signal fires paused; WS ticks drive active reduce-only closes when
        # close PnL >= 0). Exit close mode only when BOTH venues are fully
        # flat — resuming while still carrying inventory just cycles back up
        # to the limit and pays the open→close slippage round-trip again.
        self._close_mode = False
        self._close_in_progress = False

        # Per-venue position accounting for close PnL computation.
        # _*_pos_qty is signed (+long, -short). avg is cost basis (unsigned).
        self._var_pos_qty: Decimal = Decimal("0")
        self._var_pos_avg: Decimal = Decimal("0")
        self._lighter_pos_qty: Decimal = Decimal("0")
        self._lighter_pos_avg: Decimal = Decimal("0")

        if self.config.position_limit == 0:
            self.config.position_limit = self.config.qty * Decimal("2")

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

        # Position-limit hysteresis:
        # - normal mode: reject if projected |net| would exceed position_limit
        # - close mode: reject ALL signal fires (closes happen via WS-driven
        #   try_close_on_book_tick, not via the signal).
        if self._close_mode:
            return "close_mode"
        signed = self.config.qty if direction == "long_var_short_lighter" else -self.config.qty
        projected = self._projected_net_qty() + signed
        if abs(projected) > self.config.position_limit:
            return "position_limit_exceeded"
        return None

    def _projected_net_qty(self) -> Decimal:
        """Net signed exposure including in-flight cycles.

        = filled var position + unfilled remainder of open cycles' var legs,
        signed by direction. No separate accumulator; derived on demand.
        """
        net = self._var_pos_qty
        for c in self._open_cycles.values():
            remaining = c.var_leg.requested_qty - c.var_leg.filled_qty
            if remaining <= 0:
                continue
            net += remaining if c.direction == "long_var_short_lighter" else -remaining
        return net

    def _update_mode(self) -> None:
        """Recompute close-mode state from actual filled venue positions.

        Enter at |pos| >= limit; exit only when BOTH venues are flat
        (<= QTY_QUANTUM to ignore dust). Flat-only exit prevents the
        open→close oscillation where we close halfway, re-fire, and pay
        the round-trip slippage repeatedly.
        """
        abs_net = max(abs(self._var_pos_qty), abs(self._lighter_pos_qty))
        limit = self.config.position_limit
        if not self._close_mode and abs_net >= limit:
            self._close_mode = True
            self.logger.warning(
                "Entering close mode: var_pos=%s lighter_pos=%s limit=%s. "
                "Signal fires paused; WS ticks drive reduce-only closes "
                "when close_pnl >= 0 until BOTH venues are flat.",
                self._var_pos_qty, self._lighter_pos_qty, limit,
            )
        elif self._close_mode and abs_net <= QTY_QUANTUM:
            self._close_mode = False
            self.logger.info(
                "Exiting close mode: var_pos=%s lighter_pos=%s. "
                "Position flat; resuming normal cycle dispatch.",
                self._var_pos_qty, self._lighter_pos_qty,
            )

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

        expected_net_pct = float(ds.cross_spread_pct) if ds.cross_spread_pct is not None else None

        plan = TradePlan(
            qty_target=self.config.qty,
            expected_var_fill_px=expected_var_fill,
            expected_lighter_fill_px=expected_lighter_fill,
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
        # Quantize to 2dp so both venues get the identical qty string.
        open_qty = quantize_qty(self.config.qty)
        cycle.var_leg.requested_qty = open_qty
        cycle.lighter_leg.requested_qty = open_qty

        async with self._lock:
            self._open_cycles[cycle_id] = cycle
            self._recent_cycles.appendleft(cycle)
            self._last_fire_monotonic[direction] = now_mono
            self.stats.trades_today += 1
            self._update_mode()

        self.events.emit({
            "ts": now_iso, "event": "cycle_opened", "cycle_id": cycle_id,
            "direction": direction, "asset": cycle.asset,
            "qty_target": _dec_str(self.config.qty),
            "plan": {
                "expected_var_fill_px": _dec_str(expected_var_fill),
                "expected_lighter_fill_px": _dec_str(expected_lighter_fill),
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

        # Pre-assign + pre-register the Lighter client_order_id BEFORE we await
        # place_order. The lighter-sdk's create_order waits for on-chain
        # confirmation (~5s observed) and Lighter's account_orders WS can push
        # the filled event during that window — if _lighter_order_to_cycle
        # hasn't been populated yet the fill is silently dropped. Pre-register
        # so on_lighter_fill can always resolve to a cycle.
        lighter_client_order_id: int | None = None
        if not cycle.lighter_leg.terminal:
            now_mono_ms = int(time.time() * 1000)
            async with self._lock:
                lighter_client_order_id = now_mono_ms
                while lighter_client_order_id in self._lighter_order_to_cycle:
                    lighter_client_order_id += 1
                self._lighter_order_to_cycle[lighter_client_order_id] = cycle.cycle_id
            cycle.lighter_leg.client_order_id = lighter_client_order_id

        tasks = [asyncio.create_task(self._fire_var_leg(cycle, var_side))]
        if not cycle.lighter_leg.terminal and lighter_client_order_id is not None:
            tasks.append(asyncio.create_task(
                self._fire_lighter_leg(cycle, lighter_side, lighter_client_order_id)
            ))
        await asyncio.gather(*tasks, return_exceptions=True)

        asyncio.create_task(self._settle_cycle_when_done(cycle))

    async def _fire_var_leg(self, cycle: TradeCycle, side: str) -> None:
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
                # res.trade_id is actually the rfq_id from /api/orders/new/market.
                # Record it, then drain any fills that arrived before this HTTP
                # returned (Var /events WS often beats the HTTP by several hundred ms).
                if res.trade_id:
                    cycle.var_leg.rfq_id = res.trade_id
                    async with self._lock:
                        buffered = self._pending_var_fills.pop(res.trade_id, None)
                    if buffered:
                        for fill_px, fill_qty, trade_id, _ts in buffered:
                            if trade_id and not cycle.var_leg.trade_id:
                                cycle.var_leg.trade_id = trade_id
                            self._apply_var_fill(cycle, fill_px, fill_qty)
        except Exception as exc:
            cycle.var_leg.error = f"exception: {exc}"
            cycle.var_leg.terminal = True
            self._register_failure("var_exception")
            cycle.reason_codes.append(cycle.var_leg.error)
            self.events.emit({
                "ts": _utc_now_iso(), "event": "cycle_error",
                "cycle_id": cycle.cycle_id, "side": "var", "error_msg": str(exc),
            })

    async def _fire_lighter_leg(self, cycle: TradeCycle, side: str, client_order_id: int) -> None:
        if cycle.lighter_leg.limit_px is None:
            cycle.lighter_leg.error = "no_limit_px"
            cycle.lighter_leg.terminal = True
            self._register_failure("lighter_book_empty")
            cycle.reason_codes.append("lighter_book_empty")
            async with self._lock:
                self._lighter_order_to_cycle.pop(client_order_id, None)
            return
        cycle.lighter_leg.placed_at = _utc_now_iso()
        self.events.emit({
            "ts": cycle.lighter_leg.placed_at, "event": "lighter_place_attempt",
            "cycle_id": cycle.cycle_id, "side": side,
            "qty": _dec_str(cycle.lighter_leg.requested_qty),
            "limit_px": _dec_str(cycle.lighter_leg.limit_px),
            "client_order_id": client_order_id,
        })
        try:
            res = await self.lighter.place_order(
                side=side,
                qty=cycle.lighter_leg.requested_qty,
                limit_px=cycle.lighter_leg.limit_px,
                client_order_id=client_order_id,
            )
            self.events.emit({
                "ts": _utc_now_iso(), "event": "lighter_place_ack",
                "cycle_id": cycle.cycle_id, "ok": res.ok,
                "client_order_id": client_order_id,
                "tx_hash": res.tx_hash, "error": res.error,
            })
            if not res.ok:
                err = res.error or "unknown"
                cycle.lighter_leg.error = err
                cycle.lighter_leg.terminal = True
                err_lc = err.lower()
                # Insufficient margin is a hard stop — keep retrying and we
                # just hammer Lighter's API while positions stay stranded.
                # Trip the breaker immediately rather than accumulating 3
                # consecutive failures.
                if "not enough margin" in err_lc or "code=21739" in err_lc:
                    self._trip_breaker("lighter_insufficient_margin")
                    cycle.reason_codes.append("lighter_insufficient_margin")
                else:
                    self._register_failure("lighter_sign_error")
                    cycle.reason_codes.append(err)
                async with self._lock:
                    self._lighter_order_to_cycle.pop(client_order_id, None)
                return
            cycle.lighter_leg.tx_hash = res.tx_hash
        except Exception as exc:
            cycle.lighter_leg.error = f"exception: {exc}"
            cycle.lighter_leg.terminal = True
            self._register_failure("lighter_exception")
            cycle.reason_codes.append(cycle.lighter_leg.error)
            async with self._lock:
                self._lighter_order_to_cycle.pop(client_order_id, None)
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

    async def on_variational_fill(
        self,
        rfq_id: str | None,
        trade_id: str | None,
        fill_px: Decimal,
        fill_qty: Decimal,
        side: str | None = None,
    ) -> None:
        """Route a Variational fill from /events WS to its cycle via rfq_id.

        Per-venue position accounting runs unconditionally so close-order
        fills (which don't belong to any cycle) still update the tracker.
        Cycle routing is rfq_id-keyed; fills whose rfq_id hasn't been
        assigned yet (HTTP still in flight) are buffered and replayed from
        _fire_var_leg when the HTTP response lands.
        """
        if side is not None:
            signed = fill_qty if side.lower() == "buy" else -fill_qty
            self._update_venue_position("var", signed, fill_px)
            self.stats.var_volume_usd += fill_px * fill_qty
            async with self._lock:
                self._update_mode()

        if not rfq_id:
            return  # close order without correlation — position updated above

        import time as _time
        now_mono = _time.monotonic()
        async with self._lock:
            target: TradeCycle | None = None
            for cycle in self._open_cycles.values():
                if cycle.var_leg.rfq_id == rfq_id:
                    target = cycle
                    break
            if target is None:
                self._pending_var_fills.setdefault(rfq_id, []).append(
                    (fill_px, fill_qty, trade_id, now_mono)
                )
                # GC: drop buffer entries older than 60s. The HTTP-vs-WS race
                # window is < 1s in practice; anything older is an orphaned
                # fill (close order with no cycle, or a cycle whose HTTP
                # errored before it could set rfq_id).
                ttl = 60.0
                stale = [k for k, v in self._pending_var_fills.items()
                         if all((now_mono - entry[3]) > ttl for entry in v)]
                for k in stale:
                    del self._pending_var_fills[k]
                return

        if trade_id and not target.var_leg.trade_id:
            target.var_leg.trade_id = trade_id
        self._apply_var_fill(target, fill_px, fill_qty)

    async def on_lighter_fill(
        self, client_order_id: int, fill_px: Decimal, fill_qty: Decimal, side: str | None = None,
    ) -> None:
        if side is not None:
            signed = fill_qty if side.upper() == "BUY" else -fill_qty
            self._update_venue_position("lighter", signed, fill_px)
            self.stats.lighter_volume_usd += fill_px * fill_qty
            async with self._lock:
                self._update_mode()
        async with self._lock:
            cycle_id = self._lighter_order_to_cycle.get(client_order_id)
            cycle = self._open_cycles.get(cycle_id) if cycle_id else None
        if cycle is None:
            return
        self._apply_lighter_fill(cycle, fill_px, fill_qty)

    def _update_venue_position(self, venue: str, fill_qty_signed: Decimal, fill_px: Decimal) -> None:
        """Classic perpetual position accounting (signed qty + cost basis avg)."""
        if venue == "var":
            old_qty, old_avg = self._var_pos_qty, self._var_pos_avg
        else:
            old_qty, old_avg = self._lighter_pos_qty, self._lighter_pos_avg

        new_qty = old_qty + fill_qty_signed
        if old_qty == 0:
            new_avg = fill_px
        elif (old_qty > 0) == (fill_qty_signed > 0):
            new_avg = (abs(old_qty) * old_avg + abs(fill_qty_signed) * fill_px) / abs(new_qty)
        elif new_qty == 0:
            new_avg = Decimal("0")
        elif (new_qty > 0) != (old_qty > 0):
            new_avg = fill_px     # flipped — new position starts at fill_px
        else:
            new_avg = old_avg     # partial close — basis unchanged

        if venue == "var":
            self._var_pos_qty, self._var_pos_avg = new_qty, new_avg
        else:
            self._lighter_pos_qty, self._lighter_pos_avg = new_qty, new_avg

    # ---------- WS-driven active close (close mode) ----------

    async def try_close_on_book_tick(
        self,
        asset: str,
        var_bid: Decimal | None, var_ask: Decimal | None,
        lighter_bid: Decimal | None, lighter_ask: Decimal | None,
        lighter_bid_qty: Decimal | None, lighter_ask_qty: Decimal | None,
    ) -> None:
        """Called on every Lighter order-book WS update. If we're in close
        mode AND closing now at taker prices would yield non-negative PnL
        AND Lighter has book depth to execute, fire a reduce-only close on
        both venues sized to the Lighter top book qty (or remaining net,
        whichever is smaller).
        """
        if not self._close_mode or self._close_in_progress:
            return
        if var_bid is None or var_ask is None or lighter_bid is None or lighter_ask is None:
            return
        # If one side is 0, paired close is impossible. Kick off a residual
        # handler that queries actual positions from both venues and fires a
        # single-venue reduce-only close on whichever side still has inventory.
        # Exception: if the remaining residual is below the venue's min order
        # size (RESIDUAL_DUST_QTY), Lighter rejects every close attempt with
        # code=21706 and we'd spin here forever. Give up, exit close_mode,
        # let signal fires resume. User cleans the dust manually.
        if self._var_pos_qty == 0 or self._lighter_pos_qty == 0:
            residual = abs(self._var_pos_qty) + abs(self._lighter_pos_qty)
            if residual < RESIDUAL_DUST_QTY:
                async with self._lock:
                    if self._close_mode:
                        self._close_mode = False
                        self.logger.warning(
                            "Residual var=%s lighter=%s below dust threshold %s; "
                            "exiting close_mode. Close manually on the venue UI.",
                            self._var_pos_qty, self._lighter_pos_qty, RESIDUAL_DUST_QTY,
                        )
                return
            if not self._close_in_progress:
                self._close_in_progress = True
                asyncio.create_task(self._handle_close_mode_residual(asset))
            return
        if self._var_pos_avg == 0 or self._lighter_pos_avg == 0:
            return

        if self._var_pos_qty > 0:
            # Long Var, short Lighter. Close = SELL var @ var_bid + BUY lighter @ lighter_ask.
            # To "close" this is equivalent to firing the short_var_long_lighter
            # direction signal — so we gate on that direction's is_green.
            pnl_per_unit = (var_bid - self._var_pos_avg) + (self._lighter_pos_avg - lighter_ask)
            top_qty = lighter_ask_qty
            var_side, lighter_side = "sell", "BUY"
            closable = min(abs(self._var_pos_qty), abs(self._lighter_pos_qty))
            reverse_is_green_dir = "short_var_long_lighter"
        else:
            # Short Var, long Lighter. Close = BUY var @ var_ask + SELL lighter @ lighter_bid.
            # Closing this mirrors firing long_var_short_lighter direction.
            pnl_per_unit = (self._var_pos_avg - var_ask) + (lighter_bid - self._lighter_pos_avg)
            top_qty = lighter_bid_qty
            var_side, lighter_side = "buy", "SELL"
            closable = min(abs(self._var_pos_qty), abs(self._lighter_pos_qty))
            reverse_is_green_dir = "long_var_short_lighter"

        # Use the SignalEngine's reverse-direction green as the primary gate:
        # only close when the cross-spread relative to its own recent history
        # is favorable (adjusted > median_5m/30m/1h). This filters out
        # "just-crossed-zero" noisy moments that eat pnl via execution slippage.
        state = self.signal.get_state()
        if state is None:
            return
        reverse_state = (
            state.short_direction if reverse_is_green_dir == "short_var_long_lighter"
            else state.long_direction
        )
        if not reverse_state.is_green:
            return

        if pnl_per_unit < 0 or top_qty is None or top_qty <= 0:
            return

        # Only consume 30% of Lighter top-of-book qty per attempt — competitors
        # snapping up the level can eat ~70% without pushing us to worse levels.
        # Successive WS ticks re-fire until position is drained.
        # Quantize to 2dp so Var RFQ and Lighter limit get the identical qty.
        close_qty = quantize_qty(min(closable, top_qty * CLOSE_BOOK_FRACTION))
        if close_qty <= 0:
            return

        self._close_in_progress = True
        asyncio.create_task(self._execute_close(
            asset=asset, qty=close_qty,
            var_side=var_side, lighter_side=lighter_side,
            lighter_bid=lighter_bid, lighter_ask=lighter_ask,
            est_pnl_per_unit=pnl_per_unit,
        ))

    async def _execute_close(
        self, *,
        asset: str, qty: Decimal,
        var_side: str, lighter_side: str,
        lighter_bid: Decimal, lighter_ask: Decimal,
        est_pnl_per_unit: Decimal,
    ) -> None:
        t0_iso = _utc_now_iso()
        self.events.emit({
            "ts": t0_iso, "event": "close_attempt",
            "asset": asset, "qty": _dec_str(qty),
            "var_side": var_side, "lighter_side": lighter_side,
            "est_pnl_per_unit": _dec_str(est_pnl_per_unit),
            "var_pos_before": _dec_str(self._var_pos_qty),
            "lighter_pos_before": _dec_str(self._lighter_pos_qty),
        })
        try:
            # Pre-allocate Lighter client_order_id; slippage-buffered limit.
            slippage = Decimal(str(self.config.hedge_slippage_bps)) / Decimal("10000")
            if lighter_side == "BUY":
                limit_px = lighter_ask * (Decimal("1") + slippage)
            else:
                limit_px = lighter_bid * (Decimal("1") - slippage)
            import time as _time
            co_id = int(_time.time() * 1000)
            async with self._lock:
                while co_id in self._lighter_order_to_cycle:
                    co_id += 1

            # Fire both legs in parallel; both reduce-only.
            var_task = asyncio.create_task(self.var_placer.place_close_order(
                side=var_side, qty=qty, asset=asset,
                timeout_ms=self.config.var_order_timeout_ms,
            ))
            lighter_task = asyncio.create_task(self.lighter.place_order(
                side=lighter_side, qty=qty, limit_px=limit_px,
                client_order_id=co_id, reduce_only=True,
            ))
            var_res, lig_res = await asyncio.gather(var_task, lighter_task, return_exceptions=True)

            var_ok = getattr(var_res, "ok", False) if not isinstance(var_res, Exception) else False
            lig_ok = getattr(lig_res, "ok", False) if not isinstance(lig_res, Exception) else False
            var_error = (str(var_res) if isinstance(var_res, Exception)
                         else getattr(var_res, "error", None))
            lig_error = (str(lig_res) if isinstance(lig_res, Exception)
                         else getattr(lig_res, "error", None))

            self.events.emit({
                "ts": _utc_now_iso(), "event": "close_ack",
                "qty": _dec_str(qty),
                "var_ok": var_ok, "var_error": var_error,
                "lighter_ok": lig_ok, "lighter_error": lig_error,
                "var_pos_after": _dec_str(self._var_pos_qty),
                "lighter_pos_after": _dec_str(self._lighter_pos_qty),
                "close_mode_after": self._close_mode,
            })
        except Exception as exc:
            self.events.emit({
                "ts": _utc_now_iso(), "event": "close_error",
                "error_msg": str(exc),
            })
        finally:
            self._close_in_progress = False

    async def _handle_close_mode_residual(self, asset: str) -> None:
        """Called when close_mode sees one venue at 0. Queries actual positions
        from both venues and fires a single-venue reduce-only close on whichever
        side still has inventory. Syncs tracker to match reality afterward."""
        try:
            var_actual = await self.var_placer.get_position(asset)
            lig_actual = await self.lighter.get_position(asset)
            self.events.emit({
                "ts": _utc_now_iso(), "event": "close_residual_probe",
                "var_actual": _dec_str(var_actual),
                "lighter_actual": _dec_str(lig_actual),
                "var_tracker": _dec_str(self._var_pos_qty),
                "lighter_tracker": _dec_str(self._lighter_pos_qty),
            })
            if var_actual is None or lig_actual is None:
                self.logger.warning("residual probe failed: var=%s lighter=%s",
                                    var_actual, lig_actual)
                return

            # Sync tracker to reality before acting.
            self._var_pos_qty = var_actual
            self._lighter_pos_qty = lig_actual

            var_abs = abs(var_actual)
            lig_abs = abs(lig_actual)

            if var_abs == 0 and lig_abs == 0:
                # Both flat — just exit close_mode via _update_mode re-check
                async with self._lock:
                    self._update_mode()
                return

            # Fire single-venue close on the heavier / non-zero side
            if var_abs > 0 and lig_abs == 0:
                await self._close_var_residual(asset, var_actual)
            elif lig_abs > 0 and var_abs == 0:
                await self._close_lighter_residual(asset, lig_actual)
            else:
                # Both non-zero but tracker showed one as 0 — drift case.
                # Let normal paired close resume next tick now that tracker synced.
                async with self._lock:
                    self._update_mode()
        finally:
            self._close_in_progress = False

    async def _close_var_residual(self, asset: str, residual_qty: Decimal) -> None:
        """Single-venue Var reduce-only close for `residual_qty` signed."""
        qty = quantize_qty(abs(residual_qty))
        if qty <= 0:
            return
        # If long, SELL to close. If short, BUY to close.
        side = "sell" if residual_qty > 0 else "buy"
        self.events.emit({
            "ts": _utc_now_iso(), "event": "residual_close_var",
            "asset": asset, "side": side, "qty": _dec_str(qty),
            "residual_signed": _dec_str(residual_qty),
        })
        try:
            res = await self.var_placer.place_close_order(
                side=side, qty=qty, asset=asset,
                timeout_ms=self.config.var_order_timeout_ms,
            )
            self.events.emit({
                "ts": _utc_now_iso(), "event": "residual_close_var_ack",
                "ok": res.ok, "error": res.error,
            })
        except Exception as exc:
            self.logger.warning("residual_close_var failed: %s", exc)

    async def _close_lighter_residual(self, asset: str, residual_qty: Decimal) -> None:
        """Single-venue Lighter reduce-only close for `residual_qty` signed."""
        qty = quantize_qty(abs(residual_qty))
        if qty <= 0:
            return
        # If long, SELL; if short, BUY
        side = "SELL" if residual_qty > 0 else "BUY"
        # Aggressive limit price with slippage buffer
        bid, ask = await self.lighter.best_bid_ask()
        if bid is None or ask is None:
            return
        slippage = Decimal(str(self.config.hedge_slippage_bps)) / Decimal("10000")
        if side == "BUY":
            limit_px = ask * (Decimal("1") + slippage)
        else:
            limit_px = bid * (Decimal("1") - slippage)
        import time as _time
        co_id = int(_time.time() * 1000)
        async with self._lock:
            while co_id in self._lighter_order_to_cycle:
                co_id += 1
        self.events.emit({
            "ts": _utc_now_iso(), "event": "residual_close_lighter",
            "asset": asset, "side": side, "qty": _dec_str(qty),
            "residual_signed": _dec_str(residual_qty), "limit_px": _dec_str(limit_px),
        })
        try:
            res = await self.lighter.place_order(
                side=side, qty=qty, limit_px=limit_px,
                client_order_id=co_id, reduce_only=True,
            )
            self.events.emit({
                "ts": _utc_now_iso(), "event": "residual_close_lighter_ack",
                "ok": res.ok, "error": res.error,
            })
        except Exception as exc:
            self.logger.warning("residual_close_lighter failed: %s", exc)

    def get_recent_cycles(self, limit: int | None = None) -> list[TradeCycle]:
        """Snapshot of recent cycles (open + closed), newest-first.

        Dashboard / CSV export read from this; they MUST treat the returned
        objects as read-only — AutoTrader continues to mutate them as fills
        stream in.
        """
        snapshot = list(self._recent_cycles)
        if limit is not None:
            snapshot = snapshot[:limit]
        return snapshot

    async def reset_for_asset_switch(self) -> None:
        """Clear all cycle/position state on asset-switch. Positions on the old
        asset are assumed already closed (else asset-switch shouldn't fire)."""
        async with self._lock:
            self._open_cycles.clear()
            self._recent_cycles.clear()
            self._pending_var_fills.clear()
            self._lighter_order_to_cycle.clear()
            self._var_pos_qty = Decimal("0")
            self._var_pos_avg = Decimal("0")
            self._lighter_pos_qty = Decimal("0")
            self._lighter_pos_avg = Decimal("0")
            self._close_mode = False
            self._close_in_progress = False

    def _apply_var_fill(self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal) -> None:
        self._apply_fill(cycle.var_leg, fill_px, fill_qty)
        self.events.emit({
            "ts": cycle.var_leg.filled_at, "event": "var_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })

    def _apply_lighter_fill(self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal) -> None:
        self._apply_fill(cycle.lighter_leg, fill_px, fill_qty)
        self.events.emit({
            "ts": cycle.lighter_leg.filled_at, "event": "lighter_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })

    @staticmethod
    def _apply_fill(leg: LegState, fill_px: Decimal, fill_qty: Decimal) -> None:
        prior_filled = leg.filled_qty
        leg.filled_qty += fill_qty
        if leg.avg_fill_px is None:
            leg.avg_fill_px = fill_px
        else:
            leg.avg_fill_px = ((leg.avg_fill_px * prior_filled) + (fill_px * fill_qty)) / leg.filled_qty
        leg.filled_at = _utc_now_iso()
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
            self._update_mode()

        attribution = self._compute_attribution(cycle)
        cycles_row = self._cycle_to_row(cycle, attribution)
        self.cycles.emit(cycles_row)
        self.events.emit({
            "ts": cycle.closed_at, "event": "cycle_closed",
            "cycle_id": cycle.cycle_id, "status": cycle.status,
        })

        self._update_stats_on_close(cycle, attribution)

        # Post-cycle position verification: query both venues and compare
        # with the tracker. Catches partial-fill drift, missed WS events, etc.
        asyncio.create_task(self._verify_positions_after_cycle(cycle))

    async def _verify_positions_after_cycle(self, cycle: TradeCycle) -> None:
        try:
            var_actual = await self.var_placer.get_position(cycle.asset)
            lig_actual = await self.lighter.get_position(cycle.asset)
        except Exception as exc:
            self.logger.warning("verify_positions_after_cycle fetch failed: %s", exc)
            return
        var_track = self._var_pos_qty
        lig_track = self._lighter_pos_qty
        var_diff = (var_actual - var_track) if var_actual is not None else None
        lig_diff = (lig_actual - lig_track) if lig_actual is not None else None
        self.events.emit({
            "ts": _utc_now_iso(), "event": "position_verify",
            "cycle_id": cycle.cycle_id,
            "var_tracker": _dec_str(var_track),
            "var_actual": _dec_str(var_actual),
            "var_diff": _dec_str(var_diff),
            "lighter_tracker": _dec_str(lig_track),
            "lighter_actual": _dec_str(lig_actual),
            "lighter_diff": _dec_str(lig_diff),
        })
        # If either side drifted > quantum, sync tracker to exchange reality.
        threshold = QTY_QUANTUM
        synced = False
        if var_actual is not None and var_diff is not None and abs(var_diff) > threshold:
            self.logger.warning(
                "Var position drift %s -> syncing tracker %s to actual %s",
                var_diff, var_track, var_actual,
            )
            self._var_pos_qty = var_actual
            synced = True
        if lig_actual is not None and lig_diff is not None and abs(lig_diff) > threshold:
            self.logger.warning(
                "Lighter position drift %s -> syncing tracker %s to actual %s",
                lig_diff, lig_track, lig_actual,
            )
            self._lighter_pos_qty = lig_actual
            synced = True
        if synced:
            async with self._lock:
                self._update_mode()

    def _compute_attribution(self, cycle: TradeCycle) -> dict[str, Any]:
        plan = cycle.plan or TradePlan(
            qty_target=cycle.var_leg.requested_qty,
            expected_var_fill_px=None,
            expected_lighter_fill_px=None,
            expected_net_pct=None,
        )
        var_avg = cycle.var_leg.avg_fill_px
        lig_avg = cycle.lighter_leg.avg_fill_px

        realized_net_pct: float | None = None
        var_slip_pct: float | None = None
        lig_slip_pct: float | None = None

        if cycle.direction == "long_var_short_lighter" and var_avg is not None and lig_avg is not None and var_avg != 0:
            realized_net_pct = float((lig_avg - var_avg) / var_avg) * 100.0
        elif cycle.direction == "short_var_long_lighter" and var_avg is not None and lig_avg is not None and lig_avg != 0:
            realized_net_pct = float((var_avg - lig_avg) / lig_avg) * 100.0

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
                "fill_ratio": fill_ratio,
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
                "expected_net_pct": plan.expected_net_pct,
            },
            "var_leg": {
                "placed_at": cycle.var_leg.placed_at,
                "filled_at": cycle.var_leg.filled_at,
                "requested_qty": _dec_str(cycle.var_leg.requested_qty),
                "filled_qty": _dec_str(cycle.var_leg.filled_qty),
                "avg_fill_px": _dec_str(cycle.var_leg.avg_fill_px),
                "api_latency_ms": cycle.var_leg.api_latency_ms,
                "rfq_id": cycle.var_leg.rfq_id,
                "trade_id": cycle.var_leg.trade_id,
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
