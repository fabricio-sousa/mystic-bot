"""
dashboard.py — single-file Flask dashboard for bot.py.

Everything lives in this one file (backend + HTML + CSS + JS) on purpose —
no templates/ or static/ folders to lose when copying files around.

It's read-only and decoupled from bot.py: never imports it (that would
trigger bot.py's top-level API-key loading and Kalshi client setup) and
never calls the Kalshi API. It just polls the three files bot.py already
writes:

    state.json   -> strikes, paper_balance, current_trade
    trades.json  -> full trade history (settlements, stops, overrides)
    log.txt      -> raw log tail

Run it from the same folder as bot.py:

    python3 dashboard.py

Or point it elsewhere:

    BOT_DIR=/path/to/your/bot python3 dashboard.py

Then open http://127.0.0.1:5000
"""

import os
import json
import datetime
import traceback
from flask import Flask, jsonify, Response

# ====================== CONFIG ======================
# Folder containing bot.py's state.json / trades.json / log.txt.
# Defaults to wherever this script lives — drop it right next to bot.py.
BOT_DIR = os.environ.get("BOT_DIR") or os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BOT_DIR, "state.json")
TRADES_FILE = os.path.join(BOT_DIR, "trades.json")
LOG_FILE = os.path.join(BOT_DIR, "log.txt")

# Mirrors bot.py's STRIKE_LIMIT — bot.py doesn't persist this, so keep in sync
# by hand or override with an env var if you change it there.
STRIKE_LIMIT = int(os.environ.get("STRIKE_LIMIT", "3"))
PAPER_START_BALANCE = float(os.environ.get("PAPER_START_BALANCE", "1000.0"))
DEBUG = os.environ.get("DASHBOARD_DEBUG", "").lower() in ("1", "true", "yes")

LOG_TAIL_LINES = 60
TRADES_LIMIT = 200
ET = datetime.timezone(datetime.timedelta(hours=-5))  # fallback; real tz below

try:
    import pytz
    ET = pytz.timezone("US/Eastern")
except ImportError:
    pytz = None

app = Flask(__name__)


# ====================== FILE HELPERS ======================
def _read_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _tail_lines(path, n):
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            block = 4096
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def _now_et():
    return datetime.datetime.now(ET)


def _parse_ts(ts_str):
    """Trade timestamps are written as '%Y-%m-%d %H:%M:%S' (naive, ET)."""
    try:
        return datetime.datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


# ====================== DATA ASSEMBLY ======================
def build_snapshot():
    state = _read_json(STATE_FILE, {})
    if not isinstance(state, dict):
        state = {}
    trades = _read_json(TRADES_FILE, [])
    if not isinstance(trades, list):
        trades = []

    # Prefer the explicit mode the bot now writes to state ("PAPER"/"DEMO"/"LIVE").
    # Fall back to the old heuristic (presence of paper_balance) only for state files
    # written by an older bot.py that didn't record a mode.
    state_mode = state.get("mode")
    if state_mode in ("PAPER", "DEMO", "LIVE"):
        is_paper = (state_mode == "PAPER")
        mode_label = state_mode
    else:
        is_paper = "paper_balance" in state
        mode_label = "PAPER" if is_paper else "LIVE"
    balance = state.get("paper_balance", PAPER_START_BALANCE) if is_paper else None
    strikes = state.get("strikes", 0)
    current_trade = state.get("current_trade")

    today = _now_et().date()
    wins = losses = 0
    total_pnl = 0.0
    today_pnl = 0.0
    for t in trades:
        if not isinstance(t, dict):
            continue
        pnl = t.get("pnl", 0) or 0
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
        dt = _parse_ts(t.get("timestamp", ""))
        if dt and dt.date() == today:
            today_pnl += pnl

    decided = wins + losses
    win_rate = (wins / decided * 100.0) if decided else None

    recent = [t for t in reversed(trades) if isinstance(t, dict)][:TRADES_LIMIT]

    # cumulative pnl series (chronological) for the sparkline
    running = 0.0
    spark = []
    for t in trades[-120:]:
        if not isinstance(t, dict):
            continue
        running += t.get("pnl", 0) or 0
        spark.append(round(running, 2))

    return {
        "mode": mode_label,
        "balance": round(balance, 2) if balance is not None else None,
        "strikes": strikes,
        "strike_limit": STRIKE_LIMIT,
        "current_trade": current_trade,
        "trades": recent,
        "stats": {
            "wins": wins,
            "losses": losses,
            "win_rate": round(win_rate, 1) if win_rate is not None else None,
            "total_pnl": round(total_pnl, 2),
            "today_pnl": round(today_pnl, 2),
            "trade_count": len(trades),
        },
        "spark": spark,
        "log_tail": _tail_lines(LOG_FILE, LOG_TAIL_LINES),
        "updated_at": _now_et().strftime("%H:%M:%S"),
        "bot_dir": BOT_DIR,
    }


