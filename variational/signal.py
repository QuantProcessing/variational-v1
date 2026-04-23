"""Market state + premium computation.

The strategy is a single-sided mean-reversion bet on the Var-premium:

    premium_bp(t) = (var_bid(t) - lighter_ask(t)) / lighter_ask(t) * 1e4

Positive = Var is bid above Lighter's ask, so shorting Var + going long
Lighter captures (var_bid - lighter_ask) per unit. A round trip unwinds
delta-neutrally; its PnL reduces to

    PnL ≈ entry_premium_bp - exit_premium_bp - slippage_roundtrip

so the whole strategy orbits around one number. No rolling medians, no
direction ensemble, no edge detection — AutoTrader checks absolute
premium thresholds (OPEN_BP / CLOSE_BP) on every MarketUpdate.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal


def compute_premium_bp(
    var_bid: Decimal | None,
    lighter_ask: Decimal | None,
) -> float | None:
    """Open-executable premium: per-unit edge captured by short-var/long-lighter
    at the touch. Positive when Var bid sits above Lighter ask."""
    if var_bid is None or lighter_ask is None or lighter_ask == 0:
        return None
    return float((var_bid - lighter_ask) / lighter_ask) * 10000.0


def compute_close_premium_bp(
    lighter_bid: Decimal | None,
    var_ask: Decimal | None,
) -> float | None:
    """Close-executable premium: per-unit cash flow from unwinding (sell
    lighter at its bid, buy var at its ask). Typically negative — we pay
    venue_spreads to cross out. Only positive under deep inversion where
    lit_bid > var_ask. This is the right gate for the close decision."""
    if lighter_bid is None or var_ask is None or lighter_bid == 0:
        return None
    return float((lighter_bid - var_ask) / lighter_bid) * 10000.0


@dataclass(frozen=True, slots=True)
class MarketState:
    """Per-tick snapshot consumed by AutoTrader.on_market_update."""
    ts_monotonic: float
    asset: str | None
    var_bid: Decimal | None
    var_ask: Decimal | None
    lighter_bid: Decimal | None
    lighter_ask: Decimal | None
    lighter_bid_qty: Decimal | None
    lighter_ask_qty: Decimal | None
    premium_bp: float | None       # open gate: (var_bid - lit_ask) / lit_ask
    close_premium_bp: float | None  # close gate: (lit_bid - var_ask) / lit_bid


def build_market_state(
    *,
    asset: str | None,
    var_bid: Decimal | None,
    var_ask: Decimal | None,
    lighter_bid: Decimal | None,
    lighter_ask: Decimal | None,
    lighter_bid_qty: Decimal | None = None,
    lighter_ask_qty: Decimal | None = None,
) -> MarketState:
    return MarketState(
        ts_monotonic=time.monotonic(),
        asset=asset,
        var_bid=var_bid,
        var_ask=var_ask,
        lighter_bid=lighter_bid,
        lighter_ask=lighter_ask,
        lighter_bid_qty=lighter_bid_qty,
        lighter_ask_qty=lighter_ask_qty,
        premium_bp=compute_premium_bp(var_bid, lighter_ask),
        close_premium_bp=compute_close_premium_bp(lighter_bid, var_ask),
    )
