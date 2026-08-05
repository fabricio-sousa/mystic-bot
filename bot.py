import os
import json
import time
import uuid
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

# ====================== CONFIG ======================
BOT_NAME = "Mystic-Bot 1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APIKEY_FILE = os.path.join(BASE_DIR, "apikey.txt")
PRIVATE_FILE = os.path.join(BASE_DIR, "private.txt")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "heartbeat.txt")
STATUS_FILE = os.path.join(BASE_DIR, "status.json")

MAX_SLIPPAGE = 1
MAX_POSITION_DOLLARS = 500.0
SAFETY_FLOOR = 78
STRIKE_LIMIT = 3
STOP_LOSS_THRESHOLD = 0.20
ENTRY_THRESHOLD = 93          # yes_bid/no_bid cents level that triggers entry (was 97)
RISK_PCT = 0.01               # flat risk per trade; was per-window via get_dynamic_risk, now constant everywhere
ORDER_POLL_SECONDS = 3       # how many 1s polls to wait for a fill before canceling the rest
WICK_MIN_PCT = 0.00015       # min rejection-wick size as a % of BTC price (~$15 at $100k BTC); scales with price instead of a fixed dollar amount
OVERRIDE_TRIGGERED = False
SESSION_PNL = 0.00
LAST_FILTER_CHECK = None   # most recent done-deal filter evaluation, for status.json

# ====================== TRADING SCHEDULE ======================
# Previously "get_dynamic_risk": every window returned the same 0.01 risk
# fraction anyway, so the per-window risk return was dead weight. Risk is
# now the flat RISK_PCT constant above; this function keeps exactly the
# same window schedule and just returns whether "now" falls inside one.
def in_trading_window():
    tz = pytz.timezone("US/Eastern")
    now = datetime.now(tz)
    day = now.weekday() # 0=Mon, 1=Tue, ..., 5=Sat, 6=Sun
    hour = now.hour
    minute = now.minute
    time_float = hour + (minute / 60.0)

    # --- MONDAY through FRIDAY ---
    if 0 <= day <= 4:
        if 0.0 <= time_float < 5.0: return True    # Safe Overnights
        if 5.0 <= time_float < 8.5: return True    # Safe / Low Priority
        if 10.5 <= time_float < 12.0: return True  # High Confidence
        if 12.0 <= time_float < 16.0: return True  # Balanced Midday
        if 16.5 <= time_float < 17.5: return True  # Primary Window
        if 22.0 <= time_float < 24.0: return True  # Asian Open

    # --- SUNDAY ---
    elif day == 6:
        if 12.0 <= time_float < 17.0: return True  # Sunday

    return False

# ====================== HELPERS ======================
def log(msg: str):
    ts = datetime.now(pytz.timezone("US/Eastern")).strftime("%Y-%m-%d %H:%M:%S ET")
    print(f"\n[{ts}] {msg}")
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(f"[{ts}] {msg}\n")

def write_heartbeat():
    """Writes a timestamp every loop pass, independent of log().
    log() only fires on specific events (entries, exits, errors), so during
    quiet or off-window periods log.txt can go stale for a long time even
    while the bot is running fine — this file is what the dashboard should
    use to detect liveness instead."""
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
    """Write-to-temp-then-replace so a crash mid-write never leaves a
    truncated/corrupt file behind."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)

def save_state(state):
    write_json_atomic(STATE_FILE, state)

def write_status(payload):
    """Writes a snapshot of what the bot is currently seeing — market being
    watched, current bid prices, whether it's in a trading window, and the
    last done-deal filter evaluation. Separate from log.txt on purpose: log.txt is
    for events (entries/exits/errors), this is for 'is it actually looking
    right now' — updated every loop pass without spamming either the log or
    the console."""
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
    with open(TRADES_FILE, "w", encoding="utf-8") as f: json.dump(trades, f, indent=2)

def safe_price_cents(value) -> int:
    try:
        return int(round(float(value or 0) * 100))
    except Exception as e:
        log(f"⚠️ safe_price_cents parse error for value={value!r}: {e}")
        return 0

def play_sound(event_type):
    if not HAS_WINDOWS: return
    s = {"buy":[(2000,200)], "settle_win":[(2500,200),(3000,200)], "settle_loss":[(600,500)], "stop":[(400,1000)]}
    for f, d in s.get(event_type, []): winsound.Beep(f, d)

# ====================== BTC PRICE HELPERS (for done-deal filters) ======================
_last_klines_error_log_ts = 0.0
KLINES_ERROR_LOG_THROTTLE_SECONDS = 30  # avoid flooding log.txt when the endpoint is down/blocked

def get_btc_klines(limit=25):
    """
    Fetch recent 1-min BTCUSDT candles from Binance's public market-data
    endpoint. Uses data-api.binance.vision rather than api.binance.com —
    the latter returns HTTP 451 for US-originating requests (Binance
    geo-blocks api.binance.com for US IPs under its Terms of Use).
    data-api.binance.vision is Binance's own documented endpoint for
    public market data and isn't subject to that restriction.
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
    """Time-adjusted minimum % move from open to consider 'done deal'."""
    if time_left_min > 3.5:
        return 0.12   # need bigger move earlier
    elif time_left_min > 2.5:
        return 0.08
    else:
        return 0.05

