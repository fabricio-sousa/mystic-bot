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
        trades = []          # keep defined: the WHEN section reads it
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

    # Denominator = BREACH 1/N lines, one per distinct breach that began confirming.
    # Counting STOP LOSS lines instead over-counts badly: it sweeps in every stop logged
    # before the instrumented build existed, plus retries on a position that already
    # breached, and makes a tiny sample look large. That is a real bug this tool had --
    # it reported 12 events when only 2 were instrumented.
    instrumented = len(breaches)
    drifted = [s for s in stops if s["drift"] is not None]
    print("=" * 66)
    print("STOP CONFIRMATION FILTER — cost vs benefit")
    print("=" * 66)
    if instrumented == 0:
        print("\nNo BREACH lines found. Either no stop has been approached since the")
        print("instrumented build was deployed, or this log predates it.")
        if stops:
            print(f"\n({len(stops)} STOP LOSS line(s) present, but from before")
            print(" instrumentation -- no breach or drift data, so they cannot")
            print(" answer this question.)")
        return 0

    total = instrumented
    print(f"\nInstrumented breach events: {instrumented}   "
          f"recovered: {len(recoveries)}   fired: {instrumented - len(recoveries)}")
    if len(stops) > len(drifted):
        print(f"({len(stops)} STOP LOSS lines total; {len(stops) - len(drifted)} predate")
        print(" instrumentation or are retries, and are excluded from the rate.)")
    if total < 5:
        print(f"\n⚠️  Only {total} instrumented event(s) -- far too few to judge the filter.")
        print("   0 recoveries out of a handful is NOT evidence it never helps: a filter")
        print("   that saves 1 breach in 3 would still show zero this often by chance.")

    print("\n--- BENEFIT: stops the filter prevented ---")
    if recoveries:
        for r in recoveries:
            print(f"  {r['ticker']}: recovered after {r['polls']} poll(s) / {r['secs']}s, "
                  f"bid {r['from_bid']}c -> {r['to_bid']}c")
        print(f"\n  {len(recoveries)} of {total} breaches ({len(recoveries)/total*100:.0f}%) "
              f"recovered before firing.")
        print("  Each avoided a stop. Whether that was lucky or systematic needs volume.")
    else:
        print(f"  None across {instrumented} instrumented breach(es).")
        if instrumented < 5:
            print("  With this few events that says almost nothing.")
        else:
            print("  If this holds up, the confirmation delay is pure cost -- it has")
            print("  prevented nothing while letting the book run.")

    print("\n--- COST: how far the bid moved while confirming ---")
    drifts = drifted
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

    # --- GAP RISK -------------------------------------------------------------
    # The metric that actually explains the bad days: how far the bid had ALREADY
    # fallen past the stop level before the bot's first observation of the breach.
    # The stop level is a trigger, not a floor -- between two polls the book can gap
    # straight through it, and no exit logic recovers value that was gone before the
    # bot ever saw it.
    print("\n--- GAP RISK: how far past the stop the FIRST look already was ---")
    gaps = []
    for br in breaches:
        gap = br["stop"] - br["bid"]          # positive = already below the trigger
        gaps.append((br["ticker"], br["entry"], br["stop"], br["bid"], gap))
    if not gaps:
        print("  No breach observations yet.")
    else:
        for tk, entry, stop, bid, gap in gaps:
            flag = "  <-- GAPPED" if gap >= 10 else ""
            print(f"  {tk}: entry {entry}c, stop {stop}c, first seen {bid}c "
                  f"({gap:+d}c past){flag}")
        vals = sorted(g[4] for g in gaps)
        n = len(vals)
        mean = sum(vals) / n
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        clean = sum(1 for v in vals if v < 5)
        gapped = sum(1 for v in vals if v >= 10)
        print(f"\n  mean {mean:+.1f}c past the trigger, median {median:+.1f}c, "
              f"worst {max(vals):+d}c")
        print(f"  caught within 5c: {clean}/{n} ({clean/n*100:.0f}%)   "
              f"gapped 10c+: {gapped}/{n} ({gapped/n*100:.0f}%)")
        if n < 5:
            print("  (too few to read a distribution from)")
        elif gapped / n > 0.25:
            print("\n  More than a quarter of breaches gap 10c+ past the trigger. On those")
            print("  the stop is not acting as a floor -- the loss is already taken before")
            print("  the first poll. Size on the assumption that a bad trade costs most of")
            print("  the entry, not 20% of it.")
        else:
            print("\n  Most breaches are caught near the trigger, so the stop is behaving")
            print("  like a floor on typical moves. The gapped ones are the tail risk.")

    # --- WHEN ------------------------------------------------------------------
    # Losses have clustered by time of day far more than by anything else. This reads
    # trades.json directly (not the log) so it covers the full record, including trades
    # from before the breach instrumentation existed.
    print("\n--- WHEN: losses by time of day and weekday ---")
    if not trades:
        print("  trades.json not loaded, skipping.")
    else:
        from datetime import datetime as _dt
        from collections import defaultdict as _dd

        def _bucket(h):
            if 0 <= h < 6:   return "00-06 overnight"
            if 6 <= h < 12:  return "06-12 morning"
            if 12 <= h < 18: return "12-18 afternoon"
            return "18-24 evening"

        by_hr, by_day = _dd(lambda: [0, 0, 0.0]), _dd(lambda: [0, 0, 0.0])
        for tr_ in trades:
            try:
                d = _dt.strptime(str(tr_["timestamp"])[:19], "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            pnl = float(tr_.get("pnl") or 0)
            for store, key in ((by_hr, _bucket(d.hour)), (by_day, d.strftime("%a"))):
                store[key][0] += 1
                if pnl < 0:
                    store[key][1] += 1
                store[key][2] += pnl

        print(f"\n  {'window (ET)':<18}{'n':<6}{'losses':<8}{'loss%':<8}{'PnL'}")
        for k in ("00-06 overnight", "06-12 morning", "12-18 afternoon", "18-24 evening"):
            n, l, pl = by_hr[k]
            if n:
                print(f"  {k:<18}{n:<6}{l:<8}{l/n*100:<8.1f}${pl:+.2f}")

        print(f"\n  {'day':<18}{'n':<6}{'losses':<8}{'loss%':<8}{'PnL'}")
        for k in ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"):
            n, l, pl = by_day[k]
            if n:
                print(f"  {k:<18}{n:<6}{l:<8}{l/n*100:<8.1f}${pl:+.2f}")

        ev_n, ev_l, ev_p = by_hr["18-24 evening"]
        rest_n = sum(by_hr[k][0] for k in by_hr if k != "18-24 evening")
        rest_l = sum(by_hr[k][1] for k in by_hr if k != "18-24 evening")
        rest_p = sum(by_hr[k][2] for k in by_hr if k != "18-24 evening")
        if ev_n and rest_n:
            print(f"\n  evening 18-24: {ev_n:>3} trades, {ev_l} losses "
                  f"({ev_l/ev_n*100:.1f}%), ${ev_p:+.2f}")
            print(f"  all other:     {rest_n:>3} trades, {rest_l} losses "
                  f"({rest_l/rest_n*100:.1f}%), ${rest_p:+.2f}")
            if ev_n < 60 or rest_n < 60:
                print("\n  Sample is thin. A gap like this can appear from one bad session,")
                print("  so treat it as a hypothesis to re-check, not a reason to change")
                print("  the schedule yet. Watch whether it persists across separate weeks.")
            elif ev_l / ev_n > (rest_l / rest_n) * 1.8:
                print("\n  The evening loss rate is well above the rest of the day across a")
                print("  reasonable sample now. Worth considering whether to trade it.")

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
