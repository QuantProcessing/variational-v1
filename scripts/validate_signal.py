#!/usr/bin/env python3
"""validate_signal.py — diagnose whether observed direction skew is real or a bug.

Reads log/*.jsonl produced by main.py and (optionally) probes live venues.
Answers the question: "only one direction is firing — is the market actually
asymmetric, or is my signal / code wrong?"

Usage:
    python scripts/validate_signal.py                        # analyze ./log
    python scripts/validate_signal.py --log-dir ./log
    python scripts/validate_signal.py --since-minutes 60     # last hour only
    python scripts/validate_signal.py --live                 # also probe Lighter REST

Exit code 0 on successful run (regardless of findings). Use stdout for verdict.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable


# ---------- I/O helpers ----------

def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # skip malformed lines (e.g., mid-write truncation)
                pass
    return out


def parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def filter_by_recency(rows: list[dict], since: datetime | None, ts_key: str = "ts") -> list[dict]:
    if since is None:
        return rows
    out = []
    for r in rows:
        t = parse_iso(r.get(ts_key) or r.get("triggered_at") or r.get("closed_at"))
        if t is not None and t >= since:
            out.append(r)
    return out


def to_dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


# ---------- Analyses ----------

def analyze_signal_events(events: list[dict]) -> dict:
    greens = [e for e in events if e.get("event") == "signal_turned_green"]
    reds = [e for e in events if e.get("event") == "signal_turned_red"]

    greens_by_dir = Counter(e.get("direction") for e in greens)

    # adjusted_pct distribution per direction (at green moment)
    adj_by_dir: dict[str, list[float]] = defaultdict(list)
    for e in greens:
        adj = e.get("adjusted_pct")
        if adj is None:
            continue
        try:
            adj_by_dir[e.get("direction")].append(float(adj))
        except (TypeError, ValueError):
            pass

    # bid < ask sanity + mid-price offset, across ALL signal events
    mid_offsets: list[float] = []
    book_violations: list[str] = []  # "var" / "lighter"
    quote_count = 0
    for e in events:
        q = e.get("quotes") or {}
        vb = to_dec(q.get("var_bid"))
        va = to_dec(q.get("var_ask"))
        lb = to_dec(q.get("lighter_bid"))
        la = to_dec(q.get("lighter_ask"))
        if None in (vb, va, lb, la):
            continue
        quote_count += 1
        if vb >= va:
            book_violations.append("var_bid>=var_ask")
        if lb >= la:
            book_violations.append("lighter_bid>=lighter_ask")
        var_mid = (vb + va) / 2
        lig_mid = (lb + la) / 2
        if lig_mid != 0:
            mid_offsets.append(float((var_mid - lig_mid) / lig_mid) * 100)

    def stats(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        return {
            "n": len(xs),
            "min": min(xs),
            "p10": statistics.quantiles(xs, n=10)[0] if len(xs) >= 10 else None,
            "median": statistics.median(xs),
            "mean": statistics.mean(xs),
            "p90": statistics.quantiles(xs, n=10)[8] if len(xs) >= 10 else None,
            "max": max(xs),
        }

    return {
        "total_events": len(events),
        "green_total": len(greens),
        "red_total": len(reds),
        "greens_by_direction": dict(greens_by_dir),
        "adjusted_pct_at_green": {d: stats(v) for d, v in adj_by_dir.items()},
        "mid_offset_pct_stats": stats(mid_offsets),
        "mid_offset_pct_positive_frac": (
            sum(1 for x in mid_offsets if x > 0) / len(mid_offsets) if mid_offsets else None
        ),
        "quote_count": quote_count,
        "book_violations": Counter(book_violations),
    }


def verify_signal_math(events: list[dict], tolerance: float = 1e-4) -> dict:
    """Re-compute adjusted_pct from stored quotes and compare against stored value."""
    checked = 0
    mismatches: list[dict] = []
    for e in events:
        direction = e.get("direction")
        q = e.get("quotes") or {}
        vb = to_dec(q.get("var_bid"))
        va = to_dec(q.get("var_ask"))
        lb = to_dec(q.get("lighter_bid"))
        la = to_dec(q.get("lighter_ask"))
        stored = e.get("adjusted_pct")
        if None in (vb, va, lb, la) or stored is None:
            continue
        try:
            stored_f = float(stored)
        except (TypeError, ValueError):
            continue

        var_mid = (vb + va) / 2
        lig_mid = (lb + la) / 2
        if var_mid == 0 or lig_mid == 0:
            continue
        var_book_pct = (va - vb) / var_mid * 100
        lig_book_pct = (la - lb) / lig_mid * 100
        baseline = (var_book_pct + lig_book_pct) / 2

        if direction == "long_var_short_lighter":
            if va == 0:
                continue
            cross = (lb - va) / va * 100
        elif direction == "short_var_long_lighter":
            if la == 0:
                continue
            cross = (vb - la) / la * 100
        else:
            continue

        computed = float(cross - baseline)
        checked += 1
        if abs(computed - stored_f) > tolerance:
            mismatches.append({
                "ts": e.get("ts"),
                "direction": direction,
                "stored": stored_f,
                "recomputed": computed,
                "delta": computed - stored_f,
            })
    return {"checked": checked, "mismatch_count": len(mismatches), "first_mismatches": mismatches[:10]}


def analyze_cycles(rows: list[dict]) -> dict:
    by_dir = Counter(r.get("direction") for r in rows)
    by_status = Counter(r.get("status") for r in rows)
    by_dir_status = Counter((r.get("direction"), r.get("status")) for r in rows)

    realized_by_dir: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        rn = (r.get("attribution") or {}).get("realized_net_pct")
        if rn is None:
            continue
        try:
            realized_by_dir[r.get("direction")].append(float(rn))
        except (TypeError, ValueError):
            pass

    return {
        "total": len(rows),
        "by_direction": dict(by_dir),
        "by_status": dict(by_status),
        "by_direction_and_status": {f"{d} | {s}": c for (d, s), c in by_dir_status.items()},
        "realized_net_pct_per_direction": {
            d: {
                "n": len(v),
                "sum": sum(v),
                "mean": statistics.mean(v) if v else None,
                "median": statistics.median(v) if v else None,
            }
            for d, v in realized_by_dir.items()
        },
    }


def analyze_order_events(rows: list[dict]) -> dict:
    skip_reasons = Counter(
        r.get("reason")
        for r in rows
        if r.get("event") == "signal_decision" and r.get("decision") == "skip"
    )
    type_counts = Counter(r.get("event") for r in rows)
    return {
        "total_events": len(rows),
        "event_type_counts": dict(type_counts),
        "skip_reasons": dict(skip_reasons),
    }


# ---------- Live probes (optional) ----------

def live_lighter_snapshot(asset: str) -> dict:
    try:
        import requests  # deferred so the script works w/o network deps if user doesn't pass --live
    except ImportError:
        return {"error": "requests module not available"}

    base_url = "https://mainnet.zklighter.elliot.ai"
    out: dict[str, Any] = {"asset": asset}

    # resolve market_id
    try:
        r = requests.get(f"{base_url}/api/v1/orderBooks",
                         headers={"accept": "application/json"}, timeout=10)
        r.raise_for_status()
        mid = None
        for m in r.json().get("order_books", []):
            if m.get("symbol") == asset:
                mid = int(m["market_id"])
                out["market_id"] = mid
                break
        if mid is None:
            out["error"] = f"market_id not found for symbol {asset}"
            return out
    except Exception as exc:
        out["error"] = f"orderBooks fetch failed: {exc}"
        return out

    # best bid/ask via orderBookOrders
    try:
        r = requests.get(
            f"{base_url}/api/v1/orderBookOrders",
            params={"market_id": mid, "limit": 1},
            headers={"accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []
        out["lighter_bid"] = bids[0]["price"] if bids else None
        out["lighter_ask"] = asks[0]["price"] if asks else None
    except Exception as exc:
        out["orderbook_error"] = str(exc)

    # account balance
    try:
        idx = os.getenv("LIGHTER_ACCOUNT_INDEX")
        if idx:
            r = requests.get(
                f"{base_url}/api/v1/account",
                params={"by": "index", "value": idx},
                headers={"accept": "application/json"},
                timeout=10,
            )
            r.raise_for_status()
            data = r.json()
            if data.get("accounts"):
                a = data["accounts"][0]
                out["collateral"] = a.get("collateral")
                out["portfolio_value"] = a.get("total_asset_value")
                positions_summary = []
                for p in a.get("positions") or []:
                    try:
                        if float(p.get("position") or 0) != 0:
                            positions_summary.append({
                                "symbol": p.get("symbol"),
                                "sign": p.get("sign"),
                                "position": p.get("position"),
                                "avg_entry_price": p.get("avg_entry_price"),
                                "unrealized_pnl": p.get("unrealized_pnl"),
                            })
                    except (TypeError, ValueError):
                        pass
                out["open_positions"] = positions_summary
    except Exception as exc:
        out["account_error"] = str(exc)

    return out


# ---------- Verdict composition ----------

def render_section(title: str, body: Any) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)
    if isinstance(body, (dict, list)):
        print(json.dumps(body, indent=2, default=str))
    else:
        print(body)


def compose_verdict(signal_analysis: dict, math_check: dict,
                    cycle_analysis: dict, order_analysis: dict) -> list[str]:
    out: list[str] = []

    # 1) Book sanity
    violations = signal_analysis.get("book_violations") or Counter()
    if sum(violations.values()) > 0:
        out.append(
            f"❌ BUG candidate: book inversions detected — {dict(violations)}. "
            "Either bid/ask labeling is wrong, or we're reading stale / crossed books."
        )
    else:
        out.append("✅ bid < ask holds across all sampled quotes (no book inversions).")

    # 2) Signal math self-consistency
    mm = math_check.get("mismatch_count", 0)
    ch = math_check.get("checked", 0)
    if ch == 0:
        out.append("⚠️ Couldn't re-verify signal math (no usable quote snapshots).")
    elif mm == 0:
        out.append(f"✅ Signal math is self-consistent across {ch} samples "
                   "(stored adjusted_pct matches recomputation).")
    else:
        out.append(f"❌ Signal math diverges on {mm}/{ch} samples — see "
                   "'Signal math self-consistency' section above.")

    # 3) Direction balance
    greens = signal_analysis.get("greens_by_direction") or {}
    long_g = greens.get("long_var_short_lighter", 0)
    short_g = greens.get("short_var_long_lighter", 0)
    if long_g == 0 and short_g == 0:
        out.append("⚠️ No green edges in the analysed window. "
                   "Either still in warmup (<5m history), or signal never crossed median.")
    elif long_g == 0 and short_g > 0:
        out.append(f"⚠️ DIRECTION ONE-SIDED: long_var_short_lighter never turned green "
                   f"(short_var_long_lighter: {short_g} greens).")
    elif short_g == 0 and long_g > 0:
        out.append(f"⚠️ DIRECTION ONE-SIDED: short_var_long_lighter never turned green "
                   f"(long_var_short_lighter: {long_g} greens).")
    else:
        ratio = max(long_g, short_g) / max(min(long_g, short_g), 1)
        out.append(f"ℹ️ Direction balance: long={long_g}, short={short_g} "
                   f"(skew ratio {ratio:.2f}).")

    # 4) Mid-price offset interpretation
    mo_stats = signal_analysis.get("mid_offset_pct_stats") or {}
    pos_frac = signal_analysis.get("mid_offset_pct_positive_frac")
    if mo_stats.get("n", 0) > 10 and pos_frac is not None:
        median = mo_stats.get("median")
        pct_pos = pos_frac * 100
        out.append(
            f"ℹ️ Mid-price offset (var_mid - lig_mid)/lig_mid: "
            f"median={median:+.4f}%, positive fraction={pct_pos:.1f}% "
            f"(n={mo_stats['n']})"
        )
        if median is not None and median > 0.01 and pct_pos > 70:
            out.append(
                "   → DIAGNOSIS: Variational priced systematically HIGHER than Lighter. "
                "This naturally favors short_var_long_lighter (sell var high / buy lighter low). "
                "Almost certainly real market asymmetry (funding differential or Var RFQ "
                "market-maker inventory skew), NOT a signal/code bug."
            )
        elif median is not None and median < -0.01 and pct_pos < 30:
            out.append(
                "   → DIAGNOSIS: Lighter priced systematically HIGHER than Variational. "
                "This naturally favors long_var_short_lighter."
            )
        else:
            out.append("   → Mid prices roughly balanced. Direction skew (if any) must come "
                       "from a different source — investigate signal math or volatility regime.")

    # 5) Per-direction adjusted_pct sign — is the missing side correctly gated?
    adj_stats = signal_analysis.get("adjusted_pct_at_green") or {}
    for direction, s in adj_stats.items():
        if s.get("n", 0) < 5:
            continue
        med = s.get("median")
        mx = s.get("max")
        if med is not None and med < 0 and (mx is None or mx < 0):
            out.append(
                f"ℹ️ All {direction} greens have adjusted_pct < 0 "
                f"(median={med:+.4f}%, max={mx:+.4f}%)."
            )
            out.append(
                "   → These greens represent 'best of a bad lot' vs recent median, but are "
                "still theoretically unprofitable at entry. AutoTrader's signal_flipped "
                "gate correctly rejects them — no orders fire. This is SAFE behavior, "
                "not a bug."
            )
        elif med is not None and med > 0 and (s.get("min") is None or s.get("min") > 0):
            out.append(
                f"ℹ️ All {direction} greens have adjusted_pct > 0 "
                f"(median={med:+.4f}%) — genuinely profitable at entry."
            )

    # 6) Cycle vs signal alignment + gate attribution
    by_dir_cycle = cycle_analysis.get("by_direction") or {}
    long_c = by_dir_cycle.get("long_var_short_lighter", 0)
    short_c = by_dir_cycle.get("short_var_long_lighter", 0)
    skip_reasons = order_analysis.get("skip_reasons") or {}
    if (long_c + short_c) > 0 and (long_g + short_g) > 0:
        cycle_short_frac = short_c / (long_c + short_c)
        signal_short_frac = short_g / (long_g + short_g)
        gap = abs(cycle_short_frac - signal_short_frac)
        out.append(
            f"ℹ️ Fire-through: signals_short={signal_short_frac:.2f}, "
            f"cycles_short={cycle_short_frac:.2f} (delta={gap:.2f})."
        )
        if gap > 0.15:
            # Figure out what's blocking
            sf = skip_reasons.get("signal_flipped", 0)
            dl = skip_reasons.get("day_limit", 0)
            fz = skip_reasons.get("frozen", 0)
            pl = skip_reasons.get("position_limit_exceeded", 0)
            ro = skip_reasons.get("reduce_only_mode", 0)
            ni = skip_reasons.get("net_imbalance_exceeded", 0)  # legacy name
            th = skip_reasons.get("throttled", 0)
            out.append(
                f"   → skip_reasons: signal_flipped={sf} day_limit={dl} frozen={fz} "
                f"position_limit={pl} reduce_only={ro} net_imbalance={ni} throttled={th}"
            )
            if sf > 0 and sf >= max(long_g, short_g) - max(long_c, short_c):
                out.append(
                    "   → Dominant blocker is signal_flipped (adjusted_pct <= 0 at fire). "
                    "This is the safety gate, behaving correctly. The missing direction "
                    "simply had no profitable moments."
                )
            elif dl + fz + pl + ro + ni > sf:
                out.append(
                    "   → Dominant blocker is capacity/risk gate (day_limit / frozen / "
                    "position_limit / reduce_only). Consider raising --max-trades-per-day "
                    "or --position-limit if the missing direction had profitable greens."
                )

    # 7) Realized P&L sanity — is the profitable-side actually profitable?
    realized = cycle_analysis.get("realized_net_pct_per_direction") or {}
    for direction, s in realized.items():
        if s.get("n", 0) < 3:
            continue
        mean = s.get("mean")
        if mean is None:
            continue
        sign = "+" if mean >= 0 else ""
        if mean < 0:
            out.append(
                f"⚠️ {direction}: realized_net_pct mean = {sign}{mean:.4f}% over {s['n']} "
                "fully-filled cycles → cycles are net-losing. Entry edge eaten by exit "
                "cost / slippage. Consider --signal-strict or tightening qty."
            )
        else:
            out.append(
                f"✅ {direction}: realized_net_pct mean = {sign}{mean:.4f}% over {s['n']} "
                "fully-filled cycles → positive alpha captured."
            )

    return out


# ---------- Main ----------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose whether observed direction skew is real or a bug."
    )
    parser.add_argument("--log-dir", default="log", help="Directory with *.jsonl files")
    parser.add_argument("--since-minutes", type=int, default=0,
                        help="Only analyse events newer than N minutes ago (0 = all)")
    parser.add_argument("--live", action="store_true",
                        help="Also probe Lighter public REST for current state")
    parser.add_argument("--asset", default="HYPE", help="Asset symbol for live probe")
    args = parser.parse_args()

    log_dir = Path(args.log_dir)
    if not log_dir.exists():
        print(f"log-dir not found: {log_dir}", file=sys.stderr)
        return 1

    since: datetime | None = None
    if args.since_minutes > 0:
        since = datetime.now(timezone.utc) - timedelta(minutes=args.since_minutes)

    signal_events = filter_by_recency(load_jsonl(log_dir / "signal_events.jsonl"), since, "ts")
    cycle_rows = filter_by_recency(load_jsonl(log_dir / "cycle_pnl.jsonl"), since, "triggered_at")
    order_events = filter_by_recency(load_jsonl(log_dir / "order_events.jsonl"), since, "ts")

    window_desc = f"last {args.since_minutes}m" if since else "all time"
    print(f"Analysing: signal_events={len(signal_events)} cycles={len(cycle_rows)} "
          f"order_events={len(order_events)}  window: {window_desc}")

    signal_analysis = analyze_signal_events(signal_events)
    math_check = verify_signal_math(signal_events)
    cycle_analysis = analyze_cycles(cycle_rows)
    order_analysis = analyze_order_events(order_events)

    render_section("1. Signal event summary", signal_analysis)
    render_section("2. Signal math self-consistency", math_check)
    render_section("3. Cycle outcome breakdown", cycle_analysis)
    render_section("4. Order event summary (skip reasons)", order_analysis)

    if args.live:
        render_section("5. Live Lighter probe", live_lighter_snapshot(args.asset))

    print()
    print("=" * 78)
    print("VERDICT")
    print("=" * 78)
    for line in compose_verdict(signal_analysis, math_check, cycle_analysis, order_analysis):
        print(line)
    print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
