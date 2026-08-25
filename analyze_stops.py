"""
Measure whether STOP_CONFIRM_LOOPS is earning its keep.

The confirmation filter trades one cost against one benefit:

  COST     waiting N polls lets the book keep falling, so the exit fills
           further below the trigger than it would have on first breach
  BENEFIT  some breaches recover before the Nth poll, so a stop that would
           have fired (and locked in a loss) never happens

Neither was measurable until the bot started logging BREACH lines. This reads
log.txt, pairs the breach events with their outcomes, and reports both sides.

    python analyze_stops.py [log.txt] [trades.json]

Run it after a few days of stops. Under ~5 breach events the numbers are
noise; the script says so rather than pretending otherwise.
"""

import json
import re
import sys
from collections import defaultdict

LOG = sys.argv[1] if len(sys.argv) > 1 else "log.txt"
TRADES = sys.argv[2] if len(sys.argv) > 2 else "trades.json"

RE_BREACH = re.compile(
    r"BREACH (\d+)/(\d+) on (\S+): bid (\d+)c <= stop (\d+)c \(entry (\d+)c\)")
RE_RECOVER = re.compile(
    r"BREACH RECOVERED on (\S+) after (\d+)/(\d+) poll\(s\), (\d+)s: bid (\d+)c -> (\d+)c")
RE_STOP = re.compile(
    r"STOP LOSS: Selling (\S+) \(confirmed over (\d+) consecutive polls\)"
    r"(?: \| bid moved (\d+)c -> (\d+)c \(([+-]\d+)c\) over (\d+)s)?")
RE_EXITED = re.compile(r"Exited (\d+)/(\d+) on (\S+) at avg (\d+)c")


def main():
    try:
        lines = open(LOG, errors="ignore").read().splitlines()
    except FileNotFoundError:
        print(f"Could not open {LOG}")
        return 1

    try:
        trades = json.load(open(TRADES))
        pnl_by_ticker = defaultdict(float)
        for t in trades:
            if t.get("type") == "STOP_LOSS":
                pnl_by_ticker[t["ticker"]] += float(t.get("pnl") or 0)
    except Exception:
        pnl_by_ticker = {}

    breaches, recoveries, stops, exits = [], [], [], {}
    for ln in lines:
        m = RE_BREACH.search(ln)
        if m:
            breaches.append(dict(ticker=m.group(3), bid=int(m.group(4)),
                                 stop=int(m.group(5)), entry=int(m.group(6))))
        m = RE_RECOVER.search(ln)
        if m:
            recoveries.append(dict(ticker=m.group(1), polls=int(m.group(2)),
                                   secs=int(m.group(4)),
                                   from_bid=int(m.group(5)), to_bid=int(m.group(6))))
        m = RE_STOP.search(ln)
        if m:
            stops.append(dict(ticker=m.group(1),
                              first_bid=int(m.group(3)) if m.group(3) else None,
                              exec_bid=int(m.group(4)) if m.group(4) else None,
                              drift=int(m.group(5)) if m.group(5) else None,
                              secs=int(m.group(6)) if m.group(6) else None))
        m = RE_EXITED.search(ln)
        if m:
            exits[m.group(3)] = dict(filled=int(m.group(1)), of=int(m.group(2)),
                                     avg=int(m.group(4)))

    total = len(recoveries) + len(stops)
    print("=" * 66)
    print("STOP CONFIRMATION FILTER — cost vs benefit")
    print("=" * 66)
    if total == 0:
        print("\nNo BREACH lines found. Either no stop has been approached since the")
        print("instrumented build was deployed, or this log predates it.")
        return 0

    print(f"\nBreach events: {total}   recovered: {len(recoveries)}   fired: {len(stops)}")
    if total < 5:
        print("\n⚠️  Fewer than 5 events — treat everything below as anecdote, not signal.")

    print("\n--- BENEFIT: stops the filter prevented ---")
    if recoveries:
        for r in recoveries:
            print(f"  {r['ticker']}: recovered after {r['polls']} poll(s) / {r['secs']}s, "
                  f"bid {r['from_bid']}c -> {r['to_bid']}c")
        print(f"\n  {len(recoveries)} of {total} breaches ({len(recoveries)/total*100:.0f}%) "
              f"recovered before firing.")
        print("  Each avoided a stop. Whether that was lucky or systematic needs volume.")
    else:
        print("  None. Every breach observed went on to fire a stop.")
        print("  If this holds up, the confirmation delay is pure cost — it has")
        print("  prevented nothing while letting the book run.")

    print("\n--- COST: how far the bid moved while confirming ---")
    drifts = [s for s in stops if s["drift"] is not None]
    if drifts:
        for s in drifts:
            realized = exits.get(s["ticker"], {}).get("avg")
            extra = f"  (exited at {realized}c)" if realized else ""
            print(f"  {s['ticker']}: {s['first_bid']}c -> {s['exec_bid']}c "
                  f"({s['drift']:+d}c) over {s['secs']}s{extra}")
        avg = sum(s["drift"] for s in drifts) / len(drifts)
        print(f"\n  mean drift while waiting: {avg:+.1f}c per stop")
        if pnl_by_ticker:
            tot = sum(pnl_by_ticker.get(s["ticker"], 0) for s in drifts)
            print(f"  realised PnL on these stops: ${tot:+.2f}")
        if avg > 0:
            print(f"  Positive drift = the bid fell while waiting. Firing on first")
            print(f"  breach would have exited roughly {avg:.0f}c higher per stop.")
    else:
        print("  No stops with drift data yet.")

    print("\n--- READ ---")
    if recoveries and drifts:
        save_rate = len(recoveries) / total
        avg_drift = sum(s["drift"] for s in drifts) / len(drifts)
        print(f"  The filter saves {save_rate*100:.0f}% of breaches from becoming stops,")
        print(f"  and costs about {avg_drift:.0f}c of exit price on the ones that do fire.")
        print("  Lowering STOP_CONFIRM_LOOPS trades the first away for the second.")
        print("  Compare a saved stop (roughly a full loss avoided) against the drift")
        print("  cost times the number of stops to see which way it nets out.")
    else:
        print("  Need both recoveries and fired stops before the tradeoff can be judged.")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
