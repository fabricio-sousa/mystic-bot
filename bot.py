import os
import sys
import json
import time
import math
import uuid
from datetime import datetime, timedelta
import pytz
from kalshi_python_sync import Configuration, KalshiClient

# ---------------------------------------------------------------------------
# SDK resilience patch: tolerate null booleans in the Market model.
# During Kalshi API partial outages, the /markets endpoint can return null
# for fields the SDK types as a strict bool (e.g. fractional_trading_enabled,
# can_close_early), which makes Pydantic raise a validation error and kills
# the loop. We relax every strict-bool field on the Market model to
# Optional[bool] (default None) so a missing/null flag is parsed as None
# instead of crashing. This only affects local parsing — it never changes
# what we send to Kalshi — and is a no-op once the API returns proper bools.
try:
    from typing import Optional as _Optional
    from kalshi_python_sync.models.market import Market as _Market
    _patched_bool_fields = 0
    for _name, _fi in list(_Market.model_fields.items()):
        if _fi.annotation is bool or str(_fi.annotation) == "bool":
            _fi.annotation = _Optional[bool]
            _fi.default = None
            _patched_bool_fields += 1
    if _patched_bool_fields:
        _Market.model_rebuild(force=True)
except Exception as _patch_err:  # never let the patch itself break startup
    print(f"[warn] Market bool-tolerance patch skipped: {_patch_err}")

# Windows-only tools
try:
    import winsound
    import msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False

# ====================== CONFIG ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# --- Environment: prod vs demo sandbox ---
# DEMO_MODE routes live order placement to Kalshi's SANDBOX (demo) exchange, which
# uses mock funds — real V2 orders, fake money. Use this to prove the V2 order path
# (especially the bid/ask + NO-price-inversion mapping) end-to-end before trading
# real capital. Requires a SEPARATE demo account + demo API keys from demo.kalshi.co.
# When True, PAPER_MODE must be False (you want real order calls, just on the sandbox).
# NOTE: defined here (before the key-file paths) because those paths depend on it.
DEMO_MODE = True              # True = place real orders against the demo sandbox host.
PROD_HOST = "https://api.elections.kalshi.com/trade-api/v2"
DEMO_HOST = "https://demo-api.kalshi.co/trade-api/v2"

# Demo and prod are SEPARATE accounts with SEPARATE API keys. Keep two key-file sets
# so credentials never mix: drop demo keys into apikey_demo.txt / private_demo.txt.
# The correct set is auto-selected by DEMO_MODE — no manual swapping.
APIKEY_FILE  = os.path.join(BASE_DIR, "apikey_demo.txt"  if DEMO_MODE else "apikey.txt")
PRIVATE_FILE = os.path.join(BASE_DIR, "private_demo.txt" if DEMO_MODE else "private.txt")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
UNFILLED_ATTEMPTS_FILE = os.path.join(BASE_DIR, "unfilled_attempts.json")

# --- Trading mode ---
PAPER_MODE = True              # Shadow/paper trading. No real orders are placed.
PAPER_START_BALANCE = 500.0   # Simulated starting cash; moves with realized PnL.
PAPER_SAFETY_FLOOR = 0.0       # Paper floor (live SAFETY_FLOOR would block trading from $1000).

FLAT_RISK = 0.05               # Fraction of cash staked per entry (before caps below).
                               # v6.1.0: 5% confirmed by MC (20k sims, loss rates up to
                               # the 0.7% 95% UB): P(10%-halt) ~4% @ 5% risk vs 40-59%
                               # @ 10% risk — one full 96c loss at 10% is ~-9.6%, an
                               # effectively guaranteed sticky halt.
MAX_POSITION_DOLLARS = 500.0   # Dollar ceiling per entry.

# Hard per-market contract cap — an INDEPENDENT safety layer on top of dollar/risk
# sizing. The bot will never hold more than this many contracts in a single market,
# combined across entries. This is a backstop against the 2026-07-10 over-buy bug,
# where a position read-back failure let the bot re-enter repeatedly and stack ~70+
# contracts in seconds. Enforcement is fail-safe: if the current position can't be
# read, the bot SKIPS the entry rather than assuming flat. Set to a size you're
# comfortable holding to settlement on one 15-min contract. 0 disables the cap.
MAX_CONTRACTS_PER_MARKET = 150
FEE_RATE = 0.07                # Kalshi trading-fee rate for paper/sim PnL. VERIFY against the
                               # current KXBTC15M schedule (get_series_fee_changes); fees change.

# --- Entry ---
MAX_SLIPPAGE = 1               # v6.4.0: was 0. With 0, the IOC order was only
                               # marketable if the ask hadn't moved at ALL since
                               # the price used to build it was read — no cushion
                               # for the real network latency of the RSI check in
                               # between (see the fresh re-check added below,
                               # which is the primary fix; this is the backstop
                               # for whatever latency remains after that).
ENTRY_TIME_MIN = 1.0           # Minutes-before-close window start.
ENTRY_TIME_MAX = 10.0          # Minutes-before-close window end.

# --- RSI filter ---
# Backtesting on real Kalshi data (Apr-Jun 2026, 1,435 entries) showed that 96c
# entries with RSI-14 < 55 won only 12% of the time — far below the 96.27% breakeven.
# Entries with RSI-14 >= 55 won 98.63%, producing +84.2% ROI with a $573 max DD
# vs +49.6% / $2,317 without the filter. The filter cuts ~40% of trade volume but
# eliminates losing months entirely on the 3-month test window.
# RSI is computed from 1-min KXBTC15M candles (BTC-implied price) spanning the current
# open market plus recently-settled ones — same series the bot already reads, no
# external data source needed. See compute_rsi() for the multi-market merge.
# v6.1.0 finding (May-Jul re-test): because RSI runs on the YES price series,
# RSI>=55 structurally suppresses NO-side entries (a 4c yes print = falling
# series = low RSI). Unfiltered NO entries won only ~57% in-sample and cratered
# the account — this filter is doing double duty as the side selector, and its
# value is regime-dependent (BTC uptrend). Do NOT disable without re-running
# backtest.py.
USE_RSI_FILTER = True          # Set False to disable and trade all 96c prints.
RSI_MIN = 55                   # Skip entries where RSI-14 is below this threshold.
RSI_LOOKBACK_MIN = 60          # Minutes of candle history to fetch (need >= 15 valid closes).

# --- FOMC skip ---
# FOMC decision days (rate announcement at 2:00 PM ET) produced 83-91% win rates
# vs the 96.27% breakeven — the only macro release type that hurt RSI-filtered entries.
# CPI, PPI, and NFP days all hit 100% (RSI filter already handled those).
# Skipping FOMC days improves ROI from +71.7% to +82.8% and cuts max DD from $900
# to $768 on the 3-month backtest. Add the FOMC decision date (the second day of
# each two-day meeting) in YYYY-MM-DD format. Update this list each year.
SKIP_FOMC_DAYS = True
FOMC_DECISION_DATES = {
    # 2026 — source: federalreserve.gov/monetarypolicy/fomccalendars.htm
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",   # backtested — 83.3% win rate (2 losses on 12 trades)
    "2026-06-17",   # backtested — 90.9% win rate (1 loss on 11 trades)
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
}