# ====================== PAGE (HTML + CSS + JS, all inline) ======================
PAGE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Magick Bot — Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,500;8..60,600&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #0a0a0c;
  --surface: #111113;
  --surface-alt: #18181b;
  --border: #27272a;
  --border-soft: #3f3f46;
  --text: #f4f4f5;
  --text-muted: #a1a1aa;
  --text-faint: #71717a;
  --accent: #f97316;
  --accent-soft: #431407;
  --accent-dim: #fdba74;
  --positive: #22c55e;
  --positive-soft: #052e16;
  --negative: #ef4444;
  --negative-soft: #450a0a;
  --radius-lg: 16px;
  --shadow-card: 0 10px 15px -3px rgb(0 0 0 / 0.1), 0 4px 6px -4px rgb(0 0 0 / 0.1);
  --font-display: 'Source Serif 4', Georgia, serif;
  --font-ui: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  --font-mono: 'JetBrains Mono', ui-monospace, 'SFMono-Regular', Menlo, monospace;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg); color: var(--text);
  font-family: var(--font-ui); font-size: 15px;
  -webkit-font-smoothing: antialiased;
}
a { color: var(--accent); }
::selection { background: #f97316; color: #0a0a0c; }
:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 18px 32px; border-bottom: 1px solid var(--border-soft);
  background: var(--surface); position: sticky; top: 0; z-index: 10;
}
.topbar-left { display: flex; align-items: center; gap: 12px; }
.wordmark { font-family: var(--font-display); font-size: 19px; font-weight: 600; letter-spacing: -0.01em; }
.mode-pill {
  font-family: var(--font-mono); font-size: 11px; font-weight: 500; letter-spacing: 0.04em;
  padding: 3px 9px; border-radius: 100px; background: var(--accent-soft); color: var(--accent-dim);
  text-transform: uppercase;
}
.mode-pill.live { background: var(--negative-soft); color: var(--negative); }
.topbar-right { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); font-family: var(--font-mono); }
.live-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--positive); display: inline-block; animation: pulse 2.4s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) { .live-dot { animation: none; } }
@keyframes pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.35); }
  50% { opacity: 0.55; box-shadow: 0 0 0 4px rgba(34, 197, 94, 0); }
}
.live-dot.stale { background: var(--negative); animation: none; }

.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 32px 60px; display: flex; flex-direction: column; gap: 20px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius-lg); box-shadow: var(--shadow-card); }
.card-head { display: flex; align-items: baseline; justify-content: space-between; padding: 18px 22px 0; }
.card-head h2 { font-family: var(--font-ui); font-size: 14px; font-weight: 600; margin: 0; }
.card-head-sub { font-size: 12px; color: var(--text-faint); font-family: var(--font-mono); }

.stat-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; }
.stat-card { padding: 20px 22px; }
.stat-label { font-size: 12px; color: var(--text-muted); font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
.stat-value { font-family: var(--font-display); font-size: 30px; font-weight: 500; margin-top: 6px; letter-spacing: -0.01em; font-variant-numeric: tabular-nums; }
.stat-value.positive { color: var(--positive); }
.stat-value.negative { color: var(--negative); }
.stat-sub { font-size: 12px; color: var(--text-faint); margin-top: 4px; font-family: var(--font-mono); }
.stat-sub .positive { color: var(--positive); }
.stat-sub .negative { color: var(--negative); }

.row-2 { display: grid; grid-template-columns: 1.1fr 1fr; gap: 20px; align-items: stretch; }

.position-card #position-body { padding: 18px 22px 22px; }
.position-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 20px; }
.position-field { display: flex; flex-direction: column; gap: 2px; }
.position-field .k { font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: 0.04em; }
.position-field .v { font-family: var(--font-mono); font-size: 15px; }
.side-tag { display: inline-block; font-family: var(--font-mono); font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 100px; letter-spacing: 0.03em; text-transform: uppercase; }
.side-tag.yes { background: var(--positive-soft); color: var(--positive); }
.side-tag.no { background: var(--negative-soft); color: var(--negative); }
.armed-tag { display: inline-flex; align-items: center; gap: 5px; font-family: var(--font-mono); font-size: 11px; color: var(--accent-dim); margin-top: 10px; }
.armed-tag::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); }

