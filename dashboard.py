"""
Mystic-Bot 1.0 — Dashboard
==========================
A single-file, read-only monitoring dashboard for the Mystic-Bot trading bot.

It reads the same files bot.py already writes — state.json, trades.json,
log.txt — from the same folder, and never touches the Kalshi API or your
credentials. It's safe to run alongside the bot at all times.

Setup:
    pip install flask pytz pyngrok
    Place this file in the same folder as bot.py (it reads state.json,
    trades.json, and log.txt from its own directory).

    Optional -- to browse the dashboard from outside your machine via ngrok:
      - Put your ngrok authtoken in a file called ngrok.txt, next to this
        script (same convention as bot.py's apikey.txt / private.txt).
      - Optional but recommended: put "username:password" in a file called
        ngrok_auth.txt, next to this script, to put an HTTP Basic Auth
        prompt in front of the tunnel. This dashboard shows your cash
        balance, open position, and full trade history with no login of
        its own -- an ngrok URL without ngrok_auth.txt is reachable by
        anyone who has the link, not just you.
      - If ngrok.txt is missing, empty, or the tunnel fails to start for
        any reason, the dashboard falls back to local-only
        (http://localhost:5000) rather than failing to start.

Run:
    python dashboard.py

Then open http://localhost:5000 in a browser (and the printed ngrok URL,
if a tunnel started). The page polls for updates automatically -- no need
to refresh.
"""

import os
import json
import math
import argparse
from datetime import datetime

import pytz
from flask import Flask, jsonify

try:
    from pyngrok import ngrok as _ngrok
    HAS_PYNGROK = True
except ImportError:
    HAS_PYNGROK = False

# ====================== CONFIG ======================
BOT_NAME = "Mystic-Bot 1.0"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
TRADES_FILE = os.path.join(BASE_DIR, "trades.json")
LOG_FILE = os.path.join(BASE_DIR, "log.txt")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "heartbeat.txt")
STATUS_FILE = os.path.join(BASE_DIR, "status.json")
NGROK_TOKEN_FILE = os.path.join(BASE_DIR, "ngrok.txt")       # never logged, never committed -- treat like apikey.txt
NGROK_AUTH_FILE = os.path.join(BASE_DIR, "ngrok_auth.txt")   # optional "username:password" for the tunnel's Basic Auth

STRIKE_LIMIT = 3
LOG_TAIL_LINES = 80
RECENT_TRADES_LIMIT = 30
STALE_AFTER_SECONDS = 60   # heartbeat file untouched longer than this => bot considered offline
_argp = argparse.ArgumentParser()
_argp.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)),
                    help="Port to serve on (default: 5000, or $PORT if set)")
_args = _argp.parse_args()
PORT = _args.port
ET = pytz.timezone("US/Eastern")

app = Flask(__name__)

# ====================== NGROK TUNNEL (optional) ======================
def start_ngrok_tunnel(port):
    """Best-effort: any failure here falls back to local-only and prints why,
    it never raises -- a bad ngrok.txt shouldn't take down the dashboard."""
    if not HAS_PYNGROK:
        print("ℹ️  pyngrok not installed (`pip install pyngrok`) -- dashboard is local-only "
              f"at http://localhost:{port}.")
        return None

    if not os.path.exists(NGROK_TOKEN_FILE):
        print(f"ℹ️  {os.path.basename(NGROK_TOKEN_FILE)} not found -- dashboard is local-only "
              f"at http://localhost:{port}. To browse it online, put your ngrok authtoken in "
              f"{os.path.basename(NGROK_TOKEN_FILE)} next to this script.")
        return None

    try:
        with open(NGROK_TOKEN_FILE, "r", encoding="utf-8") as f:
            token = f.read().strip()
    except Exception as e:
        print(f"⚠️  Couldn't read {os.path.basename(NGROK_TOKEN_FILE)} ({e}) -- dashboard is local-only.")
        return None

    if not token:
        print(f"⚠️  {os.path.basename(NGROK_TOKEN_FILE)} is empty -- dashboard is local-only.")
        return None

    basic_auth = None
    if os.path.exists(NGROK_AUTH_FILE):
        try:
            with open(NGROK_AUTH_FILE, "r", encoding="utf-8") as f:
                candidate = f.read().strip()
            if ":" in candidate:
                basic_auth = candidate
            else:
                print(f"⚠️  {os.path.basename(NGROK_AUTH_FILE)} exists but isn't in "
                      f"\"username:password\" format -- starting the tunnel WITHOUT Basic Auth.")
        except Exception as e:
            print(f"⚠️  Couldn't read {os.path.basename(NGROK_AUTH_FILE)} ({e}) -- "
                  f"starting the tunnel WITHOUT Basic Auth.")

    try:
        _ngrok.set_auth_token(token)
        connect_kwargs = {"auth": basic_auth} if basic_auth else {}
        tunnel = _ngrok.connect(port, "http", **connect_kwargs)
        print(f"🌐 ngrok tunnel live: {tunnel.public_url}  (forwards to http://localhost:{port})")
        if basic_auth:
            print("   Basic Auth is enabled on the tunnel.")
        else:
            print("   ⚠️  No Basic Auth set -- this URL shows cash balance, open position, and "
                  f"trade history to anyone who has it. Add {os.path.basename(NGROK_AUTH_FILE)} "
                  "(\"username:password\") to require a login.")
        return tunnel
    except Exception as e:
        print(f"⚠️  ngrok tunnel failed to start ({e}) -- dashboard is local-only at "
              f"http://localhost:{port}.")
        return None