def check_momentum_and_wick(klines, direction: str, ref_price: float) -> bool:
    """
    direction = 'up' or 'down'
    ref_price = current BTC reference price, used to scale the wick-rejection
    threshold as a % of price (WICK_MIN_PCT) instead of a fixed dollar amount,
    so the filter stays meaningful regardless of what BTC is trading at.

    Returns True if short-term momentum is still aligned AND no large opposing wick
    in the last ~2 minutes.
    """
    if len(klines) < 5:
        return False
    # Last 3 closed candles (ignore the forming one)
    recent = klines[-4:-1]  # previous 3 completed 1m candles
    if len(recent) < 3:
        return False

    closes = [float(c[4]) for c in recent]
    highs  = [float(c[2]) for c in recent]
    lows   = [float(c[3]) for c in recent]
    opens  = [float(c[1]) for c in recent]

    # Momentum: net direction of last 3 closes
    net_move = closes[-1] - closes[0]
    if direction == "up" and net_move <= 0:
        return False
    if direction == "down" and net_move >= 0:
        return False

    min_wick_dollars = ref_price * WICK_MIN_PCT

    # Opposing wick check (last 2 candles)
    for i in range(-2, 0):
        body = abs(closes[i] - opens[i])
        if body < 1e-8:
            body = 1e-8
        if direction == "up":
            # large lower wick relative to body = potential rejection of upside
            lower_wick = min(opens[i], closes[i]) - lows[i]
            if lower_wick > body * 2.5 and lower_wick > min_wick_dollars:
                return False
        else:
            # large upper wick relative to body
            upper_wick = highs[i] - max(opens[i], closes[i])
            if upper_wick > body * 2.5 and upper_wick > min_wick_dollars:
                return False
    return True

# ====================== API SETUP ======================
with open(APIKEY_FILE, "r", encoding="utf-8") as f: api_key_id = f.read().strip()
with open(PRIVATE_FILE, "r", encoding="utf-8") as f: private_key_pem = f.read()

config = Configuration(host="https://api.elections.kalshi.com/trade-api/v2")
config.api_key_id = api_key_id
config.private_key_pem = private_key_pem
client = KalshiClient(config)

# Kalshi's V2 orders endpoint (client.create_order_v2 / cancel_order_v2) quotes
# everything from the YES leg only: "bid" = buy YES, "ask" = sell YES. Buying
# or selling NO is expressed as the complementary trade on YES at (100 - price)
# cents. This bot reasons in yes/no terms everywhere else (state.json, trades,
# the dashboard, the filters) — the conversion happens only here, right at the
# API boundary, so nothing else needs to change.
# Source: kalshi_python_sync 3.23.0's BookSide docstring — "For event markets,
# this refers to the YES leg only: bid means buy YES, ask means sell YES.
# (Selling YES is economically equivalent to buying NO at 1 - price...)"
SELF_TRADE_PREVENTION_TYPE = "taker_at_cross"  # cancels our own taker order rather than crossing our own resting order

def _to_book_order(side, action, price_cents):
    if side == "yes":
        book_side = "bid" if action == "buy" else "ask"
        book_price_cents = price_cents
    else:  # side == "no"
        book_side = "ask" if action == "buy" else "bid"
        book_price_cents = 100 - price_cents
    return book_side, book_price_cents

