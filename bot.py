import os
import json
import time
import math
import uuid
from datetime import datetime
import pytz
from kalshi_python_sync import Configuration, KalshiClient

# Windows-only tools
try:
    import winsound
    import msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False

# ====================== CONFIG ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")

# --- Trading mode ---
PAPER_MODE = True              # Shadow/paper trading. No real orders are placed.
PAPER_START_BALANCE = 1000.0   # Simulated starting cash; moves with realized PnL.
PAPER_SAFETY_FLOOR = 0.0       # Paper floor (live SAFETY_FLOOR would block trading from $1000).

# --- Sizing (flat 1%) ---
FLAT_RISK = 0.25               # Stake 1% of available cash per trade (flat, within the schedule windows).
MAX_POSITION_DOLLARS = 500.0
FEE_RATE = 0.07                # Kalshi trading-fee rate for paper/sim PnL. VERIFY against the
                               # current KXBTC15M schedule (get_series_fee_changes); fees change.

# --- Entry ---
MAX_SLIPPAGE = 0               # Pay up to ask + 0c (fills at the ask, no slippage).
ENTRY_TIME_MIN = 1.0           # Minutes-before-close window start.
ENTRY_TIME_MAX = 10.0          # Minutes-before-close window end.

# --- RSI filter ---
# Backtesting on real Kalshi data (Apr-Jun 2026, 1,435 entries) showed that 96c
# entries with RSI-14 < 55 won only 12% of the time — far below the 96.27% breakeven.
# Entries with RSI-14 >= 55 won 98.63%, producing +84.2% ROI with a $573 max DD
# vs +49.6% / $2,317 without the filter. The filter cuts ~40% of trade volume but
# eliminates losing months entirely on the 3-month test window.
# RSI is computed from the last 20 KXBTC15M 1-min yes_ask candles (BTC-implied price)
# using the same series the bot already reads — no external data source needed.
USE_RSI_FILTER = True          # Set False to disable and trade all 96c prints.
RSI_MIN = 55                   # Skip entries where RSI-14 is below this threshold.
RSI_CANDLES = 20               # 1-min candles to fetch for RSI calculation (need >= 15).

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
# DISABLED BY DEFAULT. The 80/75c fixed-cent threshold is meaningless for the
# 96c-favorite strategy: positions are already deep ITM at entry so the stop
# fires instantly on any normal intraday tick. Backtesting confirmed gap-scaled
# slippage makes fixed-cent stops net-negative (see README_tests_addendum.md).
# Only re-enable with thresholds derived for this specific entry regime.
USE_STOP = False
STOP_ARM_PRICE = 80            # Only used if USE_STOP=True. Begin monitoring once bid <= this.
STOP_TRIGGER_PRICE = 75        # Only used if USE_STOP=True. Exit once armed & bid <= this.

# --- Risk rails ---
SAFETY_FLOOR = 1000.0          # Live-mode cash floor.
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
FILL_POLL_TRIES = 4            # Seconds to wait for a marketable limit order before canceling remainder.

OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00