# ====================== DATA HELPERS ======================
def read_json_safe(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def tail_log(path, n=LOG_TAIL_LINES):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        return [line.rstrip("\n") for line in lines[-n:]]
    except Exception:
        return []

def bot_is_online():
    """Liveness is based on heartbeat.txt, which bot.py rewrites every loop
    pass. log.txt is a poor liveness signal on its own: log() only fires on
    specific events (entries, exits, errors), so it can go stale for long
    stretches — off-window hours, or just a quiet trading window — even
    while the bot is running perfectly fine.

    Falls back to log.txt's mtime if heartbeat.txt doesn't exist yet, so
    this still works with an older bot.py that hasn't been updated."""
    check_file = HEARTBEAT_FILE if os.path.exists(HEARTBEAT_FILE) else LOG_FILE
    if not os.path.exists(check_file):
        return False
    try:
        age = datetime.now().timestamp() - os.path.getmtime(check_file)
        return age < STALE_AFTER_SECONDS
    except Exception:
        return False

def _parse_ts(s):
    """trades.json timestamps are 'YYYY-MM-DD HH:MM:SS' in ET, naive."""
    try:
        return ET.localize(datetime.strptime(str(s)[:19], "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return None

def wilson_interval(wins, n, z=1.96):
    """95% confidence interval for a win rate. Wilson rather than normal-approx
    because it stays sane at tiny n and at rates near 0 or 1 -- exactly this case."""
    if n == 0:
        return 0.0, 1.0
    p = wins / n
    denom = 1.0 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)

def compute_projections(trades):
    """Realized PnL over trailing windows, plus forward projections expressed as a
    RANGE from the win-rate confidence interval -- not a point estimate.

    A single projected number from a handful of trades reads as a forecast when it
    is really noise. The interval makes the uncertainty visible: with very few
    trades the lower bound sits deep in the red, which is the honest answer.
    """
    now = datetime.now(ET)
    settled = []
    for t in trades:
        if t.get("type") not in ("SETTLEMENT", "STOP_LOSS"):
            continue
        ts = _parse_ts(t.get("timestamp"))
        if ts is None:
            continue
        settled.append((ts, float(t.get("pnl") or 0.0), t))

    out = {
        "realized_7d": 0.0, "realized_30d": 0.0,
        "proj_7d_low": None, "proj_7d_high": None,
        "proj_30d_low": None, "proj_30d_high": None,
        "settled": 0, "span_days": 0, "trades_per_day": 0.0,
        "win_rate": None, "wr_low": None, "wr_high": None,
        "avg_win": None, "avg_loss": None, "loss_is_theoretical": False,
        "breakeven_wr": None,
        "confidence": "none",
    }
    if not settled:
        return out

    settled.sort(key=lambda x: x[0])
    out["realized_7d"] = round(sum(p for ts, p, _ in settled if (now - ts).days < 7), 2)
    out["realized_30d"] = round(sum(p for ts, p, _ in settled if (now - ts).days < 30), 2)

    n = len(settled)
    out["settled"] = n
    span = max(1.0, (settled[-1][0] - settled[0][0]).total_seconds() / 86400.0)
    out["span_days"] = round(span, 1)
    per_day = n / span
    out["trades_per_day"] = round(per_day, 2)

    wins = [p for _, p, _ in settled if p > 0]
    losses = [-p for _, p, _ in settled if p < 0]
    out["win_rate"] = round(len(wins) / n * 100.0, 1)
    lo, hi = wilson_interval(len(wins), n)
    out["wr_low"], out["wr_high"] = round(lo * 100, 1), round(hi * 100, 1)

    avg_win = sum(wins) / len(wins) if wins else 0.0
    if losses:
        avg_loss = sum(losses) / len(losses)
    else:
        # No loss observed yet. Do NOT treat that as "losses cost nothing" -- derive the
        # magnitude from what a losing trade would actually cost at the entry prices seen.
        entries = [t.get("entry_price_cents") for _, _, t in settled if t.get("entry_price_cents")]
        counts = [t.get("count") or 1 for _, _, t in settled]
        avg_entry = (sum(entries) / len(entries)) if entries else 94.0
        avg_count = (sum(counts) / len(counts)) if counts else 1
        avg_loss = avg_entry / 100.0 * avg_count
        out["loss_is_theoretical"] = True
    out["avg_win"], out["avg_loss"] = round(avg_win, 3), round(avg_loss, 3)

    # The decision-relevant number: what win rate does this win/loss ratio require just
    # to break even? Compare it against the confidence interval above -- if the whole
    # interval sits BELOW breakeven, the observed edge is negative at every plausible
    # win rate, not merely unproven.
    if (avg_win + avg_loss) > 0:
        out["breakeven_wr"] = round(avg_loss / (avg_win + avg_loss) * 100.0, 1)
    else:
        out["breakeven_wr"] = None

    # Expected PnL per trade at each end of the win-rate interval.
    ev_low = lo * avg_win - (1 - lo) * avg_loss
    ev_high = hi * avg_win - (1 - hi) * avg_loss
    for days, key in ((7, "7d"), (30, "30d")):
        out[f"proj_{key}_low"] = round(ev_low * per_day * days, 2)
        out[f"proj_{key}_high"] = round(ev_high * per_day * days, 2)

    out["confidence"] = ("insufficient" if n < 10 else
                         "very low" if n < 30 else
                         "low" if n < 100 else "moderate")
    return out

def compute_stats(trades):
    today_str = datetime.now(ET).strftime("%Y-%m-%d")
    today_pnl, all_pnl, wins, settled = 0.0, 0.0, 0, 0
    for t in trades:
        pnl = t.get("pnl") or 0
        all_pnl += pnl
        if str(t.get("timestamp", "")).startswith(today_str):
            today_pnl += pnl
        if t.get("type") in ("SETTLEMENT", "STOP_LOSS"):
            settled += 1
            if pnl > 0:
                wins += 1
    win_rate = (wins / settled * 100.0) if settled else 0.0
    return {
        "today_pnl": round(today_pnl, 2),
        "all_pnl": round(all_pnl, 2),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
    }

# ====================== ROUTES ======================
@app.route("/api/status")
def api_status():
    state = read_json_safe(STATE_FILE, {"strikes": 0, "current_trade": None})
    trades = read_json_safe(TRADES_FILE, [])
    trades_sorted = sorted(trades, key=lambda t: str(t.get("timestamp", "")), reverse=True)
    scanner = read_json_safe(STATUS_FILE, None) or {}

    return jsonify({
        "bot_name": BOT_NAME,
        "online": bot_is_online(),
        "strikes": state.get("strikes", 0),
        "strike_limit": STRIKE_LIMIT,
        "cash": scanner.get("cash"),
        "peak_cash": scanner.get("peak_cash"),
        "safety_floor": scanner.get("safety_floor"),
        "daily_drawdown_pct": scanner.get("daily_drawdown_pct"),
        "daily_drawdown_limit_pct": scanner.get("daily_drawdown_limit_pct"),
        "is_daily_paused": scanner.get("is_daily_paused", False),
        "projections": compute_projections(trades),
        "current_trade": state.get("current_trade"),
        "recent_trades": trades_sorted[:RECENT_TRADES_LIMIT],
        "stats": compute_stats(trades),
        "log_tail": tail_log(LOG_FILE),
        "scanner": scanner or None,
        "server_time": datetime.now(ET).strftime("%H:%M:%S ET"),
    })

@app.route("/")
def index():
    return INDEX_HTML

# ====================== FRONTEND (HTML + CSS + JS, all inline) ======================
INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Mystic-Bot 1.0</title>
<style>
  :root {
    --bg:        #0a0b0d;
    --panel:     #111318;
    --panel-2:   #14161c;
    --hairline:  #23262d;
    --text:      #e8e6e1;
    --text-dim:  #7a7e88;
    --text-faint:#4c4f57;
    --accent:    #e3a33e;
    --positive:  #5fb88f;
    --negative:  #e0625b;
    --radius:    5px;
    --mono: "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  }

  * { box-sizing: border-box; }

  html, body {
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: var(--sans);
    -webkit-font-smoothing: antialiased;
  }

  a { color: inherit; }

  ::selection { background: var(--accent); color: #0a0b0d; }

  :focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
  }

  /* ---------- Header ---------- */
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 28px;
    border-bottom: 1px solid var(--hairline);
  }

  .wordmark {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 15px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--text);
  }

  .wordmark span { color: var(--accent); }

  .status-pill {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--mono);
    font-size: 12px;
    letter-spacing: 0.08em;
    color: var(--text-dim);
  }

  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--text-faint);
  }

  .dot.online {
    background: var(--positive);
    box-shadow: 0 0 0 3px rgba(95, 184, 143, 0.15);
    animation: pulse 2s ease-in-out infinite;
  }

  .dot.offline { background: var(--negative); }

  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
  }

  .server-time { color: var(--text-faint); }

  /* ---------- Layout ---------- */
  main {
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px;
  }

  .stats-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 1px;
    background: var(--hairline);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
    overflow: hidden;
    margin-bottom: 24px;
  }

  .stat {
    background: var(--panel);
    padding: 16px 18px;
  }

  .stat-label {
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 8px;
  }

  .stat-value {
    font-family: var(--mono);
    font-size: 22px;
    font-weight: 600;
    color: var(--text);
  }

  .stat-value.positive { color: var(--positive); }
  .stat-value.negative { color: var(--negative); }

  .strikes-dots {
    display: flex;
    gap: 6px;
    align-items: center;
    margin-top: 3px;
  }

  .strike-dot {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    border: 1px solid var(--negative);
  }

  .strike-dot.used { background: var(--negative); }

  .columns {
    display: grid;
    grid-template-columns: 1.3fr 1fr;
    gap: 24px;
    align-items: start;
  }

  .proj-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 18px;
    padding: 16px 18px;
  }

  .proj-item .proj-label {
    font-family: var(--mono);
    font-size: 10.5px;
    letter-spacing: 0.09em;
    text-transform: uppercase;
    color: var(--text-faint);
    margin-bottom: 5px;
  }

  .proj-item .proj-value {
    font-family: var(--mono);
    font-size: 17px;
    font-weight: 600;
  }

  .proj-value.proj-range { font-size: 13.5px; }

  .proj-basis {
    padding: 13px 18px 15px;
    font-family: var(--mono);
    font-size: 11px;
    line-height: 1.65;
    color: var(--text-faint);
    border-top: 1px solid var(--hairline);
  }

  .conf-badge {
    display: inline-block;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 2px 7px;
    border-radius: 3px;
  }
  .conf-badge.bad  { color: var(--negative); background: rgba(207,102,90,0.13);  border: 1px solid rgba(207,102,90,0.30); }
  .conf-badge.warn { color: var(--accent);   background: rgba(227,163,62,0.13);  border: 1px solid rgba(227,163,62,0.30); }
  .conf-badge.ok   { color: var(--positive); background: rgba(106,153,120,0.13); border: 1px solid rgba(106,153,120,0.30); }

  .panel {
    background: var(--panel);
    border: 1px solid var(--hairline);
    border-radius: var(--radius);
  }

  .panel-header {
    padding: 13px 18px;
    border-bottom: 1px solid var(--hairline);
    font-size: 11px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--text-dim);
    display: flex;
    justify-content: space-between;
    align-items: center;
  }

  .panel-body { padding: 18px; }

  /* ---------- Position panel ---------- */
  .position-empty {
    font-family: var(--mono);
    font-size: 13px;
    color: var(--text-faint);
    padding: 6px 0 2px;
  }

  .position-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 16px;
  }

  .field-label {
    font-size: 11px;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.06em;
    margin-bottom: 5px;
  }

  .field-value {
    font-family: var(--mono);
    font-size: 16px;
    color: var(--text);
  }

  .badge {
    display: inline-block;
    font-family: var(--mono);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.06em;
    padding: 3px 9px;
    border-radius: 3px;
  }

  .badge.yes { color: var(--positive); background: rgba(95, 184, 143, 0.12); }
  .badge.no  { color: var(--negative); background: rgba(224, 98, 91, 0.12); }
  .badge.skip { color: var(--accent); background: rgba(227, 163, 62, 0.12); }

  .field-value.watch { color: var(--accent); font-weight: 700; }

  .scanner-updated {
    font-family: var(--mono);
    font-size: 10.5px;
    color: var(--text-faint);
    text-transform: none;
    letter-spacing: 0;
  }

  .filter-check {
    margin-top: 18px;
    padding-top: 16px;
    border-top: 1px solid var(--hairline);
  }

  .filter-check-row {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 8px 0 5px;
    font-family: var(--mono);
    font-size: 13px;
  }

  .filter-check-reason {
    font-family: var(--mono);
    font-size: 12px;
    color: var(--text-dim);
  }

  .dim { color: var(--text-faint); font-family: var(--mono); font-size: 13px; }

  /* ---------- Trade table ---------- */
  table { width: 100%; border-collapse: collapse; font-family: var(--mono); font-size: 12.5px; }

  thead th {
    text-align: left;
    font-size: 10.5px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-faint);
    font-weight: 500;
    padding: 0 18px 10px;
    border-bottom: 1px solid var(--hairline);
  }

  tbody td {
    padding: 9px 18px;
    border-bottom: 1px solid var(--hairline);
    color: var(--text-dim);
    white-space: nowrap;
  }

  tbody tr:last-child td { border-bottom: none; }

  td.side.yes, td.side.no { font-weight: 700; }
  td.side.yes { color: var(--positive); }
  td.side.no { color: var(--negative); }

  td.pnl.positive { color: var(--positive); }
  td.pnl.negative { color: var(--negative); }

  .table-wrap { overflow-x: auto; }
  .table-empty { padding: 18px; font-size: 13px; color: var(--text-faint); font-family: var(--mono); }

  /* ---------- Log panel ---------- */
  .log-body {
    font-family: var(--mono);
    font-size: 12px;
    line-height: 1.6;
    color: var(--text-dim);
    max-height: 520px;
    overflow-y: auto;
    padding: 14px 18px;
    white-space: pre-wrap;
    word-break: break-word;
  }

  .log-body .log-line:last-child { color: var(--text); }

  /* ---------- Footer ---------- */
  footer {
    text-align: center;
    padding: 26px 0 40px;
    font-size: 11px;
    color: var(--text-faint);
    font-family: var(--mono);
    letter-spacing: 0.04em;
  }

  /* ---------- Responsive ---------- */
  @media (max-width: 880px) {
    .columns { grid-template-columns: 1fr; }
    .position-grid { grid-template-columns: 1fr 1fr; }
  }

  @media (prefers-reduced-motion: reduce) {
    .dot.online { animation: none; }
  }
