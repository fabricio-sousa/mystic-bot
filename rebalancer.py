"""
The brain of Mystic-Bot.

Strategy (buy-only rebalancing):
  Every dollar of investable cash is steered toward the names that are *furthest
  below* their 10% target. Nothing is ever sold. Over repeated deposits the
  portfolio converges on equal weights, because laggards get topped up first.

How the split is computed for a deposit of `cash`:
  base        = (value of the 10 holdings) + cash      # what we'll own after
  target_i    = weight_i * base                         # 10% of that
  deficit_i   = max(0, target_i - current_value_i)      # how far below target
  buy_i       = cash * deficit_i / sum(deficits)        # share of cash, pro-rata

Names already at or above target get a deficit of 0, so they're bought less (or
not at all) -- exactly the "buy more of the laggards, less of the leaders"
behaviour, without selling anything.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone

import config
from broker import Broker


def floor_cents(x: float) -> float:
    """Round down to whole cents so the sum of buys never exceeds the cash."""
    return math.floor(x * 100) / 100.0


def compute_buys(values: dict[str, float], cash: float,
                 weights: dict[str, float] = None) -> dict[str, float]:
    """Pure allocation math. No I/O -- easy to test.

    values:  current market value held per symbol (0 if none held)
    cash:    investable dollars to deploy now
    returns: {symbol: usd_to_buy} (cents-floored, only entries >= MIN_ORDER_USD)
    """
    weights = weights or config.TARGET_WEIGHTS
    if cash <= 0:
        return {}

    held = sum(values.get(s, 0.0) for s in weights)
    base = held + cash

    deficits = {s: max(0.0, weights[s] * base - values.get(s, 0.0))
                for s in weights}
    total_deficit = sum(deficits.values())

    if total_deficit <= 0:
        # Everything already at/above target (only possible if cash ~0).
        # Fall back to plain target weights.
        raw = {s: cash * weights[s] for s in weights}
    else:
        raw = {s: cash * deficits[s] / total_deficit for s in weights}

    buys = {}
    for s, usd in raw.items():
        usd = floor_cents(usd)
        if usd >= config.MIN_ORDER_USD:
            buys[s] = usd
    return buys


def _log_run(message: str):
    config.LOG_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    with config.RUN_LOG.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {message}\n")
    print(message)


def _log_orders(rows: list[dict]):
    config.LOG_DIR.mkdir(exist_ok=True)
    new = not config.ACTIVITY_LOG.exists()
    with config.ACTIVITY_LOG.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["timestamp", "symbol", "usd", "status"])
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def run_rebalance(broker: Broker, dry_run: bool = False) -> dict:
    """Check for investable cash and deploy it. Returns a summary dict."""
    account = broker.get_account()
    positions = broker.get_positions()

    investable = account.cash - config.CASH_RESERVE_USD
    values = {s: positions[s].market_value if s in positions else 0.0
              for s in config.TARGET_WEIGHTS}

    summary = {"investable_cash": investable, "buys": {}, "submitted": [],
               "skipped_reason": None}

    if investable < config.MIN_DEPLOY_USD:
        summary["skipped_reason"] = (
            f"Only ${investable:,.2f} investable (min ${config.MIN_DEPLOY_USD:,.2f}). "
            f"Nothing to do.")
        _log_run(summary["skipped_reason"])
        return summary

    if not config.ALLOW_OUTSIDE_REGULAR_HOURS and not account.market_open:
        summary["skipped_reason"] = "Market closed and outside-hours trading is off."
        _log_run(summary["skipped_reason"])
        return summary

    # Don't redeploy cash that's already committed to working orders.
    busy = broker.open_order_symbols() & set(config.TARGET_WEIGHTS)
    if busy:
        summary["skipped_reason"] = f"Open orders still working for {sorted(busy)}; waiting."
        _log_run(summary["skipped_reason"])
        return summary

    buys = compute_buys(values, investable)
    summary["buys"] = buys

    if not buys:
        summary["skipped_reason"] = "No single buy clears the $1 minimum."
        _log_run(summary["skipped_reason"])
        return summary

    total = sum(buys.values())
    _log_run(f"Deploying ${total:,.2f} of ${investable:,.2f} across {len(buys)} names"
             + (" [DRY RUN]" if dry_run else ""))
    for s, usd in sorted(buys.items(), key=lambda kv: -kv[1]):
        _log_run(f"  {s:<5} ${usd:,.2f}")

    if dry_run:
        return summary

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    rows = []
    for s, usd in buys.items():
        try:
            broker.buy_notional(s, usd)
            status = "submitted"
        except Exception as e:  # keep going so one bad symbol doesn't stop the rest
            status = f"error: {e}"
            _log_run(f"  !! {s} failed: {e}")
        rows.append({"timestamp": stamp, "symbol": s, "usd": f"{usd:.2f}",
                     "status": status})
        summary["submitted"].append({"symbol": s, "usd": usd, "status": status})
    _log_orders(rows)
    return summary
