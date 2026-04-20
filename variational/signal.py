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
    name: str
    cross_spread_pct: Decimal | None
    adjusted_pct: Decimal | None
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

    def detect_edges(self) -> list[tuple[str, str]]:
        """Return list of (direction, event) pairs that flipped this tick.

        event is "signal_turned_green" or "signal_turned_red".
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
        # Only return a median if history covers at least the full window.
        # Otherwise "5m", "30m", "1h" medians all collapse onto the same few
        # recent samples and the green-light test degenerates into "slightly
        # higher than the last handful of ticks" — signal would fire seconds
        # after startup instead of waiting for real history. Require the
        # oldest sample to predate the window's cutoff.
        if not self._history or self._history[0][0] > now - window_seconds:
            return None
        cutoff = now - window_seconds
        idx = 1 if long_side else 2
        values = [row[idx] for row in self._history if row[0] >= cutoff and row[idx] is not None]
        if not values:
            return None
        return float(median(values))


def _self_test() -> None:
    # --- Warmup gate: short history must NOT produce a green signal ---
    eng_warmup = SignalEngine(strict=False)
    now0 = time.monotonic()
    # Seed only 30 seconds of history (far less than the 5m window).
    for i in range(30):
        eng_warmup._history.append((now0 - (30 - i), 0.01, 0.01))
    s = eng_warmup.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.5"),
        lighter_ask=Decimal("100.6"),
    )
    assert s.long_direction.median_5m_pct is None, "5m median must be None during warmup"
    assert s.long_direction.median_30m_pct is None
    assert s.long_direction.median_1h_pct is None
    assert not s.long_direction.is_green, "warmup: no medians → no green even on wide spread"

    # --- Post-warmup: history covers the 5m window, median_5m participates ---
    eng = SignalEngine(strict=False)
    now0 = time.monotonic()
    # Seed 10 minutes of history spaced 1s apart, quiet cross spread ≈ 0.01%.
    for i in range(600):
        eng._history.append((now0 - (600 - i), 0.01, 0.01))
    s = eng.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.5"),
        lighter_ask=Decimal("100.6"),
    )
    assert s.long_direction.median_5m_pct is not None, "5m median should be available after 10m history"
    assert s.long_direction.median_30m_pct is None, "30m median still gated (history only 10m)"
    assert float(s.long_direction.adjusted_pct) > 0, s.long_direction.adjusted_pct
    assert s.long_direction.is_green, s.long_direction

    edges = eng.detect_edges()
    assert ("long_var_short_lighter", "signal_turned_green") in edges, edges

    # Second identical tick: still green but no edge (no transition).
    eng.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.5"),
        lighter_ask=Decimal("100.6"),
    )
    assert eng.detect_edges() == []

    # --- Strict mode: max of populated medians ---
    eng_strict = SignalEngine(strict=True)
    now0 = time.monotonic()
    for i in range(600):
        eng_strict._history.append((now0 - (600 - i), 5.0, 5.0))
    s = eng_strict.record(
        asset="BTC",
        var_bid=Decimal("100.0"),
        var_ask=Decimal("100.1"),
        lighter_bid=Decimal("100.2"),
        lighter_ask=Decimal("100.3"),
    )
    assert not s.long_direction.is_green, "strict: adjusted < max(medians) rejects"

    print("SignalEngine OK")


if __name__ == "__main__":
    _self_test()
