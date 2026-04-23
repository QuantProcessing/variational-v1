"""AutoTrader: premium-band cross-venue strategy.

One-sided mean-reversion on the Var-premium vs Lighter:

    premium_bp = (var_bid - lighter_ask) / lighter_ask * 1e4

State machine (per MarketUpdate tick):
    flat + premium > open_bp         -> open short-var / long-lighter cycle
    in_position + premium < close_bp -> close both legs

Round-trip PnL ≈ entry_premium_bp - exit_premium_bp - slippage_roundtrip,
so (open_bp - close_bp) is the target edge per cycle. No median, no edges,
no stop-loss, no timeout — the bet is that Var/Lighter premiums revert.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any

from variational.journal import EventJournal
from variational.signal import MarketState

# Fraction of Lighter top-of-book qty to consume per close attempt. Setting
# to 1.0 means one-shot close: request full remaining qty in a single paired
# order, paying one round of Var RFQ slippage instead of N. Lighter may walk
# one or two levels deep if top isn't enough; that's cheaper than chunking
# through N RFQ calls. A previous value (0.3) was meant to limit exposure to
# level-skip by competitors, but at our qty scale it multiplied RFQ slippage
# and produced sub-min residuals that stalled at Lighter code=21706.
CLOSE_BOOK_FRACTION = Decimal("1.0")

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
    # Premium band thresholds, in basis points.
    # open_bp  : fire a new cycle when premium strictly exceeds this.
    # close_bp : unwind the current cycle when premium drops below this.
    # Target per-cycle edge (before slippage) = open_bp - close_bp.
    open_bp: float = 8.0
    close_bp: float = 1.0
    throttle_seconds: float = 1.0
    max_trades_per_day: int = 200
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
class CloseLegs:
    """Aggregate fills from the close (unwind) phase, attached back to the
    opening TradeCycle so round-trip PnL is observable without cross-referencing
    order_events.jsonl. Close may fire in 1..N batches (chunked or residual);
    fields are VWAP-style aggregates across all fills on each side.
    """
    var_rfq_ids: list[str] = field(default_factory=list)
    lighter_client_order_ids: list[int] = field(default_factory=list)
    var_filled_qty: Decimal = Decimal("0")
    var_avg_fill_px: Decimal | None = None
    lighter_filled_qty: Decimal = Decimal("0")
    lighter_avg_fill_px: Decimal | None = None
    first_close_at: str | None = None
    last_close_at: str | None = None
    # Premium at the moment of the final close attempt that took position flat.
    exit_premium_bp: float | None = None
    # Round-trip PnL in basis points, computed from actual fills only (no
    # venue fees since both are zero). Positive = profit.
    round_trip_pnl_bp: float | None = None
    fully_closed_at: str | None = None


@dataclass(slots=True)
class TradeCycle:
    cycle_id: str
    asset: str
    opened_at: str
    # Premium (bp) at the moment we decided to open. Entry_premium_bp in
    # the PnL formula. Always short_var_long_lighter; no direction field.
    entry_premium_bp: float | None = None
    closed_at: str | None = None
    plan: TradePlan | None = None
    var_leg: VarLegState = field(default_factory=VarLegState)
    lighter_leg: LighterLegState = field(default_factory=LighterLegState)
    close: CloseLegs = field(default_factory=CloseLegs)
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
        var_placer: VariationalPlacer,
        lighter: LighterAdapter,
        config: AutoTraderConfig,
        events_journal: EventJournal,
        cycles_journal: EventJournal,
        round_trips_journal: EventJournal | None = None,
        logger: logging.Logger,
    ) -> None:
        self.var_placer = var_placer
        self.lighter = lighter
        self.config = config
        self.events = events_journal
        self.cycles = cycles_journal
        # Optional dedicated journal for completed round-trips (open+close
        # merged). If None, round-trip records also go to cycles_journal
        # tagged as status="round_trip_closed".
        self.round_trips = round_trips_journal
        self.logger = logger

        # Pointer to the cycle currently holding a position. Set after a
        # successful open and used to route close fills (which have their
        # own rfq/co_id) back to the same row. With max 1 concurrent
        # position, there's never ambiguity.
        self._active_open_cycle: TradeCycle | None = None

        self.stats = TraderStats(_day_key=_today_key())
        self._last_fire_monotonic: float = 0.0
        self._open_cycles: dict[str, TradeCycle] = {}
        self._lighter_order_to_cycle: dict[int, str] = {}
        self._lock = asyncio.Lock()

        # Var WS can push fill events before /api/orders/new/market HTTP returns
        # the rfq_id we use to correlate. Buffer these unrouted fills keyed by
        # rfq_id; _fire_var_leg drains the matching entry once HTTP returns.
        # Each list entry carries (fill_px, fill_qty, trade_id, ts_monotonic)
        # so stale entries (close-order fills, failed cycles) can be GC'd.
        self._pending_var_fills: dict[str, list[tuple[Decimal, Decimal, str | None, float]]] = {}

        # Recent cycles (open + closed) for dashboard/CSV rendering.
        self._recent_cycles: deque[TradeCycle] = deque(maxlen=500)

        # In-progress guards: only ONE cycle at a time (max 1 concurrent
        # position), and closing is serialised so two MarketUpdates in quick
        # succession don't double-fire the unwind.
        self._close_in_progress = False

        # Per-venue position accounting for close PnL computation.
        # _*_pos_qty is signed (+long, -short). avg is cost basis (unsigned).
        self._var_pos_qty: Decimal = Decimal("0")
        self._var_pos_avg: Decimal = Decimal("0")
        self._lighter_pos_qty: Decimal = Decimal("0")
        self._lighter_pos_avg: Decimal = Decimal("0")

    # ---------- public API ----------

    async def on_market_update(self, state: MarketState) -> None:
        """Single entry point driven by every MarketUpdate (Lighter WS book
        delta or Var indicative poll). Decides whether to open or close
        based on current premium and position state."""
        if state.premium_bp is None:
            return
        self._maybe_rollover()
        if self.stats.frozen:
            return

        flat = self._is_flat()
        if flat:
            if state.premium_bp > self.config.open_bp:
                await self._try_open(state)
        else:
            if state.premium_bp < self.config.close_bp:
                await self._try_close(state)

    def snapshot(self) -> TraderStats:
        self._maybe_rollover()
        return self.stats

    # ---------- state predicates ----------

    def _is_flat(self) -> bool:
        """Truly flat: no in-flight open cycle AND no filled position on
        either venue AND no close already in progress. Conservative on
        purpose — avoids double-firing near fill boundaries."""
        if self._open_cycles:
            return False
        if self._close_in_progress:
            return False
        if abs(self._var_pos_qty) > QTY_QUANTUM:
            return False
        if abs(self._lighter_pos_qty) > QTY_QUANTUM:
            return False
        return True

    def _throttled(self) -> bool:
        import time
        if self.config.throttle_seconds <= 0:
            return False
        return (time.monotonic() - self._last_fire_monotonic) < self.config.throttle_seconds

    def _maybe_rollover(self) -> None:
        today = _today_key()
        if self.stats._day_key != today:
            self.stats._day_key = today
            self.stats.trades_today = 0
            self.stats.failures_today = 0
            # Consecutive failures / frozen do NOT reset on day rollover.

    # ---------- open ----------

    async def _try_open(self, state: MarketState) -> None:
        if self._throttled():
            return
        if self.stats.trades_today >= self.config.max_trades_per_day:
            return
        # Depth sanity: lighter_ask_qty tells us the top-of-book absorb
        # capacity. If we'd eat through the top level, skip — slippage on
        # the long-lighter leg would likely kill the edge.
        need_qty = self.config.qty
        if state.lighter_ask_qty is not None and state.lighter_ask_qty < need_qty:
            return
        await self._fire_open(state)

    async def _fire_open(self, state: MarketState) -> None:
        import time
        cycle_id = _new_cycle_id()
        now_iso = _utc_now_iso()
        now_mono = time.monotonic()

        # Strategy is one-sided: short Var @ var_bid, long Lighter @ lighter_ask.
        expected_var_fill = state.var_bid
        expected_lighter_fill = state.lighter_ask
        var_side = "sell"
        lighter_side = "BUY"

        plan = TradePlan(
            qty_target=self.config.qty,
            expected_var_fill_px=expected_var_fill,
            expected_lighter_fill_px=expected_lighter_fill,
            expected_net_pct=(state.premium_bp / 100.0) if state.premium_bp is not None else None,
        )

        cycle = TradeCycle(
            cycle_id=cycle_id,
            asset=state.asset or "UNKNOWN",
            opened_at=now_iso,
            entry_premium_bp=state.premium_bp,
            plan=plan,
        )
        open_qty = quantize_qty(self.config.qty)
        cycle.var_leg.requested_qty = open_qty
        cycle.lighter_leg.requested_qty = open_qty

        async with self._lock:
            self._open_cycles[cycle_id] = cycle
            self._recent_cycles.appendleft(cycle)
            self._last_fire_monotonic = now_mono
            self.stats.trades_today += 1
            self._active_open_cycle = cycle

        self.events.emit({
            "ts": now_iso, "event": "cycle_opened", "cycle_id": cycle_id,
            "asset": cycle.asset,
            "entry_premium_bp": state.premium_bp,
            "qty_target": _dec_str(self.config.qty),
            "plan": {
                "expected_var_fill_px": _dec_str(expected_var_fill),
                "expected_lighter_fill_px": _dec_str(expected_lighter_fill),
                "expected_net_pct": plan.expected_net_pct,
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

        # If this rfq matches the active cycle's close, apply it as a close-leg
        # fill rather than an open-leg fill. Close fills aggregate VWAP-style.
        if rfq_id:
            active = self._active_open_cycle
            if active is not None and rfq_id in active.close.var_rfq_ids:
                self._apply_close_var_fill(active, fill_px, fill_qty)
                await self._maybe_finalize_round_trip(active)
                return

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

        # Close fill routing: active cycle registers every close co_id it fires.
        active = self._active_open_cycle
        if active is not None and client_order_id in active.close.lighter_client_order_ids:
            self._apply_close_lighter_fill(active, fill_px, fill_qty)
            await self._maybe_finalize_round_trip(active)
            return

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

    # ---------- close ----------

    async def _try_close(self, state: MarketState) -> None:
        """Fire reduce-only unwind on both venues. Gate = premium < close_bp
        (checked by caller) + Lighter depth + no close already in flight.
        Position is always short_var / long_lighter under the one-sided
        strategy, but we handle the opposite case too in the rare event of
        venue drift synced via _verify_positions_after_cycle."""
        if self._close_in_progress:
            return
        if state.var_bid is None or state.var_ask is None:
            return
        if state.lighter_bid is None or state.lighter_ask is None:
            return

        # Venue drift: if one side shows zero while the other is non-zero,
        # the paired close can't work; spin off a single-venue residual close.
        # Below-dust residuals are abandoned (Lighter rejects sub-min orders
        # with code=21706) — user cleans up on the venue UI.
        if self._var_pos_qty == 0 or self._lighter_pos_qty == 0:
            residual = abs(self._var_pos_qty) + abs(self._lighter_pos_qty)
            if residual < RESIDUAL_DUST_QTY:
                self.logger.warning(
                    "Residual var=%s lighter=%s below dust threshold %s; "
                    "abandoning. Clean manually on the venue UI.",
                    self._var_pos_qty, self._lighter_pos_qty, RESIDUAL_DUST_QTY,
                )
                # Zero out so _is_flat() lets new opens resume.
                self._var_pos_qty = Decimal("0")
                self._lighter_pos_qty = Decimal("0")
                return
            self._close_in_progress = True
            asset = state.asset or ""
            asyncio.create_task(self._handle_residual(asset))
            return
        if self._var_pos_avg == 0 or self._lighter_pos_avg == 0:
            return

        # Normal case: short Var / long Lighter → BUY var @ ask, SELL lighter @ bid.
        # Defensive else branch handles the drift-flipped scenario.
        if self._var_pos_qty < 0:
            top_qty = state.lighter_bid_qty
            var_side, lighter_side = "buy", "SELL"
        else:
            top_qty = state.lighter_ask_qty
            var_side, lighter_side = "sell", "BUY"
        closable = min(abs(self._var_pos_qty), abs(self._lighter_pos_qty))
        if top_qty is None or top_qty <= 0:
            return

        # Consume only 30% of Lighter top-of-book qty per attempt so
        # competitors eating the level don't push us to worse prints.
        # Successive MarketUpdates re-fire until position drains.
        close_qty = quantize_qty(min(closable, top_qty * CLOSE_BOOK_FRACTION))
        if close_qty <= 0:
            return

        self._close_in_progress = True
        asyncio.create_task(self._execute_close(
            asset=state.asset or "", qty=close_qty,
            var_side=var_side, lighter_side=lighter_side,
            lighter_bid=state.lighter_bid, lighter_ask=state.lighter_ask,
            exit_premium_bp=state.premium_bp,
        ))

    async def _execute_close(
        self, *,
        asset: str, qty: Decimal,
        var_side: str, lighter_side: str,
        lighter_bid: Decimal, lighter_ask: Decimal,
        exit_premium_bp: float | None,
    ) -> None:
        t0_iso = _utc_now_iso()
        self.events.emit({
            "ts": t0_iso, "event": "close_attempt",
            "asset": asset, "qty": _dec_str(qty),
            "var_side": var_side, "lighter_side": lighter_side,
            "exit_premium_bp": exit_premium_bp,
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

            # Pre-register lighter co_id → active cycle so on_lighter_fill can
            # route the close fill back to the TradeCycle (same pattern as open).
            active = self._active_open_cycle
            if active is not None:
                async with self._lock:
                    self._lighter_order_to_cycle[co_id] = active.cycle_id
                    active.close.lighter_client_order_ids.append(co_id)
                    if active.close.first_close_at is None:
                        active.close.first_close_at = t0_iso
                    active.close.last_close_at = t0_iso
                    if exit_premium_bp is not None:
                        active.close.exit_premium_bp = exit_premium_bp

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

            # Close Var's trade_id is the close rfq_id — stash on active cycle
            # so on_variational_fill can route the close fill back.
            var_trade_id = (
                None if isinstance(var_res, Exception)
                else getattr(var_res, "trade_id", None)
            )
            if active is not None and var_trade_id:
                async with self._lock:
                    if var_trade_id not in active.close.var_rfq_ids:
                        active.close.var_rfq_ids.append(str(var_trade_id))

            self.events.emit({
                "ts": _utc_now_iso(), "event": "close_ack",
                "qty": _dec_str(qty),
                "var_ok": var_ok, "var_error": var_error,
                "var_close_rfq_id": var_trade_id,
                "lighter_ok": lig_ok, "lighter_error": lig_error,
                "lighter_close_co_id": co_id,
                "var_pos_after": _dec_str(self._var_pos_qty),
                "lighter_pos_after": _dec_str(self._lighter_pos_qty),
            })
        except Exception as exc:
            self.events.emit({
                "ts": _utc_now_iso(), "event": "close_error",
                "error_msg": str(exc),
            })
        finally:
            self._close_in_progress = False

    async def _handle_residual(self, asset: str) -> None:
        """Called when _try_close sees one venue at 0 but the other non-zero.
        Queries actual positions and fires a single-venue reduce-only close
        on the side that still has inventory. Syncs tracker to reality."""
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
                # Both flat — nothing to do; _is_flat() will let new opens resume.
                return

            # Fire single-venue close on the non-zero side
            if var_abs > 0 and lig_abs == 0:
                await self._close_var_residual(asset, var_actual)
            elif lig_abs > 0 and var_abs == 0:
                await self._close_lighter_residual(asset, lig_actual)
            # Both non-zero — tracker drifted, normal paired close resumes next tick.
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

    def _apply_close_var_fill(
        self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal,
    ) -> None:
        c = cycle.close
        prior = c.var_filled_qty
        c.var_filled_qty += fill_qty
        if c.var_avg_fill_px is None:
            c.var_avg_fill_px = fill_px
        else:
            c.var_avg_fill_px = ((c.var_avg_fill_px * prior) + (fill_px * fill_qty)) / c.var_filled_qty
        c.last_close_at = _utc_now_iso()
        self.events.emit({
            "ts": c.last_close_at, "event": "close_var_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })

    def _apply_close_lighter_fill(
        self, cycle: TradeCycle, fill_px: Decimal, fill_qty: Decimal,
    ) -> None:
        c = cycle.close
        prior = c.lighter_filled_qty
        c.lighter_filled_qty += fill_qty
        if c.lighter_avg_fill_px is None:
            c.lighter_avg_fill_px = fill_px
        else:
            c.lighter_avg_fill_px = (
                (c.lighter_avg_fill_px * prior) + (fill_px * fill_qty)
            ) / c.lighter_filled_qty
        c.last_close_at = _utc_now_iso()
        self.events.emit({
            "ts": c.last_close_at, "event": "close_lighter_fill",
            "cycle_id": cycle.cycle_id, "fill_px": _dec_str(fill_px),
            "fill_qty": _dec_str(fill_qty),
        })

    async def _maybe_finalize_round_trip(self, cycle: TradeCycle) -> None:
        """Call after any close fill. If both venue positions are flat and
        the active cycle's close qty matches its open qty, compute the
        round-trip PnL, emit a round_trip_closed record, and clear the
        active-cycle pointer so the next open can fire."""
        # Position must be fully flat before we declare the round-trip done.
        if abs(self._var_pos_qty) > QTY_QUANTUM:
            return
        if abs(self._lighter_pos_qty) > QTY_QUANTUM:
            return
        if self._active_open_cycle is not cycle:
            return

        # Compute round-trip PnL from actual fills. Both venues zero-fee.
        # Short-var/long-lighter: open-leg cash = open_var - open_lighter
        #                         close cash   = close_lighter - close_var
        open_v = cycle.var_leg.avg_fill_px
        open_l = cycle.lighter_leg.avg_fill_px
        close_v = cycle.close.var_avg_fill_px
        close_l = cycle.close.lighter_avg_fill_px
        pnl_bp: float | None = None
        if all(x is not None for x in (open_v, open_l, close_v, close_l)) and open_l != 0:
            pnl_per_unit = (open_v - close_v) + (close_l - open_l)
            pnl_bp = float(pnl_per_unit / open_l) * 10000.0
            cycle.close.round_trip_pnl_bp = pnl_bp
        cycle.close.fully_closed_at = _utc_now_iso()

        row = {
            "ts": cycle.close.fully_closed_at,
            "event": "round_trip_closed",
            "cycle_id": cycle.cycle_id,
            "asset": cycle.asset,
            "entry_premium_bp": cycle.entry_premium_bp,
            "exit_premium_bp": cycle.close.exit_premium_bp,
            "open_var_px": _dec_str(open_v),
            "open_lighter_px": _dec_str(open_l),
            "close_var_px": _dec_str(close_v),
            "close_lighter_px": _dec_str(close_l),
            "qty": _dec_str(cycle.var_leg.requested_qty),
            "round_trip_pnl_bp": pnl_bp,
            "opened_at": cycle.opened_at,
            "first_close_at": cycle.close.first_close_at,
            "fully_closed_at": cycle.close.fully_closed_at,
        }
        target_journal = self.round_trips if self.round_trips is not None else self.cycles
        target_journal.emit(row)
        self.events.emit({
            "ts": cycle.close.fully_closed_at,
            "event": "round_trip_closed",
            "cycle_id": cycle.cycle_id,
            "round_trip_pnl_bp": pnl_bp,
        })

        self._active_open_cycle = None

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
        _ = synced  # unused after mode removal; kept for future hook

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

        # Single direction: short Var / long Lighter.
        # realized_net_pct is the OPEN-leg paper edge vs Lighter's basis. True
        # round-trip PnL lives in close_attempt / close_ack events and is NOT
        # aggregated into this field (a known gap; see roadmap).
        if var_avg is not None and lig_avg is not None and lig_avg != 0:
            realized_net_pct = float((var_avg - lig_avg) / lig_avg) * 100.0

        if plan.expected_var_fill_px is not None and var_avg is not None and plan.expected_var_fill_px != 0:
            # Sell-side slippage: expected bid - actual avg (positive = worse than quoted).
            var_slip_pct = float((plan.expected_var_fill_px - var_avg) / plan.expected_var_fill_px) * 100.0
        if plan.expected_lighter_fill_px is not None and lig_avg is not None and plan.expected_lighter_fill_px != 0:
            # Buy-side slippage: actual avg - expected ask (positive = worse than quoted).
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
        plan = cycle.plan or TradePlan(cycle.var_leg.requested_qty, None, None, None)
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
            "entry_premium_bp": cycle.entry_premium_bp,
            "status": cycle.status,
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
