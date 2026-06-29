"""
Mystic-Bot dashboard.

Run:
    python dashboard.py
then open http://127.0.0.1:5000 in a browser.

A single-page, auto-refreshing view of the account, holdings, each name's drift
from its 10% target, and recent bot activity. Read-only: it never trades.
"""

from __future__ import annotations

import csv
from flask import Flask, jsonify

import config
from broker import Broker

app = Flask(__name__)
_broker = None


def broker() -> Broker:
    global _broker
    if _broker is None:
        _broker = Broker()
    return _broker


def read_activity(limit: int = 15) -> list[dict]:
    if not config.ACTIVITY_LOG.exists():
        return []
    with config.ACTIVITY_LOG.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return list(reversed(rows))[:limit]


@app.route("/api/state")
def api_state():
    acct = broker().get_account()
    positions = broker().get_positions()
    invested = sum(p.market_value for p in positions.values())

    holdings = []
    drifts = []
    for sym in config.TARGET_WEIGHTS:
        target_pct = config.TARGET_WEIGHTS[sym] * 100
        p = positions.get(sym)
        mv = p.market_value if p else 0.0
        weight = (mv / invested * 100) if invested else 0.0
        drift = weight - target_pct
        drifts.append(abs(drift))
        holdings.append({
            "symbol": sym,
            "name": config.DISPLAY_NAMES[sym],
            "qty": p.qty if p else 0.0,
            "avg_price": p.avg_entry_price if p else 0.0,
            "price": p.current_price if p else 0.0,
            "value": mv,
            "weight": weight,
            "target": target_pct,
            "drift": drift,
            "upl": p.unrealized_pl if p else 0.0,
            "uplpc": (p.unrealized_plpc * 100) if p else 0.0,
        })
    holdings.sort(key=lambda h: h["drift"])  # most under-target first

    total_upl = sum(h["upl"] for h in holdings)
    cost = invested - total_upl
    return jsonify({
        "mode": "PAPER" if config.PAPER else "LIVE",
        "market_open": acct.market_open,
        "equity": acct.equity,
        "cash": acct.cash,
        "invested": invested,
        "today_change": acct.equity - acct.last_equity,
        "today_change_pct": ((acct.equity - acct.last_equity) / acct.last_equity * 100)
                            if acct.last_equity else 0.0,
        "unrealized_pl": total_upl,
        "unrealized_plpc": (total_upl / cost * 100) if cost else 0.0,
        "avg_drift": (sum(drifts) / len(drifts)) if drifts else 0.0,
        "holdings": holdings,
        "activity": read_activity(),
    })