.spark-card { display: flex; flex-direction: column; }
.sparkline { width: 100%; height: 100px; padding: 4px 22px 20px; flex: 1; }

.table-scroll { overflow-x: auto; padding: 14px 22px 20px; }
table { width: 100%; border-collapse: collapse; font-size: 13.5px; }
thead th { text-align: left; font-size: 11px; color: var(--text-faint); text-transform: uppercase; letter-spacing: 0.04em; font-weight: 500; padding: 8px 10px; border-bottom: 1px solid var(--border-soft); }
thead th.num, td.num { text-align: right; }
tbody td { padding: 9px 10px; border-bottom: 1px solid var(--border-soft); font-family: var(--font-mono); white-space: nowrap; }
tbody tr:last-child td { border-bottom: none; }
tbody tr:hover td { background: var(--surface-alt); }
.pnl-cell.positive { color: var(--positive); font-weight: 500; }
.pnl-cell.negative { color: var(--negative); font-weight: 500; }
.type-tag { font-size: 11px; padding: 2px 7px; border-radius: 100px; background: var(--surface-alt); color: var(--text-muted); border: 1px solid var(--border-soft); }

.log-tail { margin: 0; padding: 14px 22px 20px; font-family: var(--font-mono); font-size: 12px; line-height: 1.6; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; max-height: 280px; overflow-y: auto; }

.empty-state { color: var(--text-faint); font-size: 13px; padding: 20px 0; text-align: center; }
td.empty-state { padding: 24px 0; }
.foot { text-align: center; font-family: var(--font-mono); font-size: 11px; color: var(--text-faint); padding: 10px 0 30px; }

@media (max-width: 860px) {
  .stat-grid { grid-template-columns: repeat(2, 1fr); }
  .row-2 { grid-template-columns: 1fr; }
}
@media (max-width: 520px) {
  .stat-grid { grid-template-columns: 1fr 1fr; }
  .topbar { padding: 14px 18px; }
  .wrap { padding: 20px 16px 40px; }
  .stat-value { font-size: 24px; }
}
</style>
</head>
<body>

  <header class="topbar">
    <div class="topbar-left">
      <span class="wordmark">&#10022; Magick Bot</span>
      <span class="mode-pill" id="mode-pill">&mdash;</span>
    </div>
    <div class="topbar-right">
      <span class="live-dot" id="live-dot"></span>
      <span>Updated <span id="updated-at">&mdash;</span></span>
    </div>
  </header>

  <main class="wrap">

    <section class="stat-grid">
      <div class="card stat-card">
        <div class="stat-label">Balance</div>
        <div class="stat-value" id="stat-balance">&mdash;</div>
        <div class="stat-sub" id="stat-balance-sub">&nbsp;</div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Today&rsquo;s P&amp;L</div>
        <div class="stat-value" id="stat-today-pnl">&mdash;</div>
        <div class="stat-sub">all-time <span id="stat-total-pnl">&mdash;</span></div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Win rate</div>
        <div class="stat-value" id="stat-winrate">&mdash;</div>
        <div class="stat-sub"><span id="stat-record">&mdash;</span></div>
      </div>
      <div class="card stat-card">
        <div class="stat-label">Strikes</div>
        <div class="stat-value" id="stat-strikes">&mdash;</div>
        <div class="stat-sub">of <span id="stat-strike-limit">&mdash;</span> allowed</div>
      </div>
    </section>

    <section class="row-2">
      <div class="card position-card">
        <div class="card-head"><h2>Position</h2></div>
        <div id="position-body">
          <div class="empty-state">No open position &mdash; waiting for a signal.</div>
        </div>
      </div>

      <div class="card spark-card">
        <div class="card-head">
          <h2>Cumulative P&amp;L</h2>
          <span class="card-head-sub">last 120 trades</span>
        </div>
        <svg id="spark-svg" viewBox="0 0 100 40" preserveAspectRatio="none" class="sparkline"></svg>
      </div>
    </section>

    <section class="card table-card">
      <div class="card-head">
        <h2>Trade history</h2>
        <span class="card-head-sub" id="trade-count-label">0 trades</span>
      </div>
      <div class="table-scroll">
        <table>
          <thead>
            <tr><th>Time</th><th>Ticker</th><th>Side</th><th>Type</th><th class="num">P&amp;L</th></tr>
          </thead>
          <tbody id="trades-body">
            <tr><td colspan="5" class="empty-state">No trades recorded yet.</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <section class="card log-card">
      <div class="card-head">
        <h2>Log</h2>
        <span class="card-head-sub">log.txt</span>
      </div>
      <pre id="log-tail" class="log-tail">waiting for log data&hellip;</pre>
    </section>

  </main>

  <footer class="foot"><span id="bot-dir-label">&mdash;</span></footer>

