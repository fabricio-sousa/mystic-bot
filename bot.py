"""
Mystic-Bot runner.

Usage:
    python bot.py                 # deploy any investable cash right now, then exit
    python bot.py --dry-run       # show what it WOULD buy, place no orders
    python bot.py --watch         # keep running; deploy cash whenever it appears
    python bot.py --status        # print account + holdings, place no orders

Typical flow: deposit money into Alpaca, then either run `python bot.py` once,
or leave `python bot.py --watch` running so deposits are invested automatically.
"""

import argparse
import sys
import time

import config
from broker import Broker
from rebalancer import run_rebalance


def confirm_live() -> bool:
    if config.PAPER:
        return True
    print("\n" + "=" * 60)
    print(" LIVE TRADING with REAL money is enabled (config.PAPER = False).")
    print("=" * 60)
    answer = input(' Type "TRADE LIVE" to continue, anything else to abort: ')
    return answer.strip() == "TRADE LIVE"


def print_status(broker: Broker):
    acct = broker.get_account()
    positions = broker.get_positions()
    invested = sum(p.market_value for p in positions.values())
    mode = "PAPER" if config.PAPER else "LIVE"

    print(f"\nMystic-Bot [{mode}]  market {'OPEN' if acct.market_open else 'CLOSED'}")
    print(f"  Equity        ${acct.equity:,.2f}")
    print(f"  Cash          ${acct.cash:,.2f}")
    print(f"  Invested      ${invested:,.2f}")
    print(f"  Today         ${acct.equity - acct.last_equity:+,.2f}")
    print(f"\n  {'Sym':<5}{'Value':>12}{'Weight':>9}{'Target':>8}{'P/L %':>9}")
    for s in config.TARGET_WEIGHTS:
        p = positions.get(s)
        if not p:
            print(f"  {s:<5}{'$0.00':>12}{'0.0%':>9}{'10.0%':>8}{'-':>9}")
            continue
        weight = (p.market_value / invested * 100) if invested else 0.0
        value_str = f"${p.market_value:,.2f}"
        weight_str = f"{weight:.1f}%"
        pl_str = f"{p.unrealized_plpc * 100:+.1f}%"
        print(f"  {s:<5}{value_str:>12}{weight_str:>9}{'10.0%':>8}{pl_str:>9}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Mystic-Bot rebalancing runner")
    parser.add_argument("--watch", action="store_true",
                        help="run continuously and deploy cash as it arrives")
    parser.add_argument("--dry-run", action="store_true",
                        help="show intended buys without placing orders")
    parser.add_argument("--status", action="store_true",
                        help="print account + holdings and exit")
    args = parser.parse_args()

    try:
        broker = Broker()
    except Exception as e:
        print(f"Startup failed: {e}")
        sys.exit(1)

    if args.status:
        print_status(broker)
        return

    if not args.dry_run and not confirm_live():
        print("Aborted.")
        return

    if args.watch:
        print(f"Watching for deposits every {config.WATCH_INTERVAL_SECONDS}s. "
              f"Ctrl-C to stop.")
        try:
            while True:
                run_rebalance(broker, dry_run=args.dry_run)
                time.sleep(config.WATCH_INTERVAL_SECONDS)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        run_rebalance(broker, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