</style>
</head>
<body>

<header>
  <div class="wordmark">MYSTIC<span>-</span>BOT<span> 1.0</span></div>
  <div class="status-pill">
    <span id="status-dot" class="dot offline"></span>
    <span id="status-text">CONNECTING</span>
    <span>·</span>
    <span id="server-time" class="server-time">--:--:--</span>
  </div>
</header>

<main>

  <div class="stats-row">
    <div class="stat">
      <div class="stat-label">Bankroll</div>
      <div class="stat-value" id="cash">&mdash;</div>
    </div>
    <div class="stat">
      <div class="stat-label">Daily Drawdown</div>
      <div class="stat-value" id="daily-dd">&mdash;</div>
    </div>
    <div class="stat">
      <div class="stat-label">Strikes</div>
      <div class="strikes-dots" id="strikes-dots"></div>
    </div>
    <div class="stat">
      <div class="stat-label">Today's PnL</div>
      <div class="stat-value" id="today-pnl">$0.00</div>
    </div>
    <div class="stat">
      <div class="stat-label">All-Time PnL</div>
      <div class="stat-value" id="all-pnl">$0.00</div>
    </div>
    <div class="stat">
      <div class="stat-label">Total Trades</div>
      <div class="stat-value" id="total-trades">0</div>
    </div>
    <div class="stat">
      <div class="stat-label">Win Rate</div>
      <div class="stat-value" id="win-rate">&mdash;</div>
    </div>
  </div>

  <div class="panel" style="margin-bottom:24px;">
    <div class="panel-header">
      <span class="panel-title">Bankroll &amp; Projections</span>
      <span id="conf-badge" class="conf-badge bad">NO DATA</span>
    </div>
    <div class="proj-grid">
      <div class="proj-item">
        <div class="proj-label">Peak / Floor</div>
        <div class="proj-value" id="peak-floor">&mdash;</div>
      </div>
      <div class="proj-item">
        <div class="proj-label">Realized 7d</div>
        <div class="proj-value" id="real-7d">&mdash;</div>
      </div>
      <div class="proj-item">
        <div class="proj-label">Realized 30d</div>
        <div class="proj-value" id="real-30d">&mdash;</div>
      </div>
      <div class="proj-item">
        <div class="proj-label">Breakeven Win Rate</div>
        <div class="proj-value" id="breakeven">&mdash;</div>
      </div>
      <div class="proj-item">
        <div class="proj-label">Projected 7d</div>
        <div class="proj-value proj-range" id="proj-7d">&mdash;</div>
      </div>
      <div class="proj-item">
        <div class="proj-label">Projected 30d</div>
        <div class="proj-value proj-range" id="proj-30d">&mdash;</div>
      </div>
    </div>
    <div class="proj-basis" id="proj-basis">Waiting for settled trades&hellip;</div>
  </div>

  <div class="panel" style="margin-bottom:24px;">
    <div class="panel-header">
      <span>Scanner</span>
      <span id="scanner-updated" class="scanner-updated"></span>
    </div>
    <div class="panel-body" id="scanner-body">
      <div class="position-empty">Waiting for scanner data&hellip;</div>
    </div>
  </div>

  <div class="columns">
    <div style="display:flex; flex-direction:column; gap:24px;">

      <div class="panel">
        <div class="panel-header"><span>Open Position</span></div>
        <div class="panel-body" id="position-body">
          <div class="position-empty">No open position.</div>
        </div>
      </div>

      <div class="panel">
        <div class="panel-header"><span>Trade History</span></div>
        <div class="table-wrap" id="trades-wrap">
          <div class="table-empty">No trades recorded yet.</div>
        </div>
      </div>

    </div>

    <div class="panel">
      <div class="panel-header"><span>Live Log</span></div>
      <div class="log-body" id="log-body">
        <div class="log-line">Waiting for log.txt&hellip;</div>
      </div>
    </div>
  </div>