<script>
const POLL_MS = 3000;
const STALE_AFTER_MS = 15000;
let lastSuccessAt = 0;

function fmtMoney(n, opts = {}) {
  if (n === null || n === undefined) return "\u2014";
  const sign = opts.forceSign && n > 0 ? "+" : "";
  return `${sign}$${n.toFixed(2)}`;
}
function pnlClass(n) { if (n > 0) return "positive"; if (n < 0) return "negative"; return ""; }
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function renderStats(data) {
  const modePill = document.getElementById("mode-pill");
  modePill.textContent = data.mode;
  // Highlight any real-order mode (LIVE or DEMO sandbox) so it's visually distinct from PAPER.
  modePill.classList.toggle("live", data.mode === "LIVE" || data.mode === "DEMO");

  const balanceEl = document.getElementById("stat-balance");
  const balanceSub = document.getElementById("stat-balance-sub");
  if (data.balance === null) {
    balanceEl.textContent = data.mode === "DEMO" ? "demo account" : "live account";
    balanceSub.textContent = data.mode === "DEMO" ? "sandbox balance in Kalshi demo" : "balance shown in Kalshi";
  } else {
    balanceEl.textContent = fmtMoney(data.balance);
    balanceSub.textContent = "paper trading";
  }

  const s = data.stats;
  const todayEl = document.getElementById("stat-today-pnl");
  todayEl.textContent = fmtMoney(s.today_pnl, { forceSign: true });
  todayEl.className = "stat-value " + pnlClass(s.today_pnl);

  const totalEl = document.getElementById("stat-total-pnl");
  totalEl.textContent = fmtMoney(s.total_pnl, { forceSign: true });
  totalEl.className = pnlClass(s.total_pnl);

  const wrEl = document.getElementById("stat-winrate");
  wrEl.textContent = s.win_rate === null ? "\u2014" : `${s.win_rate}%`;
  document.getElementById("stat-record").textContent = s.trade_count ? `${s.wins}W / ${s.losses}L` : "no trades yet";

  document.getElementById("stat-strikes").textContent = data.strikes;
  document.getElementById("stat-strike-limit").textContent = data.strike_limit;
  document.getElementById("stat-strikes").className = "stat-value " + (data.strikes >= data.strike_limit ? "negative" : "");
}

function renderPosition(trade) {
  const body = document.getElementById("position-body");
  body.innerHTML = "";
  if (!trade) {
    body.appendChild(el("div", "empty-state", "No open position \u2014 waiting for a signal."));
    return;
  }
  const grid = el("div", "position-grid");
  const fields = [
    ["Ticker", trade.ticker],
    ["Side", null],
    ["Entry price", trade.entry_price_cents != null ? `${trade.entry_price_cents}c` : "\u2014"],
    ["Contracts", trade.count],
  ];
  fields.forEach(([label, value]) => {
    const f = el("div", "position-field");
    f.appendChild(el("div", "k", label));
    if (label === "Side") {
      const v = el("div", "v");
      v.appendChild(el("span", `side-tag ${trade.side}`, trade.side));
      f.appendChild(v);
    } else {
      f.appendChild(el("div", "v", value ?? "\u2014"));
    }
    grid.appendChild(f);
  });
  body.appendChild(grid);
  if (trade.stop_armed) body.appendChild(el("div", "armed-tag", "Stop armed \u2014 monitoring for exit"));
}