def place_order(ticker, side, count, action, price_cents=None):
    """
    Places a limit order via Kalshi's V2 orders endpoint and confirms its
    fill status by polling the order directly by its exchange order_id. If
    the order hasn't fully filled within ORDER_POLL_SECONDS, cancels
    whatever remains resting so nothing is left unmanaged on the book.

    V2's contract counts and prices are fixed-point decimal strings (e.g.
    "1.00", "0.97") rather than the ints/cents the old V1 endpoint used —
    converted at this boundary so the rest of the bot keeps working in
    plain integers and cents throughout.

    Returns the number of contracts actually filled (0 if none). Callers
    should treat this as a partial-fill-aware count, not a boolean.
    """
    try:
        client_order_id = str(uuid.uuid4())
        actual_price_cents = min(99, price_cents + MAX_SLIPPAGE) if action == "buy" else max(1, price_cents - MAX_SLIPPAGE)
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
                return filled
            time.sleep(1)
            o = client.get_order(exchange_order_id).order
            filled = int(round(float(o.fill_count_fp)))
            if o.status == "executed":
                return filled

        if filled < count:
            try:
                client.cancel_order_v2(exchange_order_id)
                # cancel_order_v2's response carries reduced_by, not a total
                # fill count — re-read the order directly for the
                # authoritative final count.
                o = client.get_order(exchange_order_id).order
                filled = int(round(float(o.fill_count_fp)))
            except Exception as ce:
                log(f"⚠️ Cancel Error for order {exchange_order_id}: {ce}")

        return filled
    except Exception as e:
        log(f"❌ Order Error: {e}")
        return 0

POSITION_QTY_KEYS = ("position_fp", "position", "net_position", "quantity", "count")

