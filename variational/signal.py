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
    if var_bid is None or lighter_ask is None or lighter_ask == 0:
        return None
    return float((var_bid - lighter_ask) / lighter_ask) * 10000.0


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
    premium_bp: float | None


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
    )
