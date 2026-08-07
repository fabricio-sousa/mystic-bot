"""
Mystic-Bot 1.0 — Shadow Dashboard
=================================
Read-only monitoring dashboard for a `bot.py --shadow` session. Identical
to dashboard.py except it points at the shadow-prefixed files a shadow
session writes (shadow_state.json, shadow_trades.json, shadow_log.txt,
shadow_status.json, shadow_heartbeat.txt) and runs on a different port,
so it can run side-by-side with dashboard.py monitoring a real instance
in the same folder without any conflict.

Every number shown here is SIMULATED — no real orders, no real money.
The UI is deliberately styled differently (violet accent, "SHADOW" badge)
so it's never visually confusable with the real dashboard at a glance.

Setup:
    pip install flask pytz
    Place this file in the same folder as bot.py.

Run:
    python bot.py --shadow --shadow-cash 5000    (in one terminal)
    python shadowdashboard.py                     (in another)

Then open http://localhost:5001 in a browser.
"""

import os
import json
from datetime import datetime

import pytz
from flask import Flask, jsonify

# ====================== CONFIG ======================
BOT_NAME = "Mystic-Bot 1.0 [SHADOW]"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "shadow_state.json")
TRADES_FILE = os.path.join(BASE_DIR, "shadow_trades.json")
LOG_FILE = os.path.join(BASE_DIR, "shadow_log.txt")
HEARTBEAT_FILE = os.path.join(BASE_DIR, "shadow_heartbeat.txt")
STATUS_FILE = os.path.join(BASE_DIR, "shadow_status.json")

STRIKE_LIMIT = 3
LOG_TAIL_LINES = 80
RECENT_TRADES_LIMIT = 30
STALE_AFTER_SECONDS = 60   # heartbeat file untouched longer than this => bot considered offline
ET = pytz.timezone("US/Eastern")