# --- Two-stage stop ---
# v6.1.0: OFF (hold-to-settle). The earlier Apr-Jun backtest favored a stop, but
# the May 17-Jul 21 2026 re-test on real 1-min candles (6,307 markets, 434
# entries under the live filter set) reversed that: ALL 35 stop-outs were
# whipsaws — every stopped market settled as a WINNER, including 31 that dipped
# <=40c inside the final 2 minutes. Dip depth carried zero information about
# settlement; the 75->60 stop cost -$773 vs holding, and every arm/trigger and
# time-conditional variant tested also lost money while still tripping the 10%
# drawdown halt within the first week. Tail risk is now handled by
# FLAT_RISK=0.05 + the drawdown halt instead of a price stop.
# Params left in place for re-enable if the regime turns (watch the monthly
# backtest.py re-run: if dip-recovery rates degrade, revisit).
USE_STOP = False
STOP_ARM_PRICE = 75            # Only used if USE_STOP=True. Begin monitoring once bid <= this.
STOP_TRIGGER_PRICE = 60        # Only used if USE_STOP=True. Exit once armed & bid <= this.

# --- Risk rails ---
# Live cash floor: the bot won't open trades if settled cash is below this. On the
# demo sandbox we drop it to a token amount so testing doesn't require a large mock
# balance — the real protective floor still applies to actual live trading.
SAFETY_FLOOR_LIVE = 300.0     # Real live-mode cash floor.
SAFETY_FLOOR_DEMO = 10.0      # Sandbox floor (mock funds); keeps demo runnable on a small balance.
SAFETY_FLOOR = SAFETY_FLOOR_DEMO if DEMO_MODE else SAFETY_FLOOR_LIVE
# --- Consecutive-loss circuit breaker ---
# Counts LOSING STREAKS (reset to 0 on any win), not total losses. Halts the bot
# when the streak reaches the limit. At a ~55% win rate a 3-loss streak is normal
# variance (~9% of any 3-trade run), so a limit of 3 trips constantly and is wrong
# for this strategy. Purpose of the breaker is "something is BROKEN" (feed stale,
# signal inverted), not "I lost three coin-flips": an 8-loss streak is ~0.2% under
# normal variance, so it means something's actually wrong.
#   - Paper mode: None => disabled, so data-collection runs never get cut short.
#   - Live mode : 8-loss streak halt (tune once live data shows the real variance).
# Strike COUNTING stays active either way, so streaks still show in the logs.
STRIKE_LIMIT_LIVE = 8          # Live-mode consecutive-loss halt threshold.
STRIKE_LIMIT = None if PAPER_MODE else STRIKE_LIMIT_LIVE
FILL_POLL_TRIES = 4            # (Unused since V2/IOC migration — IOC auto-cancels the unfilled remainder server-side.)

# --- Drawdown circuit breaker (LIVE MODE ONLY) ---
# Tracks a high-water mark (the highest settled balance ever seen) in state.json
# and halts the bot if the current settled balance falls by more than
# MAX_DRAWDOWN_PCT from that peak. Unlike the cash floor (an absolute dollar
# line) this is a RELATIVE guard that scales with the account: a 10% default
# means "if I'm ever down 10% from my best, stop and let me look."
#
# IMPORTANT: this is a peak-to-current drawdown, i.e. balance <= peak * (1 - pct).
# With MAX_DRAWDOWN_PCT = 0.10 and a $2,000 peak, the bot halts at $1,800.
# Adjust the percentage to your risk tolerance (0.05 = tighter, 0.20 = looser).
#
# When tripped, the bot writes a persistent `halted` flag to state.json and
# exits. It will NOT resume on restart — you must clear the halt yourself
# (delete the "halted" key in state.json, or run: python bot.py --reset-halt)
# so a drawdown stop is always a deliberate human decision to re-enter, never
# an automatic restart into a losing streak. Paper mode ignores this entirely.
USE_DRAWDOWN_LIMIT = True      # Live-mode only; no-op in paper mode.
MAX_DRAWDOWN_PCT = 0.10        # Halt if settled balance falls this fraction below its peak.

OVERRIDE_TRIGGERED = False
_last_skip_logged_ticker = None  # Tracks last ticker we logged an RSI skip for (avoids spam)
SESSION_PNL = 0.00

# ====================== TRADING SCHEDULE ======================
def in_trading_window(now=None):
    """v6.2.0->v6.3.0: Schedule A reinstated. Trade all hours EXCEPT
    17:00-21:59 ET on weekdays, plus the Sunday afternoon window; Saturday
    closed. Briefly replaced with 24/7 after a May-Jul re-test showed flat
    win rates across every hour/day in that sample — reverted back to
    Schedule A. See _in_trading_window_247() to switch back if a future
    backtest.py re-run supports it again.

    Returns True if the bot is allowed to open new trades right now."""
    now = now or datetime.now(pytz.timezone("US/Eastern"))
    day = now.weekday()   # 0=Mon ... 5=Sat, 6=Sun
    t = now.hour + now.minute / 60.0

    # Weekdays: all hours except 17:00–22:00
    if 0 <= day <= 4:
        return not (17.0 <= t < 22.0)

    # Sunday: keep the original afternoon window (avoids the thin early-week book)
    if day == 6:
        return 12.0 <= t < 17.0

    # Saturday: closed
    return False

def _in_trading_window_247(now=None):
    """v6.2.0's 24/7 variant. Backtest 2026-05-17 to 07-21 (805 entries under
    the live filter set) showed win rate flat across every hour and day,
    including the weekday 17:00-22:00 window and Saturdays that Schedule A
    excludes (excluded set alone: 370/371 = 99.73%, +$779). Not reproduced
    from the earlier Apr-Jun sample that originally justified Schedule A —
    two windows disagreed, so Schedule A was reinstated as the conservative
    default. Revisit with a fresh backtest.py run before re-enabling."""
    return True

def is_fomc_day(now=None):
    """Return True if today is a configured FOMC decision date and SKIP_FOMC_DAYS
    is enabled. FOMC announcements at 2:00 PM ET inject directional uncertainty
    that RSI cannot anticipate — backtesting showed 83–91% win rates on these days
    vs 99%+ on all other days."""
    if not SKIP_FOMC_DAYS:
        return False
    now = now or datetime.now(pytz.timezone("US/Eastern"))
    return now.strftime("%Y-%m-%d") in FOMC_DECISION_DATES

def next_window_open(now=None):
    """Return the next datetime at which in_trading_window() becomes True.

    Steps forward one minute at a time so the reported time lands exactly on the
    schedule boundary (a coarser step overshoots — e.g. probing from 21:59 in
    15-min hops reports 22:14 when the window really opens at 22:00). Capped at
    8 days of lookahead; returns None if nothing opens in that span.

    Only ever called on the IDLE path, and the idle loop sleeps 10s between
    ticks, so the worst-case ~11.5k cheap arithmetic checks are negligible.
    """
    now = now or datetime.now(pytz.timezone("US/Eastern"))
    probe = now.replace(second=0, microsecond=0)
    for _ in range(8 * 24 * 60):         # 8 days of 1-min steps
        probe += timedelta(minutes=1)
        if in_trading_window(probe) and not is_fomc_day(probe):
            return probe
    return None

