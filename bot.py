import os
import json
import math
import time
import uuid
import argparse
from datetime import datetime
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
_args = _argp.parse_args()
SHADOW_MODE = _args.shadow
SHADOW_PESSIMISTIC = _args.shadow_pessimistic
SHADOW_TAKER_FEE_MULTIPLIER = 0.07  # Kalshi's real published taker formula, verified against their fee schedule
SHADOW_FEE_CEIL_TO_CENT = True      # Kalshi rounds the fee UP to the next whole cent per order. At 1-contract
                                    # size the raw formula gives ~$0.003, so ignoring the rounding understates
                                    # the real fee by ~3x. Set False to reproduce older backtest numbers.

def shadow_taker_fee_dollars(price_cents, count):
    p = price_cents / 100.0
    raw = count * SHADOW_TAKER_FEE_MULTIPLIER * p * (1 - p)
    if SHADOW_FEE_CEIL_TO_CENT:
        return math.ceil(raw * 100 - 1e-9) / 100.0
    return raw

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
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR_PCT = 0.75       # bot halts if cash drops to this fraction of the highest balance ever reached (trailing, not a fixed dollar amount)
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.20
ENTRY_THRESHOLD = 93          # minimum yes_bid/no_bid cents to consider (was exact == 93)
RISK_PCT = 0.01               # flat risk per trade
ORDER_POLL_SECONDS = 3        # how many 1s polls to wait for a fill before canceling the rest
WICK_MIN_PCT = 0.00015        # min rejection-wick size as a % of BTC price (~$15 at $100k BTC)
MAX_ENTRY_THRESHOLD = 95      # never take a fresh entry priced above this -- 98-99c entries are intentionally excluded
ENTRY_TIME_LEFT_MIN = 1.75    # earliest entry: minutes remaining in the 15m window
ENTRY_TIME_LEFT_MAX = 5.0     # latest entry upper bound (was 4.5; extended after backtest showed 4.5–5.0 adds edge)
STOP_CONFIRM_LOOPS = 3        # consecutive loops (~1s each) the FULL stop condition must hold before it actually fires
HIGH_ENTRY_STOP_THRESHOLD = 97            # entries at/above this price get a widened % stop (see below)
HIGH_ENTRY_STOP_LOSS_PCT = 0.375          # 35-40% stop for high-probability (>=97c) entries, vs. the normal 20%
HIGH_ENTRY_STOP_FLOOR_CENTS = 67          # the widened stop never triggers above this hard cents floor (~65-70c)
HIGH_ENTRY_STOP_DISABLE_THRESHOLD = 96    # entries at/above this price...
HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN = 1.75  # ...have the stop fully disabled once time_left drops below this
BTC_STOP_CONFIRM_FRACTION = 0.5           # live BTC must have reversed >= this fraction of its original entry-time
                                           # distance from window-open before a stop is allowed to fire
OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00
LAST_FILTER_CHECK = None      # most recent done-deal filter evaluation, for status.json
print(f"DEBUG: MAX_ENTRY_THRESHOLD = {MAX_ENTRY_THRESHOLD}")

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
    }
    for f, d in s.get(event_type, []):
        winsound.Beep(f, d)

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

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
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
    limit_cents = (min(99, price_cents + MAX_SLIPPAGE) if action == "buy"
                   else max(1, price_cents - MAX_SLIPPAGE))

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

        for _ in range(ORDER_POLL_SECONDS):
            if filled >= count:
                return filled, price_cents
            time.sleep(1)
            o = client.get_order(exchange_order_id).order
            filled = int(round(float(o.fill_count_fp)))
            if o.status == "executed":
                return filled, price_cents

        if filled < count:
            try:
                client.cancel_order_v2(exchange_order_id)
                o = client.get_order(exchange_order_id).order
                filled = int(round(float(o.fill_count_fp)))
            except Exception as ce:
                log(f"⚠️ Cancel Error for order {exchange_order_id}: {ce}")

        # NOTE: this reports the requested price, not the realised average fill
        # price -- create_order_v2/get_order in this client don't surface it. On a
        # crossing order the true fill is at or better than limit_cents, so live
        # PnL is if anything slightly understated here.
        return filled, price_cents
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
                    state['current_trade'] = curr
                    save_state(state)

                if curr.get('stop_breach_count', 0) >= STOP_CONFIRM_LOOPS:
                    log(f"🚨 STOP LOSS: Selling {curr['ticker']} (confirmed over {STOP_CONFIRM_LOOPS} consecutive polls)")
                    filled, exit_price = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                    if filled > 0:
                        pnl = (exit_price - entry_p) * filled / 100.0
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
                        log(f"⚠️ Stop-loss order for {curr['ticker']} did not fill. Will reassess next loop.")

            # --- HEARTBEAT / STATUS ---
            status_text = f" [IN: {curr['side'].upper()} @ {curr['entry_price_cents']}c]" if curr else ""
            print(f"\r[{now_et.strftime('%H:%M:%S')}] Risk: {int(RISK_PCT*100)}% | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}", end="")

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

            if not is_trading_window and not curr:
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
                    pnl = (100 - curr['entry_price_cents']) * curr['count'] / 100.0 if won else -(curr['entry_price_cents'] * curr['count'] / 100.0)
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
            elif not curr and is_trading_window:
                # Time band ENTRY_TIME_LEFT_MIN..ENTRY_TIME_LEFT_MAX (1.75–5.0 min left).
                # Upper bound extended from 4.5 → 5.0 after backtest: 4.5–5.0 min slice
                # added ~296 high-quality trades at ≥ core win rate / avg PnL.
                # Priced off the ASK -- what we would actually pay -- not the bid.
                # ENTRY_THRESHOLD/MAX_ENTRY_THRESHOLD bound the real cost basis (93–95c).
                y_qualifies = ENTRY_THRESHOLD <= y_ask <= MAX_ENTRY_THRESHOLD
                n_qualifies = ENTRY_THRESHOLD <= n_ask <= MAX_ENTRY_THRESHOLD
                if ENTRY_TIME_LEFT_MIN <= time_left <= ENTRY_TIME_LEFT_MAX and (y_qualifies or n_qualifies):
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
                        log(f"⏭ Skip ≥{ENTRY_THRESHOLD}c {side.upper()} @ {price}c: {reason}")

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)