def _extract_position_qty(p):
    """
    Reads a position-size value off a MarketPosition object. Confirmed
    against kalshi_python_sync 3.23.0's actual source: the field is
    `position_fp`, a fixed-point decimal STRING (e.g. "5.00" / "-3.00"),
    not a plain int named `position` like older SDK versions used. Sign
    convention (positive=YES, negative=NO) is inferred from the rest of the
    V2 schema's YES-relative design, not explicitly restated on this field —
    worth double-checking against a real open position if anything looks off.

    Falls back through a few other plausible names in case of yet another
    version drift, and always returns the raw field dict so an unrecognized
    shape can be logged rather than silently misread.

    Returns (qty, raw_fields_dict). qty is None if nothing matched/parsed.
    """
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
    """
    One-time startup check: verifies state.json's current_trade actually
    matches a live Kalshi position. Protects against a stale/desynced state
    file after a crash, manual kill, or restart mid-trade.

    If the position-size field can't be read at all (schema mismatch),
    this logs the real field names it found and leaves state.json alone
    rather than risk wiping out a real open position based on a bad read.
    """
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
    log(f"🪄 {BOT_NAME} Active ({ENTRY_THRESHOLD}c Done-Deal Filters + New Schedule)")

    state = load_state()
    state = reconcile_state_with_positions(state)

    while True:
        try:
            write_heartbeat()

            if HAS_WINDOWS and msvcrt.kbhit():
                key = msvcrt.getch()
                if key == b'\x1b': os._exit(0)
                elif key.lower() == b'c': OVERRIDE_TRIGGERED = True

            now_et = datetime.now(pytz.timezone("US/Eastern"))
            state = load_state()
            cash = client.get_balance().balance / 100.0
            curr = state.get("current_trade")
            is_trading_window = in_trading_window()

            if OVERRIDE_TRIGGERED:
                log("🛠️ Manual Override: Clearing State"); state["current_trade"] = None
                save_state(state); OVERRIDE_TRIGGERED = False

            if cash <= SAFETY_FLOOR or state.get("strikes", 0) >= STRIKE_LIMIT:
                log(f"🚨 Shutdown: Cash ${cash:.2f} | Strikes {state.get('strikes')}"); break

            # --- TICKER FETCH ---
            resp = client.get_markets(series_ticker="KXBTC15M", limit=5, status="open")
            markets = [m for m in getattr(resp, 'markets', []) if (m.close_time - now_et).total_seconds() > 0]

            if markets:
                markets.sort(key=lambda x: x.close_time)
                market = markets[0]
                time_left = (market.close_time - now_et).total_seconds() / 60.0
                y_p, n_p = safe_price_cents(market.yes_bid_dollars), safe_price_cents(market.no_bid_dollars)
            else:
                time_left = 0

            # --- MONITORING / STOP LOSS ---
            if curr and curr.get("status") == "filled":
                m_live = client.get_market(curr['ticker']).market
                live_bid = safe_price_cents(m_live.yes_bid_dollars if curr['side'] == "yes" else m_live.no_bid_dollars)
                entry_p = curr['entry_price_cents']
                stop_p = entry_p * (1 - STOP_LOSS_THRESHOLD)

                # Stability Check: Stop Loss disabled in final 30 seconds
                if 0 < live_bid <= stop_p and time_left > 0.5:
                    log(f"🚨 STOP LOSS: Selling {curr['ticker']}")
                    filled = place_order(curr['ticker'], curr['side'], curr['count'], "sell", live_bid)
                    if filled > 0:
                        pnl = (live_bid - entry_p) * filled / 100.0
                        update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "STOP_LOSS", "count": filled})
                        SESSION_PNL += pnl
                        if filled >= curr['count']:
                            state["current_trade"] = None
                        else:
                            log(f"⚠️ Stop-loss partially filled ({filled}/{curr['count']}). {curr['count'] - filled} contracts still open.")
                            curr['count'] -= filled
                            state["current_trade"] = curr
                        state["strikes"] += 1
                        save_state(state); play_sound("stop"); continue
                    else:
                        log(f"⚠️ Stop-loss order for {curr['ticker']} did not fill. Will reassess next loop.")

            # --- HEARTBEAT ---
            status_text = f" [IN: {curr['side'].upper()} @ {curr['entry_price_cents']}c]" if curr else ""
            print(f"\r[{now_et.strftime('%H:%M:%S')}] Risk: {int(RISK_PCT*100)}% | Cash: ${cash:.2f} | Session: ${SESSION_PNL:+.2f}{status_text}", end="")

            write_status({
                "updated_at": now_et.isoformat(),
                "cash": round(cash, 2),
                "session_pnl": round(SESSION_PNL, 2),
                "strikes": state.get("strikes", 0),
                "risk_pct": round(RISK_PCT * 100, 1),
                "entry_threshold": ENTRY_THRESHOLD,
                "is_trading_window": is_trading_window,
                "current_trade": curr,
                "market": {
                    "ticker": market.ticker,
                    "time_left_min": round(time_left, 2),
                    "yes_bid": y_p,
                    "no_bid": n_p,
                    "watching_97c": (y_p == ENTRY_THRESHOLD or n_p == ENTRY_THRESHOLD),  # key name kept for dashboard compatibility; value now reflects ENTRY_THRESHOLD, not literally 97
                } if markets else None,
                "last_filter_check": LAST_FILTER_CHECK,
            })

            if not is_trading_window and not curr:
                time.sleep(10); continue
            if not markets:
                time.sleep(5); continue

            # --- SETTLEMENT CHECK ---
            if curr and market.ticker != curr["ticker"]:
                log(f"⏳ Finalizing {curr['ticker']}...")
                time.sleep(35)
                res = getattr(client.get_market(curr['ticker']).market, 'result', '').lower()
                if res in ['yes', 'no']:
                    won = (curr['side'] == res)
                    pnl = (100 - curr['entry_price_cents']) * curr['count'] / 100.0 if won else -(curr['entry_price_cents'] * curr['count'] / 100.0)
                    update_trades_json({"timestamp": now_et.strftime("%Y-%m-%d %H:%M:%S"), "ticker": curr['ticker'], "side": curr['side'], "pnl": round(pnl, 2), "type": "SETTLEMENT"})
                    SESSION_PNL += pnl
                    log(f"🏁 RESULT: {res.upper()} | {'WIN' if won else 'LOSS'} | PnL: ${pnl:+.2f}")
                    state["strikes"] = 0 if won else state.get("strikes", 0) + 1
                    state["current_trade"] = None; save_state(state)
                    play_sound("settle_win" if won else "settle_loss")
                # Loop back immediately so the next iteration re-fetches markets,
                # cash, and prices fresh rather than reusing values computed
                # before this 35s blocking sleep.
                continue

            # --- ENTRY (Exactly ENTRY_THRESHOLD cents + Done-Deal Filters) ---
            elif not curr and is_trading_window:
                if 2.0 <= time_left <= 4.0 and (y_p == ENTRY_THRESHOLD or n_p == ENTRY_THRESHOLD):
                    side, price = ("yes", y_p) if y_p == ENTRY_THRESHOLD else ("no", n_p)

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

                        # Determine price direction from open
                        price_dir = "up" if current_px > open_px else "down"

                        # Require large enough move + aligned momentum + no large opposing wick
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
                            filled = place_order(market.ticker, side, qty, "buy", price)
                            if filled > 0:
                                if filled < qty:
                                    log(f"⚠️ Partial fill on entry: {filled}/{qty} contracts.")
                                state["current_trade"] = {"ticker": market.ticker, "side": side, "count": filled, "entry_price_cents": price, "status": "filled"}
                                save_state(state); play_sound("buy")
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
                        # Optional: uncomment to see why entries are skipped
                        # log(f"⏭ Skip {ENTRY_THRESHOLD}c {side}: {reason}")
                        pass

            time.sleep(1)
        except Exception as e:
            log(f"⚠️ Loop Error: {e}")
            time.sleep(5)