app = Flask(__name__)

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

    return jsonify({
        "bot_name": BOT_NAME,
        "online": bot_is_online(),
        "strikes": state.get("strikes", 0),
        "strike_limit": STRIKE_LIMIT,
        "current_trade": state.get("current_trade"),
        "recent_trades": trades_sorted[:RECENT_TRADES_LIMIT],
        "stats": compute_stats(trades),
        "log_tail": tail_log(LOG_FILE),
        "scanner": read_json_safe(STATUS_FILE, None),
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
<title>Mystic-Bot [SHADOW]</title>
<style>
  :root {
    --bg:        #0a0b0d;
    --panel:     #111318;
    --panel-2:   #14161c;
    --hairline:  #23262d;
    --text:      #e8e6e1;
    --text-dim:  #7a7e88;
    --text-faint:#4c4f57;
    --accent:    #9b7fe3;
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

  .shadow-banner {
    font-family: var(--mono);
    font-weight: 700;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--accent);
    border: 1px solid var(--accent);
    border-radius: 999px;
    padding: 4px 12px;
  }

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

  /* ---------- Ticker tape ---------- */
  .ticker-wrap {
    border-bottom: 1px solid var(--hairline);
    background: var(--panel);
    overflow: hidden;
    white-space: nowrap;
    position: relative;
  }

  .ticker-wrap::before, .ticker-wrap::after {
    content: "";
    position: absolute;
    top: 0; bottom: 0;
    width: 40px;
    z-index: 2;
    pointer-events: none;
  }
  .ticker-wrap::before { left: 0; background: linear-gradient(90deg, var(--panel), transparent); }
  .ticker-wrap::after  { right: 0; background: linear-gradient(270deg, var(--panel), transparent); }

  .ticker-track {
    display: inline-flex;
    align-items: center;
    padding: 9px 0;
    animation: scroll-left 40s linear infinite;
  }

  .ticker-track:hover { animation-play-state: paused; }

  .ticker-track.static {
    animation: none;
    width: 100%;
    justify-content: center;
  }

  @keyframes scroll-left {
    from { transform: translateX(0); }
    to   { transform: translateX(-50%); }
  }

  .ticker-item {
    display: inline-flex;
    align-items: baseline;
    gap: 7px;
    font-family: var(--mono);
    font-size: 12px;
    padding: 0 22px;
    border-right: 1px solid var(--hairline);
    color: var(--text-dim);
  }

  .ticker-item .side { font-weight: 700; letter-spacing: 0.04em; }
  .ticker-item .side.yes { color: var(--positive); }
  .ticker-item .side.no { color: var(--negative); }
  .ticker-item .pnl.positive { color: var(--positive); }
  .ticker-item .pnl.negative { color: var(--negative); }
  .ticker-empty { font-family: var(--mono); font-size: 12px; color: var(--text-faint); padding: 9px 22px; }

  /* ---------- Layout ---------- */
  main {
    max-width: 1180px;
    margin: 0 auto;
    padding: 28px;
  }

  .stats-row {
    display: grid;
    grid-template-columns: repeat(5, 1fr);
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
    .stats-row { grid-template-columns: repeat(2, 1fr); }
    .columns { grid-template-columns: 1fr; }
    .position-grid { grid-template-columns: 1fr 1fr; }
  }

  @media (prefers-reduced-motion: reduce) {
    .ticker-track { animation: none; overflow-x: auto; }
    .dot.online { animation: none; }
  }
</style>
</head>
<body>

<header>
  <div class="wordmark">MYSTIC<span>-</span>BOT<span> 1.0</span></div>
  <div class="shadow-banner">🌗 SHADOW MODE — SIMULATED, NO REAL MONEY</div>
  <div class="status-pill">
    <span id="status-dot" class="dot offline"></span>
    <span id="status-text">CONNECTING</span>
    <span>·</span>
    <span id="server-time" class="server-time">--:--:--</span>
  </div>
</header>

<div class="ticker-wrap">
  <div id="ticker-track" class="ticker-track static">
    <span class="ticker-empty">Waiting for trade history&hellip;</span>
  </div>
</div>

<main>

  <div class="stats-row">
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

  function renderTicker(trades) {
    const track = document.getElementById("ticker-track");
    if (!trades || trades.length === 0) {
      track.className = "ticker-track static";
      track.innerHTML = '<span class="ticker-empty">Waiting for trade history&hellip;</span>';
      return;
    }
    track.className = "ticker-track";
    const items = trades.slice(0, 15).map(t => {
      const side = (t.side || "").toLowerCase();
      const pnl = t.pnl ?? 0;
      return `<span class="ticker-item">
        <span>${t.ticker || ""}</span>
        <span class="side ${side}">${side.toUpperCase()}</span>
        <span class="pnl ${pnlClass(pnl)}">${fmtMoney(pnl)}</span>
      </span>`;
    }).join("");
    // duplicate the sequence so the CSS loop (translateX -50%) is seamless
    track.innerHTML = items + items;
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
            <div class="field-value">${m.watching_97c ? '<span class="badge yes">YES</span>' : '<span class="dim">no</span>'}</div>
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
      document.getElementById("today-pnl").textContent = fmtMoney(data.stats.today_pnl);
      document.getElementById("today-pnl").className = "stat-value " + pnlClass(data.stats.today_pnl);
      document.getElementById("all-pnl").textContent = fmtMoney(data.stats.all_pnl);
      document.getElementById("all-pnl").className = "stat-value " + pnlClass(data.stats.all_pnl);
      document.getElementById("total-trades").textContent = data.stats.total_trades;
      document.getElementById("win-rate").textContent = data.stats.total_trades > 0 ? data.stats.win_rate + "%" : "\u2014";

      renderPosition(data.current_trade);
      renderTrades(data.recent_trades);
      renderTicker(data.recent_trades);
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
    app.run(host="0.0.0.0", port=5001, debug=False)