</main>

<footer>Mystic-Bot 1.0 Dashboard &middot; read-only &middot; polling every 3s</footer>

<script>
  const fmtMoney = (v) => {
    const n = Number(v) || 0;
    const sign = n > 0 ? "+" : "";
    return sign + "$" + n.toFixed(2);
  };

  const pnlClass = (v) => (Number(v) > 0 ? "positive" : (Number(v) < 0 ? "negative" : ""));

  function renderStrikes(strikes, limit) {
    const el = document.getElementById("strikes-dots");
    el.innerHTML = "";
    for (let i = 0; i < limit; i++) {
      const d = document.createElement("span");
      d.className = "strike-dot" + (i < strikes ? " used" : "");
      el.appendChild(d);
    }
  }

  function renderPosition(trade) {
    const el = document.getElementById("position-body");
    if (!trade) {
      el.innerHTML = '<div class="position-empty">No open position.</div>';
      return;
    }
    const side = (trade.side || "").toLowerCase();
    const price = trade.entry_price_cents ?? 0;
    const count = trade.count ?? 0;
    const cost = ((price * count) / 100).toFixed(2);
    el.innerHTML = `
      <div class="position-grid">
        <div>
          <div class="field-label">Ticker</div>
          <div class="field-value">${trade.ticker || "&mdash;"}</div>
        </div>
        <div>
          <div class="field-label">Side</div>
          <div class="field-value"><span class="badge ${side}">${side.toUpperCase()}</span></div>
        </div>
        <div>
          <div class="field-label">Entry Price</div>
          <div class="field-value">${price}&cent;</div>
        </div>
        <div>
          <div class="field-label">Contracts</div>
          <div class="field-value">${count}</div>
        </div>
        <div>
          <div class="field-label">Cost Basis</div>
          <div class="field-value">$${cost}</div>
        </div>
        <div>
          <div class="field-label">Status</div>
          <div class="field-value">${trade.status || "filled"}</div>
        </div>
      </div>
    `;
  }

  function renderTrades(trades) {
    const wrap = document.getElementById("trades-wrap");
    if (!trades || trades.length === 0) {
      wrap.innerHTML = '<div class="table-empty">No trades recorded yet.</div>';
      return;
    }
    const rows = trades.map(t => {
      const side = (t.side || "").toLowerCase();
      const pnl = t.pnl ?? 0;
      return `<tr>
        <td>${t.timestamp || ""}</td>
        <td>${t.ticker || ""}</td>
        <td class="side ${side}">${side.toUpperCase()}</td>
        <td>${t.type || ""}</td>
        <td>${t.count ?? ""}</td>
        <td class="pnl ${pnlClass(pnl)}">${fmtMoney(pnl)}</td>
      </tr>`;
    }).join("");
    wrap.innerHTML = `
      <table>
        <thead>
          <tr>
            <th>Time</th><th>Ticker</th><th>Side</th><th>Type</th><th>Qty</th><th>PnL</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    `;
  }

  const fmtCash = (v) => (v === null || v === undefined) ? "\u2014" : "$" + Number(v).toFixed(2);

  function renderBankroll(data) {
    document.getElementById("cash").textContent = fmtCash(data.cash);

    const ddEl = document.getElementById("daily-dd");
    if (data.daily_drawdown_pct === null || data.daily_drawdown_pct === undefined) {
      ddEl.textContent = "\u2014"; ddEl.className = "stat-value";
    } else {
      // status.json reports drawdown as a POSITIVE number when down from today's open.
      // Show it the way a trader reads it: negative = down = red.
      const signed = -data.daily_drawdown_pct;
      ddEl.textContent = signed.toFixed(1) + "%";
      ddEl.className = "stat-value " + (signed < 0 ? "negative" : signed > 0 ? "positive" : "");
    }

    const pf = document.getElementById("peak-floor");
    pf.textContent = (data.peak_cash != null)
      ? fmtCash(data.peak_cash) + " / " + fmtCash(data.safety_floor)
      : "\u2014";

    const p = data.projections || {};
    const r7 = document.getElementById("real-7d");
    const r30 = document.getElementById("real-30d");
    r7.textContent = fmtMoney(p.realized_7d || 0);
    r7.className = "proj-value " + pnlClass(p.realized_7d || 0);
    r30.textContent = fmtMoney(p.realized_30d || 0);
    r30.className = "proj-value " + pnlClass(p.realized_30d || 0);

    const rangeTxt = (lo, hi) => (lo === null || lo === undefined)
      ? "\u2014"
      : fmtMoney(lo) + "  to  " + fmtMoney(hi);
    document.getElementById("proj-7d").textContent = rangeTxt(p.proj_7d_low, p.proj_7d_high);
    document.getElementById("proj-30d").textContent = rangeTxt(p.proj_30d_low, p.proj_30d_high);

    const beEl = document.getElementById("breakeven");
    if (p.breakeven_wr == null) {
      beEl.textContent = "\u2014"; beEl.className = "proj-value";
    } else {
      beEl.textContent = p.breakeven_wr.toFixed(1) + "%";
      // Red when even the OPTIMISTIC end of the win-rate interval cannot reach breakeven;
      // amber when breakeven falls inside the interval; green when clearly above it.
      beEl.className = "proj-value " + ((p.wr_high < p.breakeven_wr) ? "negative"
                        : (p.wr_low < p.breakeven_wr) ? "" : "positive");
    }

    const badge = document.getElementById("conf-badge");
    const conf = p.confidence || "none";
    badge.textContent = conf === "none" ? "NO DATA" : conf + " confidence";
    badge.className = "conf-badge " + ((conf === "moderate") ? "ok"
                        : (conf === "low") ? "warn" : "bad");

    const basis = document.getElementById("proj-basis");
    if (!p.settled) {
      basis.textContent = "Waiting for settled trades\u2026";
      return;
    }
    let txt = "Projection range spans the 95% confidence interval on win rate ("
            + p.wr_low + "% \u2013 " + p.wr_high + "%, point estimate " + p.win_rate + "%), "
            + "from " + p.settled + " settled trade" + (p.settled === 1 ? "" : "s")
            + " over " + p.span_days + " day" + (p.span_days === 1 ? "" : "s")
            + " at " + p.trades_per_day + " trades/day. "
            + "Avg win $" + (p.avg_win || 0).toFixed(2) + ", avg loss $" + (p.avg_loss || 0).toFixed(2)
            + ", so breakeven needs " + (p.breakeven_wr == null ? "?" : p.breakeven_wr.toFixed(1)) + "%.";
    if (p.breakeven_wr != null && p.wr_high < p.breakeven_wr) {
      txt += " NOTE: even the optimistic end of the win-rate interval ("
           + p.wr_high + "%) is below breakeven, so on the trades recorded so far the "
           + "edge is negative across the whole interval \u2014 not merely unproven.";
    }
    if (p.loss_is_theoretical) {
      txt += " No loss observed yet \u2014 loss size is the theoretical full loss at the "
           + "entry prices seen, not measured.";
    }
    if (p.settled < 30) {
      txt += " At this sample size the interval is too wide to call the strategy "
           + "profitable or unprofitable; assume it is noise until well past 30 trades. "
           + "Both figures also assume the bot keeps running continuously at the same rate.";
    }
    basis.textContent = txt;
  }

  function renderLog(lines) {
    const el = document.getElementById("log-body");
    const wasNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (!lines || lines.length === 0) {
      el.innerHTML = '<div class="log-line">Waiting for log.txt&hellip;</div>';
      return;
    }
    el.innerHTML = lines.map(l => `<div class="log-line">${escapeHtml(l)}</div>`).join("");
    if (wasNearBottom) el.scrollTop = el.scrollHeight;
  }

  function escapeHtml(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  function renderScanner(scanner) {
    const body = document.getElementById("scanner-body");
    const updatedEl = document.getElementById("scanner-updated");

    if (!scanner) {
      updatedEl.textContent = "";
      body.innerHTML = '<div class="position-empty">Waiting for scanner data&hellip; (requires the latest bot.py)</div>';
      return;
    }

    updatedEl.textContent = "updated " + String(scanner.updated_at).substring(11, 19) + " ET";

    // Falls back to 97 only if pointed at an older bot.py that doesn't
    // send entry_threshold yet -- 97 was the only value that ever existed
    // before this field was added, so it's a safe, meaningful default.
    const entryThreshold = scanner.entry_threshold ?? 97;

    const windowPill = `<span class="badge ${scanner.is_trading_window ? "yes" : "no"}">${scanner.is_trading_window ? "IN WINDOW" : "OFF WINDOW"}</span>`;

    let marketBlock;
    if (scanner.market) {
      const m = scanner.market;
      marketBlock = `
        <div class="position-grid">
          <div>
            <div class="field-label">Market</div>
            <div class="field-value">${m.ticker}</div>
          </div>
          <div>
            <div class="field-label">Time Left</div>
            <div class="field-value">${Number(m.time_left_min).toFixed(1)}m</div>
          </div>
          <div>
            <div class="field-label">Yes Bid</div>
            <div class="field-value ${m.yes_bid === entryThreshold ? "watch" : ""}">${m.yes_bid}&cent;</div>
          </div>
          <div>
            <div class="field-label">No Bid</div>
            <div class="field-value ${m.no_bid === entryThreshold ? "watch" : ""}">${m.no_bid}&cent;</div>
          </div>
          <div>
            <div class="field-label">Trading Window</div>
            <div class="field-value">${windowPill}</div>
          </div>
          <div>
            <div class="field-label">Watching ${entryThreshold}&cent;</div>
            <div class="field-value">${m.watching_threshold ? '<span class="badge yes">YES</span>' : '<span class="dim">no</span>'}</div>
          </div>
        </div>`;
    } else {
      marketBlock = `<div class="field-label">Market</div><div class="field-value">${windowPill}&nbsp; no open 15-min market found</div>`;
    }

    let filterBlock = `<div class="position-empty" style="margin-top:16px;">No ${entryThreshold}&cent; setup evaluated yet this session.</div>`;
    if (scanner.last_filter_check) {
      const f = scanner.last_filter_check;
      const side = (f.side || "").toLowerCase();
      filterBlock = `
        <div class="filter-check">
          <div class="field-label">Last Filter Check &middot; ${f.timestamp}</div>
          <div class="filter-check-row">
            <span class="badge ${side}">${side.toUpperCase()}</span>
            <span class="field-value">@ ${f.price_cents}&cent;</span>
            <span class="field-value">dist ${f.dist_pct}%</span>
            <span class="badge ${f.filters_ok ? "yes" : "skip"}">${f.filters_ok ? "ENTERED" : "SKIPPED"}</span>
          </div>
          <div class="filter-check-reason">${f.reason}</div>
        </div>`;
    }

    body.innerHTML = marketBlock + filterBlock;
  }

  async function refresh() {
    try {
      const res = await fetch("/api/status");
      const data = await res.json();

      const dot = document.getElementById("status-dot");
      const text = document.getElementById("status-text");
      dot.className = "dot " + (data.online ? "online" : "offline");
      text.textContent = data.online ? "ONLINE" : "OFFLINE";
      document.getElementById("server-time").textContent = data.server_time;

      renderStrikes(data.strikes, data.strike_limit);
      renderBankroll(data);
      document.getElementById("today-pnl").textContent = fmtMoney(data.stats.today_pnl);
      document.getElementById("today-pnl").className = "stat-value " + pnlClass(data.stats.today_pnl);
      document.getElementById("all-pnl").textContent = fmtMoney(data.stats.all_pnl);
      document.getElementById("all-pnl").className = "stat-value " + pnlClass(data.stats.all_pnl);
      document.getElementById("total-trades").textContent = data.stats.total_trades;
      document.getElementById("win-rate").textContent = data.stats.total_trades > 0 ? data.stats.win_rate + "%" : "\u2014";

      renderPosition(data.current_trade);
      renderTrades(data.recent_trades);
      renderLog(data.log_tail);
      renderScanner(data.scanner);
    } catch (e) {
      const dot = document.getElementById("status-dot");
      const text = document.getElementById("status-text");
      dot.className = "dot offline";
      text.textContent = "DISCONNECTED";
    }
  }

  refresh();
  setInterval(refresh, 3000);
</script>

</body>
</html>
"""

if __name__ == "__main__":
    start_ngrok_tunnel(PORT)
    app.run(host="0.0.0.0", port=PORT, debug=False)