@app.route("/")
def index():
    return PAGE


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mystic-Bot</title>
<style>
  :root{
    --void:#0A0C12; --panel:#12161F; --panel2:#0E121A; --hair:#1F2633;
    --ink:#E8EBF2; --mist:#6B7488; --aurora:#8B7BF0; --aurora-dim:#3a356b;
    --gain:#46C28A; --loss:#E06A82;
  }
  *{box-sizing:border-box}
  body{
    margin:0; background:
      radial-gradient(900px 500px at 80% -10%, #181433 0%, transparent 60%),
      var(--void);
    color:var(--ink);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;
    -webkit-font-smoothing:antialiased; padding:32px 20px 80px;
  }
  .mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
        font-variant-numeric:tabular-nums; letter-spacing:-.02em}
  .wrap{max-width:920px; margin:0 auto}
  .eyebrow{font-size:11px; letter-spacing:.28em; text-transform:uppercase;
           color:var(--mist)}
  /* header */
  header{display:flex; align-items:baseline; justify-content:space-between;
         border-bottom:1px solid var(--hair); padding-bottom:18px; margin-bottom:28px}
  .brand{display:flex; align-items:baseline; gap:14px}
  .brand h1{font-size:20px; margin:0; font-weight:600; letter-spacing:.04em}
  .brand h1 b{color:var(--aurora); font-weight:600}
  .tag{font-size:10px; letter-spacing:.2em; color:var(--mist);
       border:1px solid var(--hair); padding:3px 8px; border-radius:99px}
  .status{display:flex; align-items:center; gap:8px; font-size:12px; color:var(--mist)}
  .dot{width:7px; height:7px; border-radius:50%; background:var(--mist)}
  .dot.open{background:var(--gain); box-shadow:0 0 8px var(--gain)}
  /* hero */
  .hero{display:flex; align-items:flex-end; justify-content:space-between;
        gap:20px; margin-bottom:8px; flex-wrap:wrap}
  .equity{font-size:52px; font-weight:600; line-height:1}
  .equity .cents{color:var(--mist); font-size:30px}
  .today{font-size:15px}
  .stats{display:grid; grid-template-columns:repeat(4,1fr); gap:1px;
         background:var(--hair); border:1px solid var(--hair); border-radius:12px;
         overflow:hidden; margin:26px 0 36px}
  .stat{background:var(--panel); padding:16px 18px}
  .stat .k{font-size:11px; letter-spacing:.14em; text-transform:uppercase;
           color:var(--mist); margin-bottom:8px}
  .stat .v{font-size:19px}
  /* table */
  .sec{display:flex; align-items:center; justify-content:space-between; margin:0 0 14px}
  table{width:100%; border-collapse:collapse}
  th{font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:var(--mist);
     text-align:right; font-weight:500; padding:0 0 12px}
  th:first-child{text-align:left}
  td{padding:13px 0; border-top:1px solid var(--hair); text-align:right; font-size:14px}
  td:first-child{text-align:left}
  .sym{font-weight:600; letter-spacing:.03em}
  .nm{color:var(--mist); font-size:11px; margin-top:2px}
  .pos{color:var(--gain)} .neg{color:var(--loss)} .flat{color:var(--mist)}
  /* the signature: balance meter */
  .meter{width:120px; height:26px; position:relative; margin-left:auto}
  .track{position:absolute; top:50%; left:0; right:0; height:4px; transform:translateY(-50%);
         background:var(--panel2); border-radius:2px; border:1px solid var(--hair)}
  .center{position:absolute; top:3px; bottom:3px; left:50%; width:1px;
          background:var(--mist); opacity:.7}
  .fill{position:absolute; top:50%; height:8px; transform:translateY(-50%);
        border-radius:2px; background:var(--aurora); transition:width .5s ease, left .5s ease}
  .drift{font-size:11px; min-width:46px; display:inline-block}
  /* activity */
  .act{margin-top:42px}
  .act ul{list-style:none; margin:14px 0 0; padding:0}
  .act li{display:flex; gap:14px; padding:9px 0; border-top:1px solid var(--hair);
          font-size:13px; color:var(--ink)}
  .act .t{color:var(--mist); white-space:nowrap}
  .act .empty{color:var(--mist); font-style:italic; padding-top:14px}
  .foot{margin-top:40px; font-size:11px; color:var(--mist); text-align:center}
  @media(max-width:620px){
    .stats{grid-template-columns:repeat(2,1fr)}
    .equity{font-size:40px}
    .meter{width:78px}
    .hide-sm{display:none}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="brand">
      <h1>MYSTIC<b>·</b>BOT</h1>
      <span class="tag" id="mode">—</span>
    </div>
    <div class="status"><span class="dot" id="dot"></span><span id="mkt">connecting</span></div>
  </header>

  <div class="hero">
    <div>
      <div class="eyebrow" style="margin-bottom:10px">Portfolio value</div>
      <div class="equity mono" id="equity">$—</div>
    </div>
    <div class="today mono" id="today">—</div>
  </div>

  <div class="stats">
    <div class="stat"><div class="k">Cash idle</div><div class="v mono" id="cash">—</div></div>
    <div class="stat"><div class="k">Invested</div><div class="v mono" id="invested">—</div></div>
    <div class="stat"><div class="k">Unrealized P/L</div><div class="v mono" id="upl">—</div></div>
    <div class="stat"><div class="k">Avg drift</div><div class="v mono" id="drift">—</div></div>
  </div>

  <div class="sec"><div class="eyebrow">Holdings · target 10% each</div>
    <div class="eyebrow hide-sm">sorted by who needs buying</div></div>
  <table>
    <thead><tr>
      <th>Asset</th><th>Value</th><th>Weight</th>
      <th class="hide-sm">Balance</th><th>Drift</th><th>P/L</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>

  <div class="act">
    <div class="eyebrow">Recent activity</div>
    <ul id="activity"></ul>
  </div>

  <div class="foot">Auto-refreshing every REFRESH s · read-only view · not investment advice</div>
</div>

<script>
const $ = id => document.getElementById(id);
const usd = n => (n<0?"-":"")+"$"+Math.abs(n).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
const cls = n => n>0?"pos":(n<0?"neg":"flat");

function splitCents(v){
  const s = usd(v); const i = s.lastIndexOf(".");
  return `${s.slice(0,i)}<span class="cents">${s.slice(i)}</span>`;
}

// map a drift in percentage points to a meter; clamp at +/- 6pp
function meter(driftPP){
  const max = 6;
  const d = Math.max(-max, Math.min(max, driftPP));
  const half = 50; // percent of track from center to edge
  const w = Math.abs(d)/max * half;
  const left = d < 0 ? (half - w) : half;
  const near = Math.abs(driftPP) < 1.0;
  const color = near ? "var(--aurora)" : (driftPP < 0 ? "var(--gain)" : "var(--loss)");
  return `<div class="meter"><div class="track"></div>
    <div class="fill" style="left:${left}%;width:${w}%;background:${color}"></div>
    <div class="center"></div></div>`;
}

async function load(){
  let d;
  try{ d = await (await fetch("/api/state")).json(); }
  catch(e){ $("mkt").textContent = "connection lost"; return; }

  $("mode").textContent = d.mode;
  $("dot").className = "dot" + (d.market_open ? " open" : "");
  $("mkt").textContent = d.market_open ? "Market open" : "Market closed";
  $("equity").innerHTML = splitCents(d.equity);

  const t = d.today_change;
  $("today").innerHTML = `<span class="${cls(t)}">${t>=0?"▲":"▼"} ${usd(Math.abs(t))} `
    + `(${t>=0?"+":""}${d.today_change_pct.toFixed(2)}%)</span><br>`
    + `<span class="flat" style="font-size:11px;letter-spacing:.1em">TODAY</span>`;

  $("cash").textContent = usd(d.cash);
  $("invested").textContent = usd(d.invested);
  const u = d.unrealized_pl;
  $("upl").innerHTML = `<span class="${cls(u)}">${u>=0?"+":"−"}${usd(Math.abs(u))} `
    + `(${u>=0?"+":""}${d.unrealized_plpc.toFixed(2)}%)</span>`;
  $("drift").textContent = "±" + d.avg_drift.toFixed(2) + "pp";

  $("rows").innerHTML = d.holdings.map(h => {
    const dr = h.drift;
    const drStr = (dr>=0?"+":"") + dr.toFixed(2) + "pp";
    const plStr = h.value>0 ? `${h.uplpc>=0?"+":""}${h.uplpc.toFixed(1)}%` : "—";
    return `<tr>
      <td><div class="sym">${h.symbol}</div><div class="nm">${h.name}</div></td>
      <td class="mono">${usd(h.value)}</td>
      <td class="mono">${h.weight.toFixed(1)}%</td>
      <td class="hide-sm">${meter(dr)}</td>
      <td class="mono drift ${dr<-0.05?'pos':(dr>0.05?'neg':'flat')}">${drStr}</td>
      <td class="mono ${cls(h.uplpc)}">${plStr}</td>
    </tr>`;
  }).join("");

  const act = $("activity");
  if(!d.activity.length){
    act.innerHTML = `<li class="empty">No orders yet. Deposit cash and run the bot to see buys here.</li>`;
  } else {
    act.innerHTML = d.activity.map(a =>
      `<li><span class="t mono">${a.timestamp}</span>
        <span>Bought <b>${usd(parseFloat(a.usd))}</b> of <b>${a.symbol}</b>
        <span class="flat">· ${a.status}</span></span></li>`).join("");
  }
}
load();
setInterval(load, REFRESH*1000);
</script>
</body>
</html>
"""
PAGE = PAGE.replace("REFRESH", str(config.DASHBOARD_REFRESH_SECONDS))


if __name__ == "__main__":
    print(f"Mystic-Bot dashboard -> http://{config.DASHBOARD_HOST}:{config.DASHBOARD_PORT}")
    app.run(host=config.DASHBOARD_HOST, port=config.DASHBOARD_PORT, debug=False)
