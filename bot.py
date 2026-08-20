import os
import json
import math
import time
import uuid
import argparse
from datetime import datetime, timedelta
import pytz
import requests
from kalshi_python_sync import Configuration, KalshiClient

# Windows-only tools
try:
    import winsound
    import msvcrt
    HAS_WINDOWS = True
except ImportError:
    HAS_WINDOWS = False

# ====================== SHADOW MODE ======================
# Shadow mode runs the exact same live market data, filters, and decision
# logic as real trading, but never calls Kalshi's order-placement API --
# fills are simulated at the observed live price (the same optimistic-fill
# assumption the backtest uses, now against live prices instead of
# historical bars) with the real published taker fee applied. Tracks its
# own cash ledger (shadow_state.json etc.) so it can run in parallel with
# a real instance, pointed at the same folder, without ever colliding.
# Does NOT solve for real slippage/fill quality -- see README.md.
_argp = argparse.ArgumentParser()
_argp.add_argument("--shadow", action="store_true", help="Run in shadow mode: simulated fills, no real orders placed")
_argp.add_argument("--shadow-cash", type=float, default=5000.0, help="Starting simulated cash for shadow mode (default: 5000)")
_argp.add_argument("--shadow-pessimistic", action="store_true",
                   help="Shadow mode: re-fetch the quote at order time and only fill if our limit price "
                        "still crosses the book. Fills at the refreshed price, not the price the signal saw.")
_argp.add_argument("--alert-only", action="store_true",
                   help="Signal-only mode: run every filter exactly as normal, alert loudly when a "
                        "setup qualifies, but NEVER place an order. You execute manually.")
_argp.add_argument("--no-sound", action="store_true",
                   help="Alert-only mode: suppress the audible beep (visual alert only)")
_args = _argp.parse_args()
ALERT_ONLY = _args.alert_only
ALERT_SOUND = not _args.no_sound
SHADOW_MODE = _args.shadow
SHADOW_PESSIMISTIC = _args.shadow_pessimistic
SHADOW_TAKER_FEE_MULTIPLIER = 0.07  # Kalshi's real published taker formula, verified against their fee schedule
SHADOW_FEE_CEIL_TO_CENT = True      # Kalshi rounds the fee UP to the next whole cent per order. At 1-contract
                                    # size the raw formula gives ~$0.003, so ignoring the rounding understates
                                    # the real fee by ~3x. Set False to reproduce older backtest numbers.

def taker_fee_dollars(price_cents, count):
    """Kalshi's published taker fee: 0.07 * p * (1-p) per contract, rounded UP to
    the next whole cent per order. Used by shadow fills AND by live PnL."""
    p = price_cents / 100.0
    raw = count * SHADOW_TAKER_FEE_MULTIPLIER * p * (1 - p)
    if SHADOW_FEE_CEIL_TO_CENT:
        return math.ceil(raw * 100 - 1e-9) / 100.0
    return raw

# Back-compat alias -- older call sites / any external tooling.
shadow_taker_fee_dollars = taker_fee_dollars