function renderTrades(trades) {
  const tbody = document.getElementById("trades-body");
  document.getElementById("trade-count-label").textContent = `${trades.length} shown`;
  tbody.innerHTML = "";
  if (!trades.length) {
    const tr = el("tr");
    const td = el("td", "empty-state", "No trades recorded yet.");
    td.colSpan = 5;
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }
  trades.forEach((t) => {
    const tr = el("tr");
    const time = (t.timestamp || "").split(" ")[1] || t.timestamp || "\u2014";
    tr.appendChild(el("td", "", time));
    tr.appendChild(el("td", "", t.ticker || "\u2014"));
    const sideTd = el("td");
    if (t.side) sideTd.appendChild(el("span", `side-tag ${t.side}`, t.side));
    tr.appendChild(sideTd);
    const typeTd = el("td");
    typeTd.appendChild(el("span", "type-tag", t.type || "\u2014"));
    tr.appendChild(typeTd);
    const pnl = t.pnl || 0;
    tr.appendChild(el("td", `num pnl-cell ${pnlClass(pnl)}`, fmtMoney(pnl, { forceSign: true })));
    tbody.appendChild(tr);
  });
}

function renderSparkline(series) {
  const svg = document.getElementById("spark-svg");
  svg.innerHTML = "";
  if (!series || series.length < 2) { svg.setAttribute("viewBox", "0 0 100 40"); return; }
  const w = 100, h = 40, pad = 3;
  const min = Math.min(...series, 0);
  const max = Math.max(...series, 0);
  const range = max - min || 1;
  const points = series.map((v, i) => {
    const x = pad + (i / (series.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    return [x, y];
  });
  const zeroY = h - pad - ((0 - min) / range) * (h - pad * 2);
  const zeroLine = document.createElementNS("http://www.w3.org/2000/svg", "line");
  zeroLine.setAttribute("x1", 0); zeroLine.setAttribute("x2", w);
  zeroLine.setAttribute("y1", zeroY); zeroLine.setAttribute("y2", zeroY);
  zeroLine.setAttribute("stroke", "#27272a"); zeroLine.setAttribute("stroke-width", "0.5");
  svg.appendChild(zeroLine);
  const last = series[series.length - 1];
  const color = last >= 0 ? "#22c55e" : "#ef4444";
  const path = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  path.setAttribute("points", points.map((p) => p.join(",")).join(" "));
  path.setAttribute("fill", "none"); path.setAttribute("stroke", color);
  path.setAttribute("stroke-width", "1.4"); path.setAttribute("stroke-linecap", "round");
  path.setAttribute("stroke-linejoin", "round"); path.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(path);
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  const [lx, ly] = points[points.length - 1];
  dot.setAttribute("cx", lx); dot.setAttribute("cy", ly); dot.setAttribute("r", "1.6"); dot.setAttribute("fill", color);
  svg.appendChild(dot);
}

function renderLog(lines) {
  const pre = document.getElementById("log-tail");
  pre.textContent = lines && lines.length ? lines.join("\n") : "log.txt is empty.";
  pre.scrollTop = pre.scrollHeight;
}

function renderAll(data) {
  renderStats(data);
  renderPosition(data.current_trade);
  renderTrades(data.trades);
  renderSparkline(data.spark);
  renderLog(data.log_tail);
  document.getElementById("updated-at").textContent = data.updated_at;
  document.getElementById("bot-dir-label").textContent = data.bot_dir;
  lastSuccessAt = Date.now();
  document.getElementById("live-dot").classList.remove("stale");
}

function checkStale() {
  const dot = document.getElementById("live-dot");
  if (lastSuccessAt && Date.now() - lastSuccessAt > STALE_AFTER_MS) dot.classList.add("stale");
}

async function poll() {
  try {
    const res = await fetch("/api/data", { cache: "no-store" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    renderAll(await res.json());
  } catch (e) {
    console.error("Dashboard poll failed:", e);
  } finally {
    checkStale();
    setTimeout(poll, POLL_MS);
  }
}
poll();
</script>
</body>
</html>
"""


# ====================== ROUTES ======================
@app.route("/")
def index():
    return Response(PAGE, mimetype="text/html")


@app.route("/api/data")
def api_data():
    try:
        return jsonify(build_snapshot())
    except Exception as e:
        app.logger.exception("Failed to build dashboard snapshot")
        payload = {"error": str(e)}
        if DEBUG:
            payload["traceback"] = traceback.format_exc()
        return jsonify(payload), 500


if __name__ == "__main__":
    print(f"Magick Bot Dashboard — reading from {BOT_DIR}")
    if not os.path.isdir(BOT_DIR):
        print(f"WARNING: BOT_DIR does not exist: {BOT_DIR}")
    print("Open http://127.0.0.1:5003")
    print("(set DASHBOARD_DEBUG=1 for tracebacks in API errors)")
    app.run(host="127.0.0.1", port=5003, debug=DEBUG)