def bot_status(now=None, has_position=False):
    """Describe whether the bot can currently OPEN new trades, and if not, why.

    Returns (label, detail) where label is one of:
        "LIVE"    - inside the schedule, free to open new entries
        "IDLE"    - cannot open new entries right now (detail says why)

    Note this describes *entry eligibility*, not whether the process is running.
    An open position is still monitored (and can still stop out) while IDLE —
    the schedule gates new entries only, so that's called out explicitly.
    """
    now = now or datetime.now(pytz.timezone("US/Eastern"))

    if is_fomc_day(now):
        reason = "FOMC day"
    elif not in_trading_window(now):
        reason = "outside schedule"
    else:
        return ("LIVE", "in trading window")

    # Idle — say when we next wake up, so a glance at the heartbeat answers
    # "is it broken or just waiting?"
    nxt = next_window_open(now)
    if nxt is None:
        detail = reason
    else:
        delta = nxt - now
        hrs, rem = divmod(int(delta.total_seconds()), 3600)
        mins = rem // 60
        eta = f"{hrs}h{mins:02d}m" if hrs else f"{mins}m"
        detail = f"{reason}, opens {nxt.strftime('%a %H:%M')} ({eta})"

    if has_position:
        detail += " — holding, stop still active"
    return ("IDLE", detail)

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"[{ts}] {msg}\n")

def ensure_aware(dt):
    """Kalshi returns UTC timestamps. Guard against a naive datetime so tz math
    can't silently raise a TypeError that gets swallowed by the outer loop."""
    if dt is None: return None
    if dt.tzinfo is None: return pytz.utc.localize(dt)
    return dt

def compute_fee_cents(price_cents, count):
    """Kalshi trading fee (cents), standard 7% formula: ceil(0.07 * C * P * (1-P)).
    Used for paper PnL only; live fees come straight from the order object."""
    p = max(0.0, min(1.0, (price_cents or 0) / 100.0))
    return math.ceil(FEE_RATE * count * p * (1.0 - p) * 100.0)