# ====================== CONFIG ======================
BOT_NAME = "Mystic-Bot 1.0 24/7" + (" [SHADOW]" if SHADOW_MODE else "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_prefix = "shadow_" if SHADOW_MODE else ""
APIKEY_FILE = os.path.join(BASE_DIR, "apikey.txt")     # never shadow-prefixed -- real credentials, read-only, needed in both modes for market data
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")   # same
LOG_FILE = os.path.join(BASE_DIR, f"{_prefix}log.txt")
STATE_FILE = os.path.join(BASE_DIR, f"{_prefix}state.json")
TRADES_FILE = os.path.join(BASE_DIR, f"{_prefix}trades.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, f"{_prefix}heartbeat.txt")
STATUS_FILE = os.path.join(BASE_DIR, f"{_prefix}status.json")

MAX_SLIPPAGE = 1
# --- Fill aggression -----------------------------------------------------------------
# Live results with the old ask+1c tolerance: ~1 entry fill in 8 signals, and a stop that
# either never filled (empty overnight book) or slipped to 57c against a 75c trigger.
# Because a marketable limit fills at the BEST RESTING PRICE rather than at the limit,
# raising these does not worsen normal fills -- it only stops us missing them.
AGGRESSIVE_ENTRY = True    # bid up to MAX_ENTRY_THRESHOLD instead of ask+MAX_SLIPPAGE.
                            # Cannot overpay: MAX_ENTRY_THRESHOLD is already the top of the
                            # band we accept, and price improvement still fills us at the
                            # best ask (a 93c ask fills at 93c even with a 95c limit).
AGGRESSIVE_EXIT = True     # price exits well through the bid so they actually cross.
EXIT_SLIPPAGE_CENTS = 15   # how far below the bid to place an exit. Wide on purpose: the
                            # bid is thin exactly when a stop fires, and price improvement
                            # means we still receive the best available bid, not this price.
                            # It is a floor on what we will accept, not what we expect.
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR_PCT = 0.75       # bot halts if cash drops to this fraction of the highest balance ever reached (trailing, not a fixed dollar amount)
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.20
ENTRY_THRESHOLD = 93          # minimum yes_bid/no_bid cents to consider (was exact == 93)
RISK_PCT = 0.01               # flat risk per trade
ORDER_POLL_SECONDS = 3        # how many 1s polls to wait for a fill before canceling the rest
WICK_MIN_PCT = 0.00015        # min rejection-wick size as a % of BTC price (~$15 at $100k BTC)
MAX_ENTRY_THRESHOLD = 95      # never take a fresh entry priced above this -- 96-99c entries are intentionally excluded
                               # (tightened from 97 after paper-run results showed the 93-95c band performing better)
ENTRY_TIME_LEFT_MIN = 1.75     # earliest entry: minutes remaining in the 15m window
ENTRY_TIME_LEFT_MAX = 5.0      # latest entry upper bound (was 4.5 -- see note below)
# NOTE on the 4.5 -> 5.0 change: this came from an in-sample window search over the same
# 3-month backtest period (candidates included 1.0-6.0, 1.5-5.0, 1.75-5.0, 2.0-5.0), and the
# backtest report's own numbers don't fully reconcile -- one table reports 509 trades for the
# original 1.75-4.5 window, another reports 487 for what's supposed to be the same window,
# a 22-trade gap with no stated explanation. Treat this upper bound as UNCONFIRMED until it's
# been checked against the raw trade-level data and/or validated out-of-sample (e.g. a stretch
# of shadow-mode runtime that wasn't part of the backtest sample), not as a settled result.
STOP_CONFIRM_LOOPS = 3        # consecutive loops (~1s each) the FULL stop condition must hold before it actually fires
HIGH_ENTRY_STOP_THRESHOLD = 97            # entries at/above this price get a widened % stop (see below)
HIGH_ENTRY_STOP_LOSS_PCT = 0.375          # 35-40% stop for high-probability (>=97c) entries, vs. the normal 20%
HIGH_ENTRY_STOP_FLOOR_CENTS = 67          # the widened stop never triggers above this hard cents floor (~65-70c)
HIGH_ENTRY_STOP_DISABLE_THRESHOLD = 96    # entries at/above this price...
HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN = 1.75  # ...have the stop fully disabled once time_left drops below this
# NOTE: with MAX_ENTRY_THRESHOLD=95, no entry can ever reach HIGH_ENTRY_STOP_THRESHOLD (97) or
# HIGH_ENTRY_STOP_DISABLE_THRESHOLD (96) -- the four constants above are currently dormant, and
# every position now uses the plain STOP_LOSS_THRESHOLD (20%) stop. Left in place rather than
# deleted so re-raising MAX_ENTRY_THRESHOLD later restores the widened/disabled-stop behavior
# without having to re-derive these values.
MAX_DAILY_DRAWDOWN_PCT = 0.10  # if today's cash is down this fraction from today's opening cash,
                                # pause new entries until midnight ET. Existing open positions are
                                # still monitored and can still stop out or settle normally while paused.
BTC_STOP_CONFIRM_FRACTION = 0.5           # live BTC must have reversed >= this fraction of its original entry-time
                                           # distance from window-open before a stop is allowed to fire
STOP_RETRY_BACKOFF_SECONDS = 5   # wait between failed stop-loss re-submissions. A stop fires when the
                                  # book is moving away, so the bid is often thin or absent; retrying every
                                  # loop just churns orders against nothing.
STOP_RETRY_WARN_AFTER = 5        # after this many failed exits, warn loudly that the position is likely
                                  # riding to settlement for a full loss rather than a stopped one
SKIP_LOG_THROTTLE_SECONDS = 30   # suppress repeat "Skip" log lines for the identical ticker/side/price/reason
                                  # within this window -- logs immediately again the moment any of those change
HEARTBEAT_PRINT_INTERVAL_SECONDS = 10   # console "Risk: ..." status line refresh rate. heartbeat.txt (the
                                         # dashboard's liveness file) still updates every loop regardless --
                                         # this only throttles the human-readable console/stdout line, which
                                         # floods any output that doesn't collapse \r (redirected to a file,
                                         # piped through a log viewer, run under a supervisor, etc.)
OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00
LAST_FILTER_CHECK = None      # most recent done-deal filter evaluation, for status.json

# ====================== TRADING SCHEDULE ======================
# 24/7 version: always in a trading window.
def in_trading_window():
    return True

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")

def write_heartbeat():
    try:
        with open(HEARTBEAT_FILE, "w", encoding="utf-8") as f:
            f.write(datetime.now(pytz.timezone("US/Eastern")).isoformat())
    except Exception:
        pass

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            try:
                return json.load(f)
            except Exception as e:
                log(f"⚠️ state.json parse error, using defaults: {e}")
    return {"strikes": 0, "current_trade": None}

def write_json_atomic(path, data):
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)

def save_state(state):
    write_json_atomic(STATE_FILE, state)

def write_status(payload):
    try:
        write_json_atomic(STATUS_FILE, payload)
    except Exception:
        pass

def update_trades_json(trade_entry):
    trades = []
    trade_entry["category"] = "bot"
    if os.path.exists(TRADES_FILE):
        with open(TRADES_FILE, "r", encoding="utf-8") as f:
            try:
                trades = json.load(f)
            except Exception as e:
                log(f"⚠️ trades.json parse error, starting fresh log: {e}")
                trades = []
    trades.append(trade_entry)
    with open(TRADES_FILE, "w", encoding="utf-8") as f:
        json.dump(trades, f, indent=2)

def safe_price_cents(value) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except Exception as e:
        log(f"⚠️ safe_price_cents parse error for value={value!r}: {e}")
        return 0

# ====================== QUOTE HELPERS ======================
# On Kalshi the yes and no books are the same book, so:
#     yes_ask == 100 - no_bid        no_ask == 100 - yes_bid
# We prefer the API's own ask field when it is present and non-zero, and fall
# back to the complement otherwise. A 0 result means "no offer resting on that
# side" -- callers must treat that as un-fillable, not as a price of zero.
def _quote_cents(m, field, complement_field=None):
    c = safe_price_cents(getattr(m, field, None))
    if c <= 0 and complement_field is not None:
        comp = safe_price_cents(getattr(m, complement_field, None))
        c = (100 - comp) if comp > 0 else 0
    return c

def market_bids(m):
    """(yes_bid, no_bid) in cents -- the price you SELL into."""
    return _quote_cents(m, "yes_bid_dollars"), _quote_cents(m, "no_bid_dollars")

def market_asks(m):
    """(yes_ask, no_ask) in cents -- the price you BUY at."""
    return (_quote_cents(m, "yes_ask_dollars", "no_bid_dollars"),
            _quote_cents(m, "no_ask_dollars", "yes_bid_dollars"))

def ask_for_side(m, side):
    y, n = market_asks(m)
    return y if side == "yes" else n

def bid_for_side(m, side):
    y, n = market_bids(m)
    return y if side == "yes" else n

def play_sound(event_type):
    if not HAS_WINDOWS:
        return
    s = {
        "buy": [(2000, 200)],
        "settle_win": [(2500, 200), (3000, 200)],
        "settle_loss": [(600, 500)],
        "stop": [(400, 1000)],
        # Deliberately distinct and insistent -- this one means "act now, by hand".
        "alert": [(2400, 150), (1800, 150), (2400, 150), (1800, 150), (2400, 300)],
    }
    for f, d in s.get(event_type, []):
        winsound.Beep(f, d)

ALERTED_TICKERS = set()          # markets already alerted on -- one signal per market window
ALERT_COOLDOWN_SECONDS = 20      # pause after alerting so the same setup doesn't re-fire immediately

def emit_alert(market, side, price, dist_pct, time_left, qty, current_px, open_px):
    """Signal-only output. Everything needed to place the trade by hand, in one block.

    Prints the stop level up front because that is the number worth deciding BEFORE
    entering, not after -- and because exit speed is the single biggest lever on
    whether this strategy clears its own breakeven.
    """
    stop_cents = int(round(price * (1 - STOP_LOSS_THRESHOLD)))
    max_loss = price / 100.0 * qty
    win_amt = (100 - price) / 100.0 * qty
    fee_est = taker_fee_dollars(price, qty)
    seconds_left = time_left * 60.0

    bar = "=" * 64
    log("")
    log(bar)
    log(f"🔔 SIGNAL — BUY {side.upper()} @ {price}c   ({qty} contract{'s' if qty != 1 else ''})")
    log(bar)
    log(f"   Market      {market.ticker}")
    log(f"   Time left   {time_left:.1f} min  (~{seconds_left:.0f}s to close)")
    log(f"   BTC move    {dist_pct:+.3f}%   (open {open_px:,.2f} -> now {current_px:,.2f})")
    log("")
    log(f"   STOP AT     {stop_cents}c  ({side.upper()} bid) — set this BEFORE you enter")
    log(f"   Risk        ${max_loss:.2f} max   |   Target +${win_amt:.2f}   |   Fee ~${fee_est:.2f}")
    log("")
    log(f"   ⏱ Act within a few seconds or SKIP — the filter measured this setup NOW,")
    log(f"     and a late entry is a different trade than the one that qualified.")
    log(bar)
    log("")
    if ALERT_SOUND:
        play_sound("alert")

# ====================== BTC PRICE HELPERS (for done-deal filters) ======================
_last_klines_error_log_ts = 0.0
KLINES_ERROR_LOG_THROTTLE_SECONDS = 30

def get_btc_klines(limit=25):
    """
    Fetch recent 1-min BTCUSDT candles from Binance's public market-data
    endpoint. Uses data-api.binance.vision (US-friendly) rather than api.binance.com.
    """
    global _last_klines_error_log_ts
    try:
        url = "https://data-api.binance.vision/api/v3/klines"
        params = {"symbol": "BTCUSDT", "interval": "1m", "limit": limit}
        r = requests.get(url, params=params, timeout=4)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        now = time.time()
        if now - _last_klines_error_log_ts > KLINES_ERROR_LOG_THROTTLE_SECONDS:
            log(f"⚠️ BTC klines fetch error: {e}")
            _last_klines_error_log_ts = now
        return []

def get_window_open_price(close_time, klines):
    """Find the open price of the 15-min window (approx close_time - 15 min)."""
    if not klines:
        return None
    window_start_ms = int((close_time.timestamp() - 15 * 60) * 1000)
    best = None
    best_diff = float("inf")
    for k in klines:
        diff = abs(k[0] - window_start_ms)
        if diff < best_diff:
            best_diff = diff
            best = k
    if best and best_diff < 90_000:  # within 90 seconds
        return float(best[1])  # candle open
    return None

def required_distance_pct(time_left_min: float) -> float:
    """Time-adjusted minimum % move from open to consider 'done deal' (slightly loosened)."""
    if time_left_min > 3.5:
        return 0.10
    elif time_left_min > 2.5:
        return 0.06
    else:
        return 0.06  # was 0.04 -- the marginal late-window entries at 0.04-0.05% were too easily flash-triggered

def check_momentum_and_wick(klines, direction: str, ref_price: float) -> bool:
    """
    direction = 'up' or 'down'
    Returns True if short-term momentum is still aligned AND no large opposing wick
    in the last ~2 minutes.
    """
    if len(klines) < 5:
        return False
    recent = klines[-4:-1]  # previous 3 completed 1m candles
    if len(recent) < 3:
        return False

    closes = [float(c[4]) for c in recent]
    highs  = [float(c[2]) for c in recent]
    lows   = [float(c[3]) for c in recent]
    opens  = [float(c[1]) for c in recent]

    net_move = closes[-1] - closes[0]
    if direction == "up" and net_move <= 0:
        return False
    if direction == "down" and net_move >= 0:
        return False

    min_wick_dollars = ref_price * WICK_MIN_PCT

    for i in range(-2, 0):
        body = abs(closes[i] - opens[i])
        if body < 1e-8:
            body = 1e-8
        if direction == "up":
            lower_wick = min(opens[i], closes[i]) - lows[i]
            if lower_wick > body * 2.5 and lower_wick > min_wick_dollars:
                return False
        else:
            upper_wick = highs[i] - max(opens[i], closes[i])
            if upper_wick > body * 2.5 and upper_wick > min_wick_dollars:
                return False
    return True

def btc_confirms_stop(curr):
    """
    Guard against firing a stop-loss purely off a Kalshi order-book spike:
    only allow it if live BTC price has also closed back at least
    BTC_STOP_CONFIRM_FRACTION of its original entry-time distance from the
    window-open reference price. A pure book flash crash with BTC still
    well on our side should not force an exit.
    """
    entry_btc_price = curr.get("entry_btc_price")
    entry_open_px = curr.get("entry_open_px")
    entry_direction = curr.get("entry_direction")
    if entry_btc_price is None or entry_open_px is None or entry_direction is None:
        # Trade opened before this field existed, or entry-time BTC context
        # wasn't captured for some other reason -- fail safe and allow the
        # stop rather than silently blocking it forever.
        return True

    klines = get_btc_klines(limit=5)
    if not klines:
        return True  # can't confirm without fresh data -- fail safe, allow the stop
    now_px = float(klines[-1][4])

    original_distance = abs(entry_btc_price - entry_open_px)
    if original_distance <= 0:
        return True

    if entry_direction == "up":
        reversal = entry_btc_price - now_px   # positive = BTC has fallen back
    else:
        reversal = now_px - entry_btc_price   # positive = BTC has risen back

    return (reversal / original_distance) >= BTC_STOP_CONFIRM_FRACTION

# ====================== API SETUP ======================
with open(APIKEY_FILE, "r", encoding="utf-8") as f:
    api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f:
    private_key_pem = f.read()

# Host note: create_order_v2 hits resource path /portfolio/events/orders (a V2-only path,
# distinct from the legacy /portfolio/orders). api.elections.kalshi.com does NOT route it --
# it returns a CloudFront 404 before the request reaches Kalshi's application layer -- even
# though read endpoints like get_balance/get_markets work there fine. external-api.kalshi.com
# is the SDK's own default and is described in kalshi_python_sync.configuration as the
# "Production Trade API server"; api.elections.kalshi.com is listed separately as the
# "Production shared API server, also supported".
config = Configuration(host="https://external-api.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

SELF_TRADE_PREVENTION_TYPE = "taker_at_cross"

def _to_book_order(side, action, price_cents):
    if side == "yes":
        book_side = "bid" if action == "buy" else "ask"
        book_price_cents = price_cents
    else:  # side == "no"
        book_side = "ask" if action == "buy" else "bid"
        book_price_cents = 100 - price_cents
    return book_side, book_price_cents

def place_order(ticker, side, count, action, price_cents=None):
    """Returns (filled_count, fill_price_cents).

    fill_price_cents is what the caller must use for PnL -- in pessimistic
    shadow mode it can differ from the requested price_cents.
    """
    # Belt-and-braces: alert-only mode should never reach here (the caller branches
    # to emit_alert first), but if any future code path calls place_order directly,
    # refuse rather than quietly placing a real order in a signal-only session.
    if ALERT_ONLY:
        log(f"🛑 place_order blocked: --alert-only is set ({action} {count}x {ticker}). "
            f"No order was sent.")
        return 0, price_cents
    # --- Limit pricing -------------------------------------------------------
    # Kalshi has no market-order type: CreateOrderV2Request requires a price and every
    # order is a limit. What the Kalshi UI calls a "market order" is just a limit priced
    # aggressively enough to cross the book (your own manual fill showed "Limit 41c -
    # Taker"). A marketable limit also gets PRICE IMPROVEMENT -- it executes against the
    # best resting order, not at your limit. Observed live: limit 15c filled at 13c,
    # limit 6c filled at 5c.
    #
    # So a wider limit does not mean paying more on a normal fill; it only means not
    # MISSING one when the quote moves between reading the book and the order landing.
    # The old ask+1c tolerance is what produced a ~1-in-8 entry fill rate.
    #
    # Entry cap is the strategy's own ceiling: we already accept anything up to
    # MAX_ENTRY_THRESHOLD, so bidding that much can never overpay by definition.
    # Exits go aggressive deliberately -- a stop fires when the book is running away,
    # and filling at a poor bid beats not filling and riding to a full loss.
    if action == "buy":
        if AGGRESSIVE_ENTRY:
            limit_cents = min(99, max(price_cents, MAX_ENTRY_THRESHOLD))
        else:
            limit_cents = min(99, price_cents + MAX_SLIPPAGE)
    else:
        if AGGRESSIVE_EXIT:
            limit_cents = max(1, price_cents - EXIT_SLIPPAGE_CENTS)
        else:
            limit_cents = max(1, price_cents - MAX_SLIPPAGE)

    if SHADOW_MODE:
        fill_price = price_cents

        if SHADOW_PESSIMISTIC:
            # The signal's quote is already stale by the time we get here: the
            # done-deal filters make a Binance klines call (up to 4s) between
            # reading the book and ordering. Re-read the book and only fill if
            # our limit still crosses it -- and fill at the NEW price.
            try:
                m = client.get_market(ticker).market
            except Exception as e:
                log(f"🌗 SHADOW[pess] NO FILL {action.upper()} {ticker}: quote re-fetch failed ({e})")
                return 0, price_cents

            if action == "buy":
                fresh = ask_for_side(m, side)
                if fresh <= 0 or fresh > limit_cents:
                    log(f"🌗 SHADOW[pess] NO FILL buy {ticker} {side}: ask "
                        f"{str(fresh) + 'c' if fresh > 0 else 'none resting'} > limit {limit_cents}c "
                        f"(signal saw {price_cents}c)")
                    return 0, price_cents
            else:
                fresh = bid_for_side(m, side)
                if fresh <= 0 or fresh < limit_cents:
                    log(f"🌗 SHADOW[pess] NO FILL sell {ticker} {side}: bid "
                        f"{str(fresh) + 'c' if fresh > 0 else 'none resting'} < limit {limit_cents}c "
                        f"(signal saw {price_cents}c)")
                    return 0, price_cents

            if fresh != price_cents:
                log(f"🌗 SHADOW[pess] slippage on {action}: {price_cents}c -> {fresh}c")
            fill_price = fresh

        fee = shadow_taker_fee_dollars(fill_price, count)
        state = load_state()
        state["shadow_cash"] = state.get("shadow_cash", _args.shadow_cash) - fee
        save_state(state)
        log(f"🌗 SHADOW {action.upper()} {count}x {ticker} @ {fill_price}c (fee ${fee:.4f}, simulated fill)")
        return count, fill_price

    try:
        client_order_id = str(uuid.uuid4())
        actual_price_cents = limit_cents
        book_side, book_price_cents = _to_book_order(side, action, actual_price_cents)

        resp = client.create_order_v2(
            ticker=ticker,
            client_order_id=client_order_id,
            side=book_side,
            count=f"{count:.2f}",
            price=f"{book_price_cents / 100:.2f}",
            time_in_force="good_till_canceled",
            self_trade_prevention_type=SELF_TRADE_PREVENTION_TYPE,
        )
        exchange_order_id = resp.order_id
        filled = int(round(float(resp.fill_count)))

        # CreateOrderV2Response carries the realised average fill price as a fixed-point
        # dollar string. Prefer it over the requested limit -- on a crossing order the true
        # fill is at or better than our limit, so using the limit understated live PnL.
        #
        # CRITICAL: average_fill_price is quoted in BOOK terms -- Kalshi runs a single
        # YES-denominated book. _to_book_order() converts a NO price p to (100 - p) on the
        # way out, so the fill comes back inverted for NO trades and MUST be converted back
        # here. Storing the raw book price as entry_price_cents recorded a 94c NO entry as
        # 6c, which both inflated settlement PnL ((100-6)=94c "profit" instead of 6c) and
        # silently disabled the stop-loss (stop_p = 6*0.8 = 4.8c, which the ~94c NO bid can
        # never breach). YES trades are unaffected since book price == yes price.
        fill_price_cents = price_cents
        if filled > 0 and getattr(resp, "average_fill_price", None):
            try:
                book_fill_cents = int(round(float(resp.average_fill_price) * 100))
                fill_price_cents = book_fill_cents if side == "yes" else 100 - book_fill_cents
            except (TypeError, ValueError):
                log(f"⚠️ Couldn't parse average_fill_price={resp.average_fill_price!r}; "
                    f"falling back to requested {price_cents}c for PnL.")

        if filled >= count:
            return filled, fill_price_cents

        # --- Partial or no immediate fill: the remainder is resting (GTC) ---
        # We deliberately do NOT poll with get_order here. get_order serialises to the
        # legacy path /portfolio/orders/{order_id}, which external-api.kalshi.com does not
        # route -- it 404s (verified live). The old poll loop let that 404 escape to the
        # outer handler, which returned filled=0 while the order was still live on the
        # exchange: the bot would log "Entry failed" and move on while actually holding an
        # unmanaged position with no stop-loss and no settlement tracking.
        #
        # Instead: give the resting remainder a moment, then cancel it. CancelOrderV2Response
        # returns reduced_by = the number of contracts pulled off the book, so anything that
        # filled in the meantime is exactly count - reduced_by. Uses only V2 paths.
        time.sleep(ORDER_POLL_SECONDS)
        try:
            cancel_resp = client.cancel_order_v2(order_id=exchange_order_id)
            reduced = int(round(float(cancel_resp.reduced_by)))
            filled = max(0, count - reduced)
            if filled > 0:
                log(f"ℹ️ Order {exchange_order_id}: {filled}/{count} filled while resting "
                    f"({reduced} cancelled).")
        except Exception as ce:
            # Cancel failed -- most likely the order fully executed before we got here, but
            # we must not guess. Ask the exchange what we actually own.
            log(f"⚠️ Cancel failed for {exchange_order_id} ({ce}) -- verifying via positions.")
            verified = _filled_qty_from_positions(ticker)
            if verified is None:
                log(f"🚨 UNVERIFIED ORDER {exchange_order_id} on {ticker}: could not cancel and "
                    f"could not read positions. Assuming FULLY FILLED ({count}) so the position "
                    f"is tracked and stop-loss/settlement still run. CHECK KALSHI MANUALLY.")
                filled = count
            elif action == "buy":
                # Entering from flat: the position we now hold IS what filled.
                filled = min(count, verified)
                log(f"ℹ️ Positions report {verified} contract(s) on {ticker}; booking {filled}.")
            else:
                # Exiting: positions reports what REMAINS, not what sold. We came in holding
                # `count`, so the fill is count - remaining. Reading `verified` directly here
                # would invert the result -- a fully-filled exit (remaining 0) would look like
                # a zero fill, and the caller would keep re-selling contracts it no longer
                # owns, flipping the position short.
                filled = max(0, count - verified)
                log(f"ℹ️ Positions report {verified} contract(s) still on {ticker}; "
                    f"booking {filled} of {count} sold.")

        return filled, fill_price_cents
    except Exception as e:
        log(f"❌ Order Error: {e}")
        return 0, price_cents

POSITION_QTY_KEYS = ("position_fp", "position", "net_position", "quantity", "count")

def _extract_position_qty(p):
    raw = {}
    try:
        raw = dict(vars(p))
    except Exception:
        pass
    for key in POSITION_QTY_KEYS:
        if key in raw and raw[key] is not None:
            try:
                return int(round(float(raw[key]))), raw
            except (TypeError, ValueError):
                continue
    return None, raw

def _filled_qty_from_positions(ticker):
    """Absolute contract count currently held on `ticker` per the exchange.

    Returns None if positions can't be read or the schema isn't understood, so
    callers can distinguish "confirmed flat" (0) from "don't know" (None).
    Uses /portfolio/positions, which is routed on the external-api host.
    """
    try:
        positions_resp = client.get_positions()
        for p in positions_resp.market_positions:
            qty, raw_fields = _extract_position_qty(p)
            if qty is None:
                continue
            p_ticker = getattr(p, "ticker", raw_fields.get("ticker"))
            if p_ticker == ticker:
                return abs(qty)
        return 0
    except Exception as e:
        log(f"⚠️ Could not read positions to verify fill on {ticker}: {e}")
        return None

def reconcile_state_with_positions(state):
    try:
        curr = state.get("current_trade")
        positions_resp = client.get_positions()

        live = {}
        schema_issue = False
        for p in positions_resp.market_positions:
            qty, raw_fields = _extract_position_qty(p)
            if qty is None:
                if not schema_issue:
                    log(f"⚠️ Reconcile: no position-size field found on MarketPosition "
                        f"(tried {POSITION_QTY_KEYS}). Actual fields: {list(raw_fields.keys())}. "
                        f"Skipping reconciliation this run — state.json left unchanged.")
                    schema_issue = True
                continue
            ticker = getattr(p, "ticker", raw_fields.get("ticker"))
            if ticker and qty != 0:
                live[ticker] = qty

        if schema_issue and not live:
            return state

        if curr:
            ticker = curr["ticker"]
            expected_yes = (curr["side"] == "yes")
            actual = live.get(ticker, 0)
            if actual == 0:
                log(f"⚠️ Reconcile: state.json shows open {ticker} but no live position found. Clearing stale state.")
                state["current_trade"] = None
            elif (actual > 0) != expected_yes:
                log(f"⚠️ Reconcile: side mismatch for {ticker} (state={curr['side']}, live_position={actual}). Clearing stale state — verify manually on Kalshi.")
                state["current_trade"] = None
            elif abs(actual) != curr["count"]:
                log(f"⚠️ Reconcile: {ticker} count mismatch (state={curr['count']}, live={abs(actual)}). Updating state to match live position.")
                curr["count"] = abs(actual)
                state["current_trade"] = curr
            else:
                log(f"✅ Reconcile: {ticker} state matches live position ({curr['count']} {curr['side']}).")
        else:
            for ticker, pos in live.items():
                if "KXBTC15M" in ticker:
                    log(f"⚠️ Reconcile: found untracked live position in {ticker} ({pos} contracts) with no matching state. Manual review recommended.")

        save_state(state)
    except Exception as e:
        log(f"⚠️ Reconcile Error (continuing with existing state.json unchanged): {e}")
    return state

# ====================== MAIN LOOP ======================
if __name__ == "__main__":
    if ALERT_ONLY:
        log("=" * 64)
        log(f"🔔 {BOT_NAME} — ALERT-ONLY MODE")
        log(f"   Filters: {ENTRY_THRESHOLD}-{MAX_ENTRY_THRESHOLD}c · "
            f"{ENTRY_TIME_LEFT_MIN}-{ENTRY_TIME_LEFT_MAX}m left · 24/7 Done-Deal")
        log("   NO ORDERS WILL BE PLACED. You execute manually on Kalshi.")
        log("   Position monitoring and stop-loss are OFF -- you manage the exit.")
        log("=" * 64)
    else:
        log(f"🪄 {BOT_NAME} Active ({ENTRY_THRESHOLD}-{MAX_ENTRY_THRESHOLD}c · {ENTRY_TIME_LEFT_MIN}-{ENTRY_TIME_LEFT_MAX}m left · 24/7 Done-Deal Filters)")
    if SHADOW_PESSIMISTIC and not SHADOW_MODE:
        log("❌ --shadow-pessimistic requires --shadow. Refusing to start so this isn't mistaken for a dry run.")
        raise SystemExit(2)
    if SHADOW_MODE:
        log(f"🌗 FILL MODEL: {'PESSIMISTIC (re-quote at order time, no-fill if the book moves away)' if SHADOW_PESSIMISTIC else 'OPTIMISTIC (unconditional fill at the signal price)'}")
        log(f"🌗 SHADOW MODE: simulated fills only, no real orders. Starting cash: ${_args.shadow_cash:.2f}. "
            f"Writing to {os.path.basename(STATE_FILE)} / {os.path.basename(LOG_FILE)} / "
            f"{os.path.basename(TRADES_FILE)} (separate from a real instance's files).")

    state = load_state()
    if not SHADOW_MODE:
        state = reconcile_state_with_positions(state)

    _last_skip_log = {"key": None, "ts": 0.0}
    _last_heartbeat_print_ts = 0.0

    while True:
        try:
            write_heartbeat()

            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b':
                    os._exit(0)
                elif key.lower() == b'c':
                    OVERRIDE_TRIGGERED = True

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            cash = state.get("shadow_cash", _args.shadow_cash) if SHADOW_MODE else client.get_balance().balance / 100.0
            if SHADOW_MODE and "shadow_cash" not in state:
                state["shadow_cash"] = cash
                save_state(state)
            curr = state.get("current_trade")
            is_trading_window = in_trading_window()

            peak_cash = max(state.get("peak_cash") or cash, cash)
            if peak_cash != state.get("peak_cash"):
                state["peak_cash"] = peak_cash
                save_state(state)
            safety_floor = peak_cash * SAFETY_FLOOR_PCT

            if OVERRIDE_TRIGGERED:
                log("🛠️ Manual Override: Clearing State")
                state["current_trade"] = None
                save_state(state)
                OVERRIDE_TRIGGERED = False

            if cash <= safety_floor or state.get("strikes", 0) >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Floor ${safety_floor:.2f} ({int(SAFETY_FLOOR_PCT*100)}% of peak ${peak_cash:.2f}) | Strikes {state.get('strikes')}")
                break

            # --- DAILY DRAWDOWN TRACKING ---
            # Resets at ET calendar-day rollover, not a rolling 24h window, so "midnight" means
            # the same thing here as it does to a human reading the log.
            today_str = now_et.strftime("%Y-%m-%d")
            if state.get("daily_date") != today_str:
                state["daily_date"] = today_str
                state["daily_start_cash"] = cash
                state["daily_paused_until"] = None
                save_state(state)
                log(f"📅 New trading day ({today_str} ET): daily drawdown baseline set to ${cash:.2f}")

            daily_start_cash = state.get("daily_start_cash", cash)
            daily_dd_pct = (1 - cash / daily_start_cash) if daily_start_cash > 0 else 0.0

            is_daily_paused = False
            paused_until_str = state.get("daily_paused_until")
            if paused_until_str:
                paused_until = datetime.fromisoformat(paused_until_str)
                if now_et < paused_until:
                    is_daily_paused = True
                else:
                    state["daily_paused_until"] = None
                    save_state(state)
                    log("✅ Daily drawdown pause lifted.")

            if not is_daily_paused and daily_dd_pct >= MAX_DAILY_DRAWDOWN_PCT - 1e-9:
                midnight_next = (now_et + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                state["daily_paused_until"] = midnight_next.isoformat()
                save_state(state)
                is_daily_paused = True
                log(f"🛑 DAILY DRAWDOWN PAUSE: cash ${cash:.2f} is {daily_dd_pct*100:.1f}% below today's open "
                    f"(${daily_start_cash:.2f}), limit is {int(MAX_DAILY_DRAWDOWN_PCT*100)}%. New entries paused "
                    f"until {midnight_next.strftime('%Y-%m-%d %H:%M')} ET. Any open position is still monitored normally.")
                play_sound("stop")

            # --- TICKER FETCH ---
            resp = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', []) if (m.close_time - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: x.close_time)
                market = markets[0]
                time_left = (market.close_time - now_et).total_seconds() / 60.0
                y_bid, n_bid = market_bids(market)
                y_ask, n_ask = market_asks(market)
            else:
                time_left = 0
                y_bid = n_bid = y_ask = n_ask = 0

            # --- MONITORING / STOP LOSS ---
            if ALERT_ONLY and curr and curr.get("status") == "filled":
                # Shouldn't happen (no orders are placed), but if a stale state.json from a
                # live session is present, don't try to manage a position we never opened.
                log("⚠️ Stale current_trade found in alert-only mode -- clearing. "
                    "Manage any real position yourself on Kalshi.")
                state["current_trade"] = None
                save_state(state)
                curr = None

            if curr and curr.get("status") == "filled":
                m_live = client.get_market(curr['ticker']).market
                live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                entry_p = curr['entry_price_cents']

                # Widened stop for high-probability (>=HIGH_ENTRY_STOP_THRESHOLD) entries:
                # a much wider % stop, but never let it trigger above a hard cents floor.
                if entry_p >= HIGH_ENTRY_STOP_THRESHOLD:
                    stop_p = max(entry_p * (1 - HIGH_ENTRY_STOP_LOSS_PCT), HIGH_ENTRY_STOP_FLOOR_CENTS)
                else:
                    stop_p = entry_p * (1 - STOP_LOSS_THRESHOLD)

                # Stop fully disabled in the closing seconds for everyone, and disabled
                # earlier (<HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN) for very high-probability
                # entries -- those are the ones most likely to be a pure late-window flash spike.
                stop_disabled = (time_left <= 0.5) or (
                    entry_p >= HIGH_ENTRY_STOP_DISABLE_THRESHOLD
                    and time_left < HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN
                )

                price_breached = (0 < live_bid <= stop_p) and not stop_disabled

                if price_breached and btc_confirms_stop(curr):
                    # Require the FULL stop condition (price breach + BTC confirmation) to
                    # hold for STOP_CONFIRM_LOOPS consecutive loops (~STOP_CONFIRM_LOOPS
                    # seconds) before actually firing -- filters out single-poll flash spikes.
                    curr['stop_breach_count'] = curr.get('stop_breach_count', 0) + 1
                    state['current_trade'] = curr
                    save_state(state)
                elif curr.get('stop_breach_count', 0):
                    curr['stop_breach_count'] = 0
                    curr['stop_retry_count'] = 0   # price recovered; a later stop starts fresh
                    state['current_trade'] = curr
                    save_state(state)

                if curr.get('stop_breach_count', 0) >= STOP_CONFIRM_LOOPS:
                    log(f"🚨 STOP LOSS: Selling {curr['ticker']} (confirmed over {STOP_CONFIRM_LOOPS} consecutive polls)")
                    filled, exit_price = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                    if filled > 0:
                        # A stop pays the taker fee TWICE -- once entering, once exiting.
                        # Shadow already charged both at fill time, so live only.
                        if SHADOW_MODE:
                            fees = 0.0
                        else:
                            fees = (taker_fee_dollars(entry_p, filled)
                                    + taker_fee_dollars(exit_price, filled))
                        pnl = (exit_price - entry_p) * filled / 100.0 - fees
                        update_trades_json({
                            "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                            "ticker": curr['ticker'],
                            "side": curr['side'],
                            "pnl": round(pnl, 2),
                            "type": "STOP_LOSS",
                            "count": filled,
                        })
                        SESSION_PNL += pnl
                        if SHADOW_MODE:
                            state["shadow_cash"] = state.get("shadow_cash", _args.shadow_cash) + pnl
                        if filled >= curr['count']:
                            state["current_trade"] = None
                        else:
                            log(f"⚠️ Stop-loss partially filled ({filled}/{curr['count']}). {curr['count'] - filled} contracts still open.")
                            curr['count'] -= filled
                            curr['stop_breach_count'] = 0
                            state["current_trade"] = curr
                        state["strikes"] += 1
                        save_state(state)
                        play_sound("stop")
                        continue
                    else:
                        curr['stop_retry_count'] = curr.get('stop_retry_count', 0) + 1
                        n = curr['stop_retry_count']
                        # Back off instead of re-submitting every loop. A stop fires exactly
                        # when the book is running away, so the bid is often thin or gone --
                        # hammering it every ~5s just churns orders against an empty book.
                        # Log the first few attempts, then only every 10th, to avoid the same
                        # log spam we throttled elsewhere.
                        if n <= 3 or n % 10 == 0:
                            log(f"⚠️ Stop-loss order for {curr['ticker']} did not fill "
                                f"(attempt {n}). Will reassess next loop.")
                        if n == STOP_RETRY_WARN_AFTER:
                            log(f"🚨 Stop-loss on {curr['ticker']} has failed {n} times -- the "
                                f"bid is likely gone. This position may ride to settlement for "
                                f"a FULL loss rather than a stopped one.")
                        state["current_trade"] = curr
                        save_state(state)
                        time.sleep(STOP_RETRY_BACKOFF_SECONDS)

            # --- HEARTBEAT / STATUS ---
            status_text = f" [IN: {curr['side'].upper()} @ {curr['entry_price_cents']}c]" if curr else ""
            pause_text = " [DAILY DD PAUSE]" if is_daily_paused else ""
            _now_wall = time.time()
            if _now_wall - _last_heartbeat_print_ts >= HEARTBEAT_PRINT_INTERVAL_SECONDS:
                print(f"\r[{now_et.strftime('%H:%M:%S')}] Risk: {int(RISK_PCT*100)}% | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f} | Daily: {daily_dd_pct*100:+.1f}%{status_text}{pause_text}", end="")
                _last_heartbeat_print_ts = _now_wall

            write_status({
                "updated_at": now_et.isoformat(),
                "cash": round(cash, 2),
                "session_pnl": round(SESSION_PNL, 2),
                "strikes": state.get("strikes", 0),
                "peak_cash": round(peak_cash, 2),
                "safety_floor": round(safety_floor, 2),
                "risk_pct": round(RISK_PCT * 100, 1),
                "entry_threshold": ENTRY_THRESHOLD,
                "max_entry_threshold": MAX_ENTRY_THRESHOLD,
                "daily_start_cash": round(daily_start_cash, 2),
                "daily_drawdown_pct": round(daily_dd_pct * 100, 2),
                "daily_drawdown_limit_pct": MAX_DAILY_DRAWDOWN_PCT * 100,
                "is_daily_paused": is_daily_paused,
                "daily_paused_until": state.get("daily_paused_until"),
                "is_trading_window": is_trading_window,
                "current_trade": curr,
                "market": {
                    "ticker": market.ticker,
                    "time_left_min": round(time_left, 2),
                    "yes_bid": y_bid,
                    "no_bid": n_bid,
                    "yes_ask": y_ask,
                    "no_ask": n_ask,
                    "watching_threshold": (
                        (ENTRY_THRESHOLD <= y_ask <= MAX_ENTRY_THRESHOLD)
                        or (ENTRY_THRESHOLD <= n_ask <= MAX_ENTRY_THRESHOLD)
                    ),
                } if markets else None,
                "last_filter_check": LAST_FILTER_CHECK,
            })

            if (not is_trading_window or is_daily_paused) and not curr:
                time.sleep(10)
                continue
            if not markets:
                time.sleep(5)
                continue

            # --- SETTLEMENT CHECK ---
            if curr and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                time.sleep(35)
                res = getattr(client.get_market(curr['ticker']).market, 'result', '').lower()
                if res in ['yes', 'no']:
                    won = (curr['side'] == res)
                    gross = ((100 - curr['entry_price_cents']) * curr['count'] / 100.0 if won
                             else -(curr['entry_price_cents'] * curr['count'] / 100.0))
                    # Kalshi charges a taker fee on the ENTRY fill; settlement itself is
                    # free. In shadow mode that fee was already deducted from shadow_cash
                    # at fill time, so netting it here too would double-count -- live only.
                    # It matters most at small size: the fee rounds UP to a whole cent per
                    # order, so a 1-contract 94c win is +$0.06 gross but +$0.05 net.
                    entry_fee = 0.0 if SHADOW_MODE else taker_fee_dollars(
                        curr['entry_price_cents'], curr['count'])
                    pnl = gross - entry_fee
                    update_trades_json({
                        "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": curr['ticker'],
                        "side": curr['side'],
                        "pnl": round(pnl, 2),
                        "type": "SETTLEMENT",
                    })
                    SESSION_PNL += pnl
                    if SHADOW_MODE:
                        state["shadow_cash"] = state.get("shadow_cash", _args.shadow_cash) + pnl
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                    state["current_trade"] = None
                    save_state(state)
                    play_sound("settle_win" if won else "settle_loss")
                continue

            # --- ENTRY (≥ ENTRY_THRESHOLD + Done-Deal Filters) ---
            elif not curr and is_trading_window and not is_daily_paused:
                # Widened time band + accept any bid >= threshold (not exact equality),
                # but never above MAX_ENTRY_THRESHOLD -- 98-99c entries are intentionally excluded.
                # Priced off the ASK -- what we would actually pay -- not the bid.
                # ENTRY_THRESHOLD/MAX_ENTRY_THRESHOLD now bound the real cost basis,
                # so the same 93-97c band admits fewer signals than the bid version did.
                y_qualifies = ENTRY_THRESHOLD <= y_ask <= MAX_ENTRY_THRESHOLD
                n_qualifies = ENTRY_THRESHOLD <= n_ask <= MAX_ENTRY_THRESHOLD
                if (ALERT_ONLY and market.ticker in ALERTED_TICKERS):
                    pass   # already alerted on this market window -- don't nag
                elif ENTRY_TIME_LEFT_MIN <= time_left <= ENTRY_TIME_LEFT_MAX and (y_qualifies or n_qualifies):
                    # Prefer the higher of the two sides if both qualify
                    if y_qualifies and n_qualifies:
                        side, price = ("yes", y_ask) if y_ask >= n_ask else ("no", n_ask)
                    elif y_qualifies:
                        side, price = "yes", y_ask
                    else:
                        side, price = "no", n_ask

                    # === Done-deal filters ===
                    klines = get_btc_klines(limit=25)
                    open_px = get_window_open_price(market.close_time, klines)
                    filters_ok = False
                    reason = ""
                    dist_pct = 0.0

                    if open_px and klines:
                        current_px = float(klines[-1][4])
                        dist_pct = abs(current_px - open_px) / open_px * 100.0
                        req_pct = required_distance_pct(time_left)

                        price_dir = "up" if current_px > open_px else "down"

                        if dist_pct >= req_pct:
                            if check_momentum_and_wick(klines, price_dir, current_px):
                                filters_ok = True
                            else:
                                reason = f"momentum/wick fail (dir={price_dir})"
                        else:
                            reason = f"dist {dist_pct:.3f}% < req {req_pct:.2f}%"
                    else:
                        reason = "no BTC price data"

                    LAST_FILTER_CHECK = {
                        "timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"),
                        "ticker": market.ticker,
                        "side": side,
                        "price_cents": price,
                        "dist_pct": round(dist_pct, 3),
                        "filters_ok": filters_ok,
                        "reason": "passed" if filters_ok else reason,
                    }

                    if filters_ok:
                        qty = int(min(MAX_POSITION_DOLLARS, (cash * RISK_PCT)) * 100 // price)
                        if qty >= 1:
                            if ALERT_ONLY:
                                emit_alert(market, side, price, dist_pct, time_left, qty,
                                           current_px, open_px)
                                # Don't re-alert on the same market window. One signal per market.
                                ALERTED_TICKERS.add(market.ticker)
                                time.sleep(ALERT_COOLDOWN_SECONDS)
                                continue
                            log(f"⚡ DONE-DEAL: {side.upper()} @ {price}c | dist={dist_pct:.3f}% | t={time_left:.1f}m (Qty: {qty})")
                            filled, fill_price = place_order(market.ticker, side, qty, "buy", price)
                            if filled > 0:
                                if filled < qty:
                                    log(f"⚠️ Partial fill on entry: {filled}/{qty} contracts.")
                                state["current_trade"] = {
                                    "ticker": market.ticker,
                                    "side": side,
                                    "count": filled,
                                    "entry_price_cents": fill_price,
                                    "status": "filled",
                                    "entry_btc_price": current_px,
                                    "entry_open_px": open_px,
                                    "entry_direction": price_dir,
                                }
                                save_state(state)
                                play_sound("buy")
                                time.sleep(5)
                            else:
                                log("⚠️ Entry failed (no fill). 15s Cooldown...")
                                time.sleep(15)
                        else:
                            min_risk_needed = (price / (cash * 100)) if cash > 0 else float("inf")
                            min_cash_needed = price / 100 / RISK_PCT if RISK_PCT > 0 else float("inf")
                            skip_reason = (f"passed filters but qty=0 at {RISK_PCT*100:.1f}% risk on ${cash:.2f} "
                                           f"(need ~{min_risk_needed*100:.2f}% risk or ~${min_cash_needed:.2f}+ cash)")
                            log(f"⚠️ DONE-DEAL signal passed ({side.upper()} @ {price}c) but sized to 0 contracts — {skip_reason}")
                            LAST_FILTER_CHECK["filters_ok"] = False
                            LAST_FILTER_CHECK["reason"] = skip_reason
                    else:
                        _skip_key = f"{market.ticker}|{side}|{price}|{reason}"
                        _now = time.time()
                        if _skip_key != _last_skip_log["key"] or (_now - _last_skip_log["ts"]) >= SKIP_LOG_THROTTLE_SECONDS:
                            log(f"⏭ Skip ≥{ENTRY_THRESHOLD}c {side.upper()} @ {price}c: {reason}")
                            _last_skip_log["key"] = _skip_key
                            _last_skip_log["ts"] = _now

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)