# ====================== TRADING SCHEDULE ======================
def in_trading_window(now=None):
    """Schedule A: trade all hours EXCEPT 17:00–21:59 ET on weekdays, and
    the Sunday afternoon window. Backtesting on real Kalshi data (Apr–Jun 2026)
    showed 17:00–22:00 ET lost −$9/trade on average vs +$3–6/trade elsewhere,
    driven by erratic post-market crypto vol. All other sessions are kept.

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

def is_fomc_day(now=None):
    """Return True if today is a configured FOMC decision date and SKIP_FOMC_DAYS
    is enabled. FOMC announcements at 2:00 PM ET inject directional uncertainty
    that RSI cannot anticipate — backtesting showed 83–91% win rates on these days
    vs 99%+ on all other days."""
    if not SKIP_FOMC_DAYS:
        return False
    now = now or datetime.now(pytz.timezone("US/Eastern"))
    return now.strftime("%Y-%m-%d") in FOMC_DECISION_DATES

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

def compute_rsi(ticker):
    """Fetch the last RSI_CANDLES 1-min yes_ask candles for `ticker`'s series and
    return RSI-14 (Wilder's smoothing via simple average of first period).
    Returns None if insufficient data or the API call fails — caller treats None
    as 'filter not applicable' and skips the entry to be safe."""
    if not USE_RSI_FILTER:
        return None  # filter disabled; caller ignores the return value
    try:
        now_ts  = int(time.time())
        resp    = client.get_series_market_candlesticks(
            series_ticker = "KXBTC15M",
            ticker        = ticker,
            start_ts      = now_ts - RSI_CANDLES * 60 - 60,
            end_ts        = now_ts,
            period_interval = 1,
        )
        candles = getattr(resp, "candlesticks", None) or []
        # Use yes_ask close as the BTC-implied price series.
        closes  = []
        for c in candles:
            ya = getattr(c, "yes_ask", None)
            v  = getattr(ya, "close", None) if ya else None
            if v is not None:
                try: closes.append(float(v) * 100)  # dollars -> cents
                except (TypeError, ValueError): pass
        if len(closes) < 15:
            log(f"⚠️ RSI: only {len(closes)} candles — skipping filter (need 15+)")
            return None
        # Wilder RSI-14 via simple-average seed.
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

def update_trades_json(trade_entry):
    trades = []
    trade_entry["category"] = "paper" if PAPER_MODE else "bot"
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r") as f:
            try: trades = json.load(f)
            except: trades = []
    trades.append(trade_entry)
    with open(TRADES_FILE, "w") as f: json.dump(trades, f, indent=2)

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

# ====================== API SETUP ======================
# NOTE: Even paper mode needs API keys to read live market data (prices, results).
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

# ====================== EXCHANGE TRUTH HELPERS ======================
def get_exchange_position(ticker):
    """Signed net contracts held in `ticker` per the exchange (live only).
    Positive = long YES, negative = long NO, 0 = flat. Paper mode has no real position."""
    if PAPER_MODE:
        return 0
    try:
        mps = client.get_positions(ticker=ticker).market_positions or []
        for mp in mps:
            if mp.ticker == ticker:
                return mp.position or 0
    except Exception as e:
        log(f"⚠️ Position check error ({ticker}): {e}")
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
    tot_cost = 0; tot_cnt = 0
    for fl in fills:
        c = fl.count or 0
        if c <= 0: continue
        p = fl.yes_price if side == "yes" else fl.no_price
        if p is None: p = fl.price
        if p is None: continue
        tot_cost += p * c; tot_cnt += c
    return round(tot_cost / tot_cnt) if tot_cnt > 0 else None

def place_order(ticker, side, count, action, price_cents):
    """Returns {filled, remaining, avg_price_cents, order_id, fees_cents}.

    PAPER: simulates an immediate full fill (buy at bid+slippage, sell at the bid).
    LIVE : submits a marketable limit order, waits briefly, then CANCELS any
           unfilled remainder so no forgotten resting order can fill later."""
    if PAPER_MODE:
        if action == "buy":
            fill = min(99, price_cents + MAX_SLIPPAGE)
        else:
            fill = max(1, price_cents)  # assume we hit the live bid passed in
        return {"filled": count, "remaining": 0, "avg_price_cents": fill,
                "order_id": "PAPER", "fees_cents": compute_fee_cents(fill, count)}

    blank = {"filled": 0, "remaining": count, "avg_price_cents": None, "order_id": None, "fees_cents": 0}
    order_id = str(uuid.uuid4())
    actual_price = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)
    try:
        resp = client.create_order(
            ticker=ticker, side=side, action=action, count=count, type="limit",
            client_order_id=order_id,
            yes_price=actual_price if side == "yes" else None,
            no_price=actual_price if side == "no" else None,
        )
        order = resp.order
        oid = order.order_id
    except Exception as e:
        log(f"❌ Order submit error: {e}")
        return blank

    for _ in range(FILL_POLL_TRIES):
        if (order.remaining_count or 0) == 0:
            break
        time.sleep(1)
        try:
            order = client.get_order(oid).order
        except Exception as e:
            log(f"⚠️ get_order error: {e}")
            break

    if (order.remaining_count or 0) > 0 and order.status != "canceled":
        try:
            order = client.cancel_order(oid).order
        except Exception as e:
            log(f"⚠️ Cancel FAILED — order {oid} may still be resting! {e}")

    filled = order.fill_count or 0
    remaining = order.remaining_count or 0
    fees = order.taker_fees or 0
    avg = None
    if filled > 0:
        avg = weighted_fill_price(side, order_id=oid)
        if avg is None and order.taker_fill_cost:
            avg = round(order.taker_fill_cost / filled)
    return {"filled": filled, "remaining": remaining, "avg_price_cents": avg, "order_id": oid, "fees_cents": fees}

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
            "pnl": round(pnl, 2), "type": trade_type})
        log(f"{reason}: sold {res['filled']} @ ~{exit_p}c | PnL ${pnl:+.2f}")
    else:
        log(f"⚠️ {reason}: sell unfilled (remainder canceled) — will retry next loop")

    if PAPER_MODE:
        return True, pnl  # paper sells fill fully
    remaining_pos = abs(get_exchange_position(curr['ticker']))
    if remaining_pos == 0:
        return True, pnl
    curr['count'] = remaining_pos  # partial: keep tracking remainder
    return False, pnl

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    mode = "PAPER/SHADOW" if PAPER_MODE else "LIVE"
    stop_txt = f"stop {STOP_ARM_PRICE}->{STOP_TRIGGER_PRICE}c" if USE_STOP else "stop OFF (hold-to-settle)"
    rsi_txt  = f"RSI≥{RSI_MIN}" if USE_RSI_FILTER else "RSI filter OFF"
    fomc_txt = "skip FOMC days" if SKIP_FOMC_DAYS else "FOMC skip OFF"
    log(f"🪄 Magick Bot v6.0.0 Active [{mode}] | {stop_txt} | schedule A (drop 17-22 ET) | {rsi_txt} | {fomc_txt}")

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
            if cash <= floor or strike_halt:
                reason = "cash floor" if cash <= floor else f"{STRIKE_LIMIT}-loss streak"
                log(f"🚨 Shutdown ({reason}): Cash ${cash:.2f} | Strikes {state.get('strikes')}"); break

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
                # when False (default, matches backtest) positions HOLD to settlement.
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
            status_text = f" [IN: {curr['side'].upper()} @ {curr['entry_price_cents']}c{' ARMED' if curr.get('stop_armed') else ''}]" if curr else ""
            print(f"\r[{now_et.strftime('%H:%M:%S')}] {mode} | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}", end="")

            if not markets:
                time.sleep(5); continue
            if not is_trading_window and not curr:
                time.sleep(10); continue  # outside schedule and flat — idle (held positions still monitored above)

            # --- SETTLEMENT CHECK ---
            if curr and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                time.sleep(35)
                res = getattr(client.get_market(curr['ticker']).market, 'result', '').lower()
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
                        "pnl": round(pnl, 2), "type": "SETTLEMENT"})
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
                            "status": "filled", "entry_fees_cents": 0, "stop_armed": False}
                        save_state(state)
                    else:
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
                        if USE_RSI_FILTER:
                            rsi = compute_rsi(market.ticker)
                            if rsi is None:
                                time.sleep(5); continue
                            if rsi < RSI_MIN:
                                log(f"⛔ RSI filter: {market.ticker} {side.upper()} RSI={rsi} < {RSI_MIN} — skip")
                                time.sleep(3); continue
                            log(f"✅ RSI filter passed: RSI={rsi} >= {RSI_MIN}")

                        buy_price = min(99, ask_price + MAX_SLIPPAGE)
                        qty = int(min(MAX_POSITION_DOLLARS, (cash * FLAT_RISK)) * 100 // buy_price)
                        if qty >= 1:
                            log(f"⚡ Entry at exactly 96c on {side.upper()}: ask {ask_price}c x{qty} (time left {time_left:.1f}min)")
                            res = place_order(market.ticker, side, qty, "buy", ask_price)
                            if res["filled"] > 0:
                                entry_p = res["avg_price_cents"] or buy_price
                                state["current_trade"] = {"ticker": market.ticker, "side": side,
                                    "count": res["filled"], "entry_price_cents": entry_p,
                                    "status": "filled", "entry_fees_cents": res["fees_cents"],
                                    "stop_armed": False}
                                save_state(state); play_sound("buy")
                                log(f"✅ Filled {res['filled']}/{qty} @ {entry_p}c (fees {res['fees_cents']}c)")
                                time.sleep(5)
                            else:
                                log("⚠️ Entry unfilled & remainder canceled. 15s Cooldown...")
                                time.sleep(15)

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)