def compute_rsi(current_ticker):
    """Compute RSI-14 from recent KXBTC15M 1-min yes_ask candles.

    A single KXBTC15M market only lives ~20 minutes, so we span the
    current open market plus the 2 most recently settled ones over the
    RSI_LOOKBACK_MIN window — reliably yielding 35+ closes for RSI-14.

    yes_ask.close is used as the price series. Falls back to yes_bid.close
    if yes_ask.close is None (can happen on the still-open current candle).

    Returns None if insufficient data or the API call fails; caller skips."""
    if not USE_RSI_FILTER:
        return None
    try:
        now_ts = int(time.time())
        window = RSI_LOOKBACK_MIN * 60

        # 1. Collect tickers: current open market + 2 recently settled
        tickers = [current_ticker]
        try:
            resp_s = client.get_markets(
                series_ticker  = "KXBTC15M",
                status         = "settled",
                min_settled_ts = now_ts - window,   # settled within the lookback window
                limit          = 2,
            )
            for m in getattr(resp_s, "markets", []) or []:
                if m.ticker != current_ticker:
                    tickers.append(m.ticker)
        except Exception as e:
            # 2026-07-23: this was a silent `pass`, which is why the two "1/1
            # markets" RSI stalls in the paper log had no diagnosable cause.
            # RSI_LOOKBACK_MIN=60 should reliably catch 2+ settled markets
            # (one settles every ~15min), so a fallback to just current_ticker
            # means this call itself is failing — now visible instead of silent.
            log(f"⚠️ RSI: settled-market lookup failed ({e}) — "
                f"falling back to current market only")

        # 2. Batch fetch 1-min candles across all collected tickers
        batch = client.batch_get_market_candlesticks(
            market_tickers  = ",".join(tickers),
            start_ts        = now_ts - window,
            end_ts          = now_ts,
            period_interval = 1,
        )

        # 3. Prefer a PURE ask-price series. Mixing ask and bid quotes within one
        # series introduces artificial jumps (the bid-ask spread) that distort the
        # Wilder RSI computation. Only fall back to blending in bid-based closes
        # (for the rare candle missing a firm ask — typically the still-forming
        # candle of the current open market) if the pure-ask series doesn't reach
        # the 15-close minimum on its own. This keeps the common case clean and
        # only accepts the noisier blended series as a last resort, logged so it's
        # auditable. Kalshi migrated live candlestick OHLC to *_dollars string
        # fields (e.g. "0.5600"); older SDK builds exposed a bare `close` in
        # cents. Read close_dollars first, fall back to close, so this works either way.
        def _close(side_obj):
            if side_obj is None:
                return None
            v = getattr(side_obj, "close_dollars", None)
            if v is None:
                v = getattr(side_obj, "close", None)
            return v

        def _dedup_sorted(pairs):
            pairs = sorted(pairs, key=lambda x: x[0])
            seen, out = set(), []
            for ts, v in pairs:
                if ts not in seen:
                    seen.add(ts); out.append(v)
            return out

        mkts = getattr(batch, "markets", []) or []
        raw = 0                                   # total candle rows returned
        ask_candles = []
        bid_fallback_candles = []
        for mkt in mkts:
            cs = getattr(mkt, "candlesticks", []) or []
            raw += len(cs)
            for c in cs:
                ts = getattr(c, "end_period_ts", None)
                if ts is None:
                    continue
                v_ask = _close(getattr(c, "yes_ask", None))
                if v_ask is not None:
                    ask_candles.append((ts, float(v_ask)))
                else:
                    v_bid = _close(getattr(c, "yes_bid", None))
                    if v_bid is not None:
                        bid_fallback_candles.append((ts, float(v_bid)))

        # 4. Sort, dedupe. Use pure-ask if it's enough; otherwise blend.
        closes = _dedup_sorted(ask_candles)
        if len(closes) < 15 and bid_fallback_candles:
            pure_count = len(closes)
            closes = _dedup_sorted(ask_candles + bid_fallback_candles)
            log(f"⚠️ RSI: pure-ask series had only {pure_count} closes — blended in "
                f"{len(bid_fallback_candles)} bid-fallback point(s) to reach {len(closes)} "
                f"(accepting some ask/bid basis noise to avoid starving the window)")

        if len(closes) < 15:
            log(f"⚠️ RSI: {len(closes)} usable / {raw} raw candles across "
                f"{len(mkts)}/{len(tickers)} markets — skipping (need 15+)")
            return None

        # 5. Wilder RSI-14 via simple-average seed
        period = 14
        gains  = [max(0.0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
        losses = [max(0.0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
        avg_g  = sum(gains[:period])  / period
        avg_l  = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i])  / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l == 0:
            return 100.0
        return round(100.0 - 100.0 / (1.0 + avg_g / avg_l), 1)

    except Exception as e:
        log(f"⚠️ RSI fetch error: {e} — skipping entry")
        return None

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except: pass
    return {"strikes": 0, "current_trade": None}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f: json.dump(state, f, indent=2)

def interruptible_sleep(total_seconds, chunk=1.0):
    """Sleep in small chunks, staying responsive to the keyboard override/exit
    during long waits (e.g. the settlement-finalization pause) instead of one
    flat, unresponsive time.sleep(). ESC exits immediately; 'c' sets the same
    OVERRIDE_TRIGGERED flag the main loop checks, so the pending flatten runs
    as soon as this wait ends rather than being silently delayed."""
    global OVERRIDE_TRIGGERED
    elapsed = 0.0
    while elapsed < total_seconds:
        if HAS_WINDOWS and msvcrt.kbhit():
            key = msvcrt.getch()
            if key == b'\x1b': os._exit(0)
            elif key.lower() == b'c': OVERRIDE_TRIGGERED = True
        time.sleep(min(chunk, total_seconds - elapsed))
        elapsed += chunk

def update_trades_json(trade_entry):
    trades = []
    trade_entry["category"] = "paper" if PAPER_MODE else "bot"
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            try: trades = json.load(f)
            except: trades = []
    trades.append(trade_entry)
    with open(TRADES_FILE, "w", encoding="utf-8") as f: json.dump(trades, f, indent=2)

def log_unfilled_attempt(entry):
    """v6.4.0: real telemetry for missed entries, replacing the bare console
    log. Two distinct causes get recorded so they can be told apart:
      'price_moved_before_submit' — the fresh re-check (added in v6.4.0) saw
          the ask had already moved past our slippage tolerance, so we never
          sent an order at all (no wasted IOC attempt).
      'ioc_unfilled'              — an order WAS submitted and the exchange
          couldn't fill it (or only partially) before the IOC auto-cancel.
    Fields: reason, ticker, side, stale_price_cents (price read at the top of
    the loop, before RSI), fresh_price_cents (price re-checked right before
    submit, None if reason=ioc_unfilled since that path skips the extra call),
    qty_attempted, rsi_at_entry, time_left_min, timestamp."""
    attempts = []
    if os.path.exists(UNFILLED_ATTEMPTS_FILE):
        with open(UNFILLED_ATTEMPTS_FILE, "r", encoding="utf-8") as f:
            try: attempts = json.load(f)
            except: attempts = []
    attempts.append(entry)
    with open(UNFILLED_ATTEMPTS_FILE, "w", encoding="utf-8") as f:
        json.dump(attempts, f, indent=2)

def safe_price_cents(value) -> int:
    try: return int(round(float(value or 0) * 100))
    except: return 0

def play_sound(event_type):
    if not HAS_WINDOWS: return
    s = {"buy":[(2000,200)], "settle_win":[(2500,200),(3000,200)], "settle_loss":[(600,500)], "stop":[(400,1000)]}
    for f, d in s.get(event_type, []): winsound.Beep(f, d)

def apply_pnl(state, pnl):
    """Book realized PnL into the session tally and, in paper mode, the paper balance."""
    global SESSION_PNL
    SESSION_PNL += pnl
    if PAPER_MODE:
        state["paper_balance"] = round(state.get("paper_balance", PAPER_START_BALANCE) + pnl, 2)

def mark_ticker_entered(state, ticker):
    """Record a CONFIRMED fill (fresh buy or adopted position) on `ticker`.

    This backs an unconditional "never buy this ticker again this session" guard,
    independent of get_exchange_position, current_trade, or any other read
    succeeding. It closes a gap that a purely fill/position-based guard doesn't:
    e.g. stop-loss exits a position with time still left before close, and
    `current_trade` goes back to None — without this, the entry gate would happily
    re-enter the SAME 15-min contract a second time. Once a ticker is in this set,
    the fresh-buy path refuses it outright, however many times the loop runs.

    Does NOT gate the unfilled-order retry loop: this is only called after a
    confirmed fill (filled > 0), so the normal "keep trying at 96c until it lands"
    behavior for an order that hasn't filled yet is untouched.

    Bounded to the most recent 200 tickers — each KXBTC15M contract only exists
    for ~20 minutes and is never reused, so 200 comfortably covers 48+ hours of
    history without the list growing unbounded over a long-running session."""
    et = state.get("entered_tickers", [])
    if ticker not in et:
        et.append(ticker)
    state["entered_tickers"] = et[-200:]

def update_peak_balance(state, balance):
    """Track the high-water mark of settled balance for the drawdown circuit breaker.
    Records the highest balance ever seen in state.json so the drawdown guard has a
    stable reference across restarts. Returns the current peak."""
    peak = state.get("peak_balance")
    if peak is None or balance > peak:
        state["peak_balance"] = round(balance, 2)
        peak = state["peak_balance"]
    return peak

def check_drawdown_halt(state, balance):
    """LIVE-MODE drawdown circuit breaker. Returns (should_halt, peak, floor_price).

    Compares the current settled balance against the high-water mark. If balance has
    fallen more than MAX_DRAWDOWN_PCT below the peak, signals a halt. Paper mode and
    the disabled flag both short-circuit to no-halt. The peak is only meaningful once
    at least one balance has been recorded, so a fresh account never trips on startup."""
    if PAPER_MODE or not USE_DRAWDOWN_LIMIT:
        return False, state.get("peak_balance"), None
    peak = update_peak_balance(state, balance)
    if peak is None or peak <= 0:
        return False, peak, None
    halt_level = peak * (1.0 - MAX_DRAWDOWN_PCT)
    return balance <= halt_level, peak, halt_level

# ====================== API SETUP ======================
# NOTE: Even paper mode needs API keys to read live market data (prices, results).
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

_active_host = DEMO_HOST if DEMO_MODE else PROD_HOST
config = Configuration(host=_active_host)
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

# ====================== EXCHANGE TRUTH HELPERS ======================
class PositionUnknown(Exception):
    """Raised when the exchange position can't be read (API/parse error), so callers
    can distinguish 'genuinely flat (0)' from 'we don't know'. Critical for the
    position cap: an unknown position must NEVER be treated as 0, or the bot could
    re-buy the full cap on top of an existing position (the 2026-07-10 over-buy bug)."""
    pass

def get_exchange_position(ticker, strict=False):
    """Signed net contracts held in `ticker` per the exchange (live only).
    Positive = long YES, negative = long NO, 0 = flat. Paper mode has no real position.

    If strict=True, raise PositionUnknown on any read/parse failure instead of
    returning 0, so safety-critical callers don't mistake an error for 'flat'."""
    if PAPER_MODE:
        return 0
    try:
        mps = client.get_positions(ticker=ticker).market_positions or []
        for mp in mps:
            if mp.ticker == ticker:
                # V2 MarketPosition uses `position_fp` (fixed-point signed contract count),
                # not the old V1 `position`. Parse defensively (may be int or numeric string).
                raw = getattr(mp, "position_fp", None)
                if raw is None:
                    raw = getattr(mp, "position", None)  # legacy fallback
                if raw is None:
                    if strict:
                        raise PositionUnknown(f"{ticker}: no position field on MarketPosition")
                    return 0
                try:
                    return int(round(float(raw)))
                except (TypeError, ValueError) as ce:
                    if strict:
                        raise PositionUnknown(f"{ticker}: unparseable position {raw!r}") from ce
                    return 0
        return 0   # ticker not in the list => genuinely flat
    except PositionUnknown:
        raise
    except Exception as e:
        log(f"⚠️ Position check error ({ticker}): {e}")
        if strict:
            raise PositionUnknown(f"{ticker}: {e}") from e
    return 0

def weighted_fill_price(side, order_id=None, ticker=None, limit=200):
    """Volume-weighted average fill price (cents) for the side, from actual fills."""
    try:
        if order_id:
            fills = client.get_fills(order_id=order_id, limit=limit).fills or []
        else:
            fills = client.get_fills(ticker=ticker, limit=limit).fills or []
    except Exception as e:
        log(f"⚠️ Fills lookup error: {e}")
        return None

    def _num(v):
        try: return float(v)
        except (TypeError, ValueError): return None

    tot_cost = 0.0; tot_cnt = 0.0
    for fl in fills:
        # V2 Fill uses `count_fp` (fixed-point count) and `yes_price_dollars` /
        # `no_price_dollars` (dollar strings), NOT the old V1 `count` / `yes_price`.
        c = _num(getattr(fl, "count_fp", None))
        if c is None:
            c = _num(getattr(fl, "count", None))  # legacy fallback
        if c is None or c <= 0:
            continue
        # Price in dollars for the chosen side -> convert to cents.
        if side == "yes":
            pd = getattr(fl, "yes_price_dollars", None)
        else:
            pd = getattr(fl, "no_price_dollars", None)
        p = _num(pd)
        if p is not None:
            p_cents = p * 100.0
        else:
            # legacy fallback: old cents fields
            legacy = getattr(fl, "yes_price", None) if side == "yes" else getattr(fl, "no_price", None)
            p_cents = _num(legacy)
        if p_cents is None:
            continue
        tot_cost += p_cents * c; tot_cnt += c
    return round(tot_cost / tot_cnt) if tot_cnt > 0 else None

def place_order(ticker, side, count, action, price_cents):
    """Returns {filled, remaining, avg_price_cents, order_id, fees_cents}.

    PAPER: simulates an immediate full fill (buy at bid+slippage, sell at the bid).
    LIVE : submits a V2 immediate-or-cancel (IOC) marketable-limit order. IOC fills
           whatever is available at/through our price immediately and the exchange
           auto-cancels any unfilled remainder — so no resting order can linger."""
    if PAPER_MODE:
        if action == "buy":
            fill = min(99, price_cents + MAX_SLIPPAGE)
        else:
            fill = max(1, price_cents)  # assume we hit the live bid passed in
        return {"filled": count, "remaining": 0, "avg_price_cents": fill,
                "order_id": "PAPER", "fees_cents": compute_fee_cents(fill, count)}

    blank = {"filled": 0, "remaining": count, "avg_price_cents": None, "order_id": None, "fees_cents": 0}
    order_id = str(uuid.uuid4())
    # Marketable-limit price in cents (same slippage logic as before).
    actual_price = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)

    # --- V1 -> V2 order mapping -------------------------------------------------
    # Kalshi retired the V1 /portfolio/orders create endpoint (HTTP 410). The V2
    # endpoint (create_order_v2) quotes a SINGLE YES book: side="bid" buys YES,
    # side="ask" sells YES. There is NO "buy NO" — buying NO is selling YES at
    # (1 - price), and selling NO is buying YES at (1 - price). We mirror NO
    # prices across the book accordingly. Prices go on the wire as fixed-point
    # DOLLAR STRINGS ("0.96"), counts as strings; never floats.
    #   buy  YES -> bid @ p            sell YES -> ask @ p
    #   buy  NO  -> ask @ (1 - p)      sell NO  -> bid @ (1 - p)
    if side == "yes":
        book_side = "bid" if action == "buy" else "ask"
        v2_price_cents = actual_price
    elif side == "no":
        book_side = "ask" if action == "buy" else "bid"
        v2_price_cents = 100 - actual_price
    else:
        log(f"❌ Order submit error: unknown side {side!r}")
        return blank
    v2_price_dollars = f"{v2_price_cents / 100:.2f}"   # e.g. 96 -> "0.96"
    v2_count = str(count)

    # Log the exact mapping BEFORE sending so the first live/demo order is auditable.
    log(f"📤 V2 order: {ticker} book_side={book_side} price={v2_price_dollars} "
        f"count={v2_count} (from {side.upper()} {action} @ {actual_price}c) IOC")

    try:
        # IOC: fill what's available at/through our price NOW, auto-cancel the rest
        # server-side. This preserves the old "marketable then cancel remainder"
        # behavior in one call, so no separate (now-removed) cancel_order is needed.
        # reduce_only=True on SELLS is an extra independent safety net: it tells the
        # exchange itself to cap the sell at whatever we actually hold, so even if our
        # locally-tracked count were ever stale/wrong, we can never accidentally sell
        # MORE than we own and flip into an unintended opposite position.
        order_kwargs = dict(
            ticker=ticker,
            client_order_id=order_id,
            side=book_side,
            count=v2_count,
            price=v2_price_dollars,
            time_in_force="immediate_or_cancel",
            self_trade_prevention_type="taker_at_cross",
        )
        if action == "sell":
            order_kwargs["reduce_only"] = True
        resp = client.create_order_v2(**order_kwargs)
    except Exception as e:
        log(f"❌ Order submit error (V2): {e}")
        return blank

    # V2 response is flat (not wrapped in .order) with fixed-point string fields:
    # order_id, fill_count, remaining_count, average_fill_price, average_fee_paid.
    # Read defensively so a shape surprise degrades gracefully instead of crashing.
    def _num(v):
        try: return float(v)
        except (TypeError, ValueError): return 0.0

    oid = getattr(resp, "order_id", None) or order_id
    filled = int(_num(getattr(resp, "fill_count", 0)))
    remaining = int(_num(getattr(resp, "remaining_count", count)))

    # Kalshi's V2 spec: average_fee_paid is the volume-weighted average fee PAID
    # PER CONTRACT (same convention as average_fill_price), not the order total.
    # Must multiply by filled count to get the total fee. (Confirmed against
    # docs.kalshi.com/api-reference/orders/create-order-v2 — CreateOrderV2Response
    # schema: "Volume-weighted average fee paid per contract for fills resulting
    # from this request.") Getting this wrong silently understates fees/overstates
    # PnL by ~filled-count-fold on any multi-contract order.
    per_contract_fee_cents = _num(getattr(resp, "average_fee_paid", 0)) * 100.0
    fees = int(round(per_contract_fee_cents * filled))

    avg = None
    if filled > 0:
        # Prefer the authoritative fills lookup (already cents); fall back to the
        # response's average_fill_price (dollars -> cents).
        avg = weighted_fill_price(side, order_id=oid)
        if avg is None:
            afp = getattr(resp, "average_fill_price", None)
            if afp is not None:
                # average_fill_price is quoted on the YES book. For a NO position
                # the YES-book fill price mirrors back: NO cost = 1 - YES price.
                yes_cents = round(_num(afp) * 100)
                avg = yes_cents if side == "yes" else (100 - yes_cents)

    return {"filled": filled, "remaining": remaining, "avg_price_cents": avg,
            "order_id": oid, "fees_cents": fees}

def flatten(curr, reason, trade_type):
    """Sell the full tracked position at the live bid and realize PnL.
    Returns (cleared: bool, pnl: float). Only clears when confirmed flat."""
    try:
        m_live = client.get_market(curr['ticker']).market
    except Exception as e:
        log(f"⚠️ {reason}: market fetch failed: {e}")
        return False, 0.0
    live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
    res = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
    pnl = 0.0
    if res["filled"] > 0:
        exit_p = res["avg_price_cents"] or live_bid
        gross = (exit_p - curr['entry_price_cents']) * res["filled"] / 100.0
        fees = (curr.get("entry_fees_cents", 0) + res["fees_cents"]) / 100.0
        pnl = gross - fees
        update_trades_json({
            "timestamp": datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S"),
            "ticker": curr['ticker'], "side": curr['side'],
            "count": res["filled"], "entry_price_cents": curr.get("entry_price_cents"),
            "exit_price_cents": exit_p,
            "pnl": round(pnl, 2), "type": trade_type,
            "rsi_at_entry": curr.get("rsi_at_entry")})
        log(f"{reason}: sold {res['filled']} @ ~{exit_p}c | PnL ${pnl:+.2f}")
    else:
        log(f"⚠️ {reason}: sell unfilled (remainder canceled) — will retry next loop")

    if PAPER_MODE:
        return True, pnl  # paper sells fill fully

    try:
        remaining_pos = abs(get_exchange_position(curr['ticker'], strict=True))
    except PositionUnknown as pe:
        # Do NOT assume flat on a read failure — that's the exact bug class that
        # caused the over-buy. Leave curr/count untouched and report "not cleared"
        # so the caller keeps tracking/monitoring it and retries next loop.
        log(f"⚠️ {reason}: can't confirm remaining position after sell attempt "
            f"({pe}) — NOT marking cleared, will re-check next loop")
        return False, pnl
    if remaining_pos == 0:
        return True, pnl
    curr['count'] = remaining_pos  # partial: keep tracking remainder
    return False, pnl

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    # --- CLI: clear a drawdown halt so the bot can run again ---
    # A drawdown stop is deliberately sticky (see check_drawdown_halt): once tripped,
    # state.json carries a `halted` flag and the bot refuses to start until a human
    # clears it. `python bot.py --reset-halt` is that deliberate re-arm switch.
    if "--reset-halt" in sys.argv:
        _st = load_state()
        _was = _st.pop("halted", None)
        # Reset the high-water mark to the current balance so the freshly re-armed
        # bot measures drawdown from where it stands now, not the old pre-loss peak.
        if not PAPER_MODE:
            try:
                _bal = client.get_balance().balance / 100.0
                _st["peak_balance"] = round(_bal, 2)
            except Exception as _e:
                print(f"[reset-halt] Could not read live balance to reset peak: {_e}")
        save_state(_st)
        if _was:
            print(f"✅ Drawdown halt cleared. Peak reset to ${_st.get('peak_balance', 'n/a')}. "
                  f"Bot can run again.")
        else:
            print("ℹ️ No active halt flag was set. Nothing to clear.")
        sys.exit(0)

    # --- Startup halt guard: refuse to run if a drawdown stop is active (live only) ---
    # This makes the halt sticky across restarts: an accidental (or cron) restart won't
    # silently resume trading into a drawdown. Clear it with `python bot.py --reset-halt`.
    if not PAPER_MODE and USE_DRAWDOWN_LIMIT:
        _boot_state = load_state()
        if _boot_state.get("halted"):
            _hb = _boot_state.get("halted_balance")
            _hp = _boot_state.get("peak_balance")
            log(f"🛑 Bot is HALTED by drawdown stop (balance ${_hb} vs peak ${_hp}). "
                f"Run `python bot.py --reset-halt` to re-arm. Exiting.")
            sys.exit(0)

    mode = "PAPER/SHADOW" if PAPER_MODE else ("LIVE-DEMO (sandbox funds)" if DEMO_MODE else "LIVE")
    stop_txt = f"stop {STOP_ARM_PRICE}->{STOP_TRIGGER_PRICE}c" if USE_STOP else "stop OFF (hold-to-settle)"
    rsi_txt  = f"RSI≥{RSI_MIN}" if USE_RSI_FILTER else "RSI filter OFF"
    fomc_txt = "skip FOMC days" if SKIP_FOMC_DAYS else "FOMC skip OFF"
    dd_txt   = f"drawdown halt {int(MAX_DRAWDOWN_PCT*100)}%" if (USE_DRAWDOWN_LIMIT and not PAPER_MODE) else "drawdown OFF"
    log(f"🪄 Magick Bot v6.4.0 Active [{mode}] | {stop_txt} | schedule A (drop 17-22 ET) | {rsi_txt} | {fomc_txt} | {dd_txt}")

    while True:
        try:
            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b': os._exit(0)
                elif key.lower() == b'c': OVERRIDE_TRIGGERED = True

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            if PAPER_MODE and "paper_balance" not in state:
                state["paper_balance"] = PAPER_START_BALANCE
                save_state(state)
            # Write an explicit mode signal for the dashboard, and clear any stale
            # paper_balance left over from a previous paper run when we're now live/demo.
            # (The dashboard used to infer paper-vs-live from the mere presence of
            # paper_balance, which lingered after switching to live and mislabeled the mode.)
            _mode_str = "PAPER" if PAPER_MODE else ("DEMO" if DEMO_MODE else "LIVE")
            _state_dirty = False
            if state.get("mode") != _mode_str:
                state["mode"] = _mode_str
                _state_dirty = True
            if not PAPER_MODE and "paper_balance" in state:
                del state["paper_balance"]   # stale paper data; live balance comes from the exchange
                _state_dirty = True
            if _state_dirty:
                save_state(state)

            cash = state.get("paper_balance", PAPER_START_BALANCE) if PAPER_MODE else client.get_balance().balance / 100.0
            curr = state.get("current_trade")
            is_trading_window = in_trading_window(now_et)  # original schedule restored; sizing stays flat 1%

            # --- RECONCILE tracked state vs exchange (live only) ---
            if curr and not PAPER_MODE:
                held = abs(get_exchange_position(curr['ticker']))
                if held and held != curr.get('count'):
                    log(f"🔧 Reconcile count: state {curr.get('count')} -> exchange {held}")
                    curr['count'] = held
                    state['current_trade'] = curr
                    save_state(state)

            if OVERRIDE_TRIGGERED:
                OVERRIDE_TRIGGERED = False
                if curr:
                    log("🛠️ Manual Override: flattening position")
                    cleared, pnl = flatten(curr, "🛠️ Override", "MANUAL_FLATTEN")
                    apply_pnl(state, pnl)
                    state["current_trade"] = None if cleared else curr
                    if not cleared: log("⚠️ Override flatten incomplete — position still open")
                else:
                    state["current_trade"] = None
                save_state(state)
                curr = state.get("current_trade")

            floor = PAPER_SAFETY_FLOOR if PAPER_MODE else SAFETY_FLOOR
            strike_halt = STRIKE_LIMIT is not None and state.get("strikes", 0) >= STRIKE_LIMIT
            # Drawdown circuit breaker (live only): also updates the high-water mark.
            dd_halt, dd_peak, dd_level = check_drawdown_halt(state, cash)
            if not PAPER_MODE and USE_DRAWDOWN_LIMIT:
                save_state(state)  # persist any new peak recorded by check_drawdown_halt
            if cash <= floor or strike_halt or dd_halt:
                if cash <= floor:
                    reason = "cash floor"
                elif strike_halt:
                    reason = f"{STRIKE_LIMIT}-loss streak"
                else:
                    reason = f"{int(MAX_DRAWDOWN_PCT*100)}% drawdown (peak ${dd_peak:.2f} -> ${cash:.2f}, halt at ${dd_level:.2f})"
                # A drawdown stop is sticky: write a persistent flag so the bot won't
                # auto-resume on restart. Cash-floor and strike halts also break, but
                # only the drawdown stop requires an explicit --reset-halt to clear.
                if dd_halt:
                    state["halted"] = True
                    state["halted_reason"] = "drawdown"
                    state["halted_balance"] = round(cash, 2)
                    save_state(state)
                log(f"🚨 Shutdown ({reason}): Cash ${cash:.2f} | Strikes {state.get('strikes')}")
                if dd_halt:
                    log("🛑 Drawdown halt is STICKY — run `python bot.py --reset-halt` to re-arm.")
                break

            # --- TICKER FETCH ---
            resp = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', [])
                       if (ensure_aware(m.close_time) - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: ensure_aware(x.close_time))
                market = markets[0]
                time_left = (ensure_aware(market.close_time) - now_et).total_seconds() / 60.0
                y_p, n_p = safe_price_cents(market.yes_bid_dollars), safe_price_cents(market.no_bid_dollars)
                # ASK prices — the edge strategy prices off what you actually PAY to buy,
                # not the bid. no_ask = 100 - yes_bid (complementary sides).
                y_ask = safe_price_cents(getattr(market, "yes_ask_dollars", None))
                n_ask = safe_price_cents(getattr(market, "no_ask_dollars", None))
            else:
                time_left = 0

            # --- MONITORING / TWO-STAGE STOP ---
            if curr and curr.get("status") == "filled":
                m_live = client.get_market(curr['ticker']).market
                curr_close = ensure_aware(m_live.close_time)
                curr_time_left = (curr_close - now_et).total_seconds() / 60.0 if curr_close else 0
                live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)

                # Stop active until final 30s of THIS market. Gated on USE_STOP —
                # when False, positions HOLD to settlement instead.
                if USE_STOP and curr_time_left > 0.5 and live_bid > 0:
                    if not curr.get("stop_armed") and live_bid <= STOP_ARM_PRICE:
                        curr["stop_armed"] = True
                        state["current_trade"] = curr; save_state(state)
                        log(f"👀 Stop ARMED on {curr['ticker']} (bid {live_bid}c <= {STOP_ARM_PRICE}c)")
                    if curr.get("stop_armed") and live_bid <= STOP_TRIGGER_PRICE:
                        log(f"🚨 STOP triggered on {curr['ticker']} (bid {live_bid}c <= {STOP_TRIGGER_PRICE}c)")
                        cleared, pnl = flatten(curr, "🚨 Stop", "STOP_LOSS")
                        apply_pnl(state, pnl)
                        if cleared:
                            state["current_trade"] = None
                            state["strikes"] = state.get("strikes", 0) + 1
                        else:
                            state["current_trade"] = curr
                        save_state(state); play_sound("stop"); continue

            # --- HEARTBEAT ---
            # Show entry eligibility (LIVE vs IDLE) so a glance at the terminal
            # answers "is it trading, waiting, or stuck?" — IDLE also reports why
            # and when it next opens. Note a held position is still monitored while
            # IDLE (the schedule gates new ENTRIES only), which bot_status() says.
            hb_label, hb_detail = bot_status(now_et, has_position=bool(curr))
            status_text = f" [IN: {curr['side'].upper()} @ {curr['entry_price_cents']}c{' ARMED' if curr.get('stop_armed') else ''}]" if curr else ""
            hb_line = (f"[{now_et.strftime('%H:%M:%S')}] {mode} | {hb_label}: {hb_detail} | "
                       f"Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}")
            # ljust pads shorter lines so a \r redraw fully erases a longer previous
            # line (status text length varies a lot between LIVE and IDLE states).
            print(f"\r{hb_line.ljust(140)}", end="")

            if not markets:
                time.sleep(5); continue
            if not is_trading_window and not curr:
                time.sleep(10); continue  # outside schedule and flat — idle (held positions still monitored above)

            # --- SETTLEMENT CHECK ---
            if curr and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                interruptible_sleep(35)  # stays responsive to ESC/override during the wait
                # getattr's default only applies if the attribute is MISSING — before
                # settlement `result` exists but is None, so the old bare `or` was
                # needed here: getattr(...,'result',None) or '' avoids .lower() on None.
                res = (getattr(client.get_market(curr['ticker']).market, 'result', None) or '').lower()
                if res in ['yes', 'no']:
                    won = (curr['side'] == res)
                    entry_fees = curr.get("entry_fees_cents", 0) / 100.0
                    if won:
                        pnl = (100 - curr['entry_price_cents']) * curr['count'] / 100.0 - entry_fees
                    else:
                        pnl = -(curr['entry_price_cents'] * curr['count'] / 100.0) - entry_fees
                    update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": curr['ticker'], "side": curr['side'],
                        "count": curr.get("count"), "entry_price_cents": curr.get("entry_price_cents"),
                        "exit_price_cents": 100 if won else 0,
                        "pnl": round(pnl, 2), "type": "SETTLEMENT",
                        "rsi_at_entry": curr.get("rsi_at_entry")})
                    apply_pnl(state, pnl)
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                    state["current_trade"] = None
                    state["last_settled_ticker"] = curr["ticker"]  # guard vs re-entry while it lingers in open list
                    save_state(state)
                    play_sound("settle_win" if won else "settle_loss")

            # --- ENTRY (buy whichever side's ask is exactly 96c) ---
            elif not curr and is_trading_window:
                # Don't re-enter a market we just settled (it can linger in the open list briefly).
                if market.ticker == state.get("last_settled_ticker"):
                    time.sleep(3); continue
                if ENTRY_TIME_MIN <= time_left <= ENTRY_TIME_MAX:
                    existing = get_exchange_position(market.ticker)  # 0 in paper
                    if existing != 0:
                        ex_side = "yes" if existing > 0 else "no"
                        avg = weighted_fill_price(ex_side, ticker=market.ticker) or (y_ask if ex_side == "yes" else n_ask)
                        log(f"🔧 Adopting untracked position: {market.ticker} {ex_side.upper()} x{abs(existing)} @ ~{avg}c")
                        state["current_trade"] = {"ticker": market.ticker, "side": ex_side,
                            "count": abs(existing), "entry_price_cents": avg,
                            "status": "filled", "entry_fees_cents": 0, "stop_armed": False,
                            # unknown — this is an adopted/orphaned position, not a fresh
                            # RSI-gated entry, so there's no RSI reading to attach to it.
                            "rsi_at_entry": None}
                        mark_ticker_entered(state, market.ticker)
                        save_state(state)
                    else:
                        # Unconditional "already traded this ticker this session" guard.
                        # Independent of get_exchange_position/current_trade — even if a
                        # stop-loss exit already closed this position earlier with time
                        # still left in the window, we never buy it again. See
                        # mark_ticker_entered() for why this is needed on top of the
                        # position cap / current_trade tracking.
                        if market.ticker in state.get("entered_tickers", []):
                            log(f"🔒 Already traded {market.ticker} this session — no re-entry")
                            time.sleep(3); continue
                        side = None
                        ask_price = None
                        if y_ask == 96:
                            side = "yes"
                            ask_price = y_ask
                        elif n_ask == 96:
                            side = "no"
                            ask_price = n_ask
                        if side is None:
                            time.sleep(3); continue

                        # FOMC skip — bail immediately, no API call needed.
                        if is_fomc_day(now_et):
                            log(f"⛔ FOMC day: skipping {market.ticker} {side.upper()} entry")
                            time.sleep(10); continue

                        # RSI filter — skip entries below RSI_MIN (default 55).
                        # Backtesting showed sub-55 RSI entries won only ~12% of the
                        # time (overwhelmingly NO-side in a bear trend), far below the
                        # 96.27% breakeven. compute_rsi() returns None on fetch failure;
                        # treat None as a skip to avoid trading blind.
                        # `rsi` is initialized here (not just inside the filter branch) so
                        # it's always defined when we build current_trade below — recorded
                        # on every trade as rsi_at_entry regardless of USE_RSI_FILTER, so
                        # trades.json carries the value that was live at entry. None means
                        # either the filter was off or (for adopted positions below) unknown.
                        rsi = None
                        if USE_RSI_FILTER:
                            rsi = compute_rsi(market.ticker)
                            if rsi is None:
                                time.sleep(5); continue
                            if rsi < RSI_MIN:
                                if _last_skip_logged_ticker != market.ticker:
                                    log(f"⛔ RSI filter: {market.ticker} {side.upper()} RSI={rsi} < {RSI_MIN} — skip")
                                    _last_skip_logged_ticker = market.ticker
                                time.sleep(3); continue
                            log(f"✅ RSI filter passed: RSI={rsi} >= {RSI_MIN}")

                        # --- v6.4.0: fresh price re-check right before submit ---
                        # ask_price was read at the TOP of this loop iteration, before
                        # compute_rsi()'s network calls (settled-market lookup +
                        # candlestick batch fetch). That's real latency — often several
                        # hundred ms, sometimes more — during which the live ask can
                        # move off 96c, especially this deep into the pre-close window
                        # where price is actively converging. Submitting a stale price
                        # with MAX_SLIPPAGE=0 (the old default) meant the IOC order was
                        # only ever marketable if the market had NOT moved at all in
                        # that gap. Re-checking here closes that gap directly; the
                        # MAX_SLIPPAGE bump above is just the backstop for whatever
                        # latency remains between this check and the order landing.
                        stale_price = ask_price
                        if not PAPER_MODE:
                            try:
                                fresh_market = client.get_market(market.ticker).market
                                fresh_field = "yes_ask_dollars" if side == "yes" else "no_ask_dollars"
                                fresh_ask = safe_price_cents(getattr(fresh_market, fresh_field, None))
                            except Exception as e:
                                log(f"⚠️ Fresh price re-check failed ({e}) — using last-known {stale_price}c")
                                fresh_ask = None
                            if fresh_ask and fresh_ask > stale_price + MAX_SLIPPAGE:
                                log(f"⏭️ Price moved {stale_price}c -> {fresh_ask}c before submit "
                                    f"(> {MAX_SLIPPAGE}c tolerance) — skipping, not submitting a doomed order")
                                log_unfilled_attempt({
                                    "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                                    "reason": "price_moved_before_submit",
                                    "ticker": market.ticker, "side": side,
                                    "stale_price_cents": stale_price, "fresh_price_cents": fresh_ask,
                                    "qty_attempted": None, "rsi_at_entry": rsi,
                                    "time_left_min": round(time_left, 1),
                                })
                                time.sleep(2); continue
                            if fresh_ask:
                                ask_price = fresh_ask   # use the freshest read (may even be better than stale)

                        buy_price = min(99, ask_price + MAX_SLIPPAGE)
                        qty = int(min(MAX_POSITION_DOLLARS, (cash * FLAT_RISK)) * 100 // buy_price)

                        # --- Hard per-market contract cap (independent safety layer) ---
                        # Never let combined exposure in this market exceed the cap. Uses a
                        # STRICT position read: if the exchange position can't be read, we do
                        # NOT assume flat — we skip, because assuming flat on an error is exactly
                        # what caused the over-buy. Paper mode has no real position (returns 0).
                        if MAX_CONTRACTS_PER_MARKET and qty >= 1:
                            try:
                                held_now = abs(get_exchange_position(market.ticker, strict=True))
                            except PositionUnknown as pe:
                                log(f"🛑 Skipping entry — can't confirm current position "
                                    f"({market.ticker}); not risking an over-buy. [{pe}]")
                                time.sleep(5); continue
                            room = MAX_CONTRACTS_PER_MARKET - held_now
                            if room <= 0:
                                log(f"🧢 Position cap reached: hold {held_now}/"
                                    f"{MAX_CONTRACTS_PER_MARKET} in {market.ticker} — no more entries")
                                time.sleep(5); continue
                            if qty > room:
                                log(f"🧢 Capping order {qty}->{room} "
                                    f"(hold {held_now}, cap {MAX_CONTRACTS_PER_MARKET})")
                                qty = room

                        if qty >= 1:
                            log(f"⚡ Entry at exactly 96c on {side.upper()}: ask {ask_price}c x{qty} (time left {time_left:.1f}min)")
                            res = place_order(market.ticker, side, qty, "buy", ask_price)
                            if res["filled"] > 0:
                                entry_p = res["avg_price_cents"] or buy_price
                                state["current_trade"] = {"ticker": market.ticker, "side": side,
                                    "count": res["filled"], "entry_price_cents": entry_p,
                                    "status": "filled", "entry_fees_cents": res["fees_cents"],
                                    "stop_armed": False, "rsi_at_entry": rsi}
                                mark_ticker_entered(state, market.ticker)
                                save_state(state); play_sound("buy")
                                log(f"✅ Filled {res['filled']}/{qty} @ {entry_p}c (fees {res['fees_cents']}c)")
                                time.sleep(5)
                            else:
                                log("⚠️ Entry unfilled & remainder canceled. 5s Cooldown...")
                                log_unfilled_attempt({
                                    "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                                    "reason": "ioc_unfilled",
                                    "ticker": market.ticker, "side": side,
                                    "stale_price_cents": stale_price, "fresh_price_cents": ask_price,
                                    "qty_attempted": qty, "rsi_at_entry": rsi,
                                    "time_left_min": round(time_left, 1),
                                })
                                time.sleep(5)


            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)
