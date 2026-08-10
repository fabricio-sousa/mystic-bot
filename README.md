# Mystic-Bot

Automated trading bot for Kalshi's `KXBTC15M` series (15-minute Bitcoin up/down binary contracts), using a late-window "Done-Deal" entry confirmed by BTC momentum filters. Includes a read-only web dashboard for monitoring.

For the full history of how the current configuration was chosen — backtesting methodology, parameter sweeps, the forensic investigation into specific losing streaks, and the walk-forward validation — see **`Mystic-Bot.md`**. This file only covers running the bot itself.

## Strategy, in brief

Every 15-minute BTC window, the bot watches for the yes/no bid to be at or above `ENTRY_THRESHOLD` cents (currently 93¢) and no higher than `MAX_ENTRY_THRESHOLD` (97¢ — 98-99¢ touches are intentionally skipped), with 2-4 minutes left before close. If it does, three additional filters must all pass before entering:

- **Distance**: BTC has moved far enough from the window's opening price (threshold scales with time remaining)
- **Momentum**: the last 3 completed 1-minute candles are trending in the same direction
- **Wick**: no recent candle has a large opposing wick (a rejection signal)

One position at a time. A stop-loss exits early if the position's own bid drops `STOP_LOSS_THRESHOLD` (20%) below entry — widened to `HIGH_ENTRY_STOP_LOSS_PCT` (37.5%), floor-capped at a hard `HIGH_ENTRY_STOP_FLOOR_CENTS` (67¢), for entries at/above `HIGH_ENTRY_STOP_THRESHOLD` (97¢), and fully disabled inside the final `HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN` (1.75) minutes for entries at/above `HIGH_ENTRY_STOP_DISABLE_THRESHOLD` (96¢) — on top of the existing disable in everyone's final 30 seconds. Two more conditions have to hold before a stop is actually allowed to fire: the breach must persist for `STOP_CONFIRM_LOOPS` (3) consecutive ~1-second polls, and live BTC must have reversed at least `BTC_STOP_CONFIRM_FRACTION` (50%) of its original entry-time distance from the window's open price. Both exist to tell a genuine reversal apart from a pure Kalshi order-book flash spike. The bot halts entirely if cash falls to `SAFETY_FLOOR_PCT` of its highest balance ever reached, or three non-winning trades happen in a row with no win in between (`STRIKE_LIMIT`).

## Setup

```
pip install kalshi_python_sync requests pytz
pip install flask          # dashboard only
```

Place in the bot's folder:
- `apikey.txt` — your Kalshi API key ID
- `private.txt` — your Kalshi private key (PEM)

Windows-only: desktop alert sounds use `winsound`/`msvcrt` and are skipped automatically on other platforms.

## Running

```
python bot.py
```

Runs until stopped (Esc) or until a shutdown condition fires. Press `c` at any time to clear the current tracked position from `state.json` (manual override — use if you've closed a position by hand on Kalshi and the bot's state has drifted from reality).

### Shadow mode

```
python bot.py --shadow --shadow-cash 5000
```

Runs the exact same live market data, filters, and decision logic as real trading, but never places a real order — fills are simulated at the observed live price with Kalshi's real published taker fee applied, tracked in its own ledger (`shadow_cash`, inside `shadow_state.json`). Safe to run **at the same time**, in the same folder, as a real instance — it reads/writes an entirely separate set of files (`shadow_state.json`, `shadow_trades.json`, `shadow_log.txt`, `shadow_status.json`, `shadow_heartbeat.txt`), so the two never collide. `--shadow-cash` sets the starting simulated balance (default 5000) and only matters for shadow mode.

What this does and doesn't tell you: shadow mode trades against *today's* live market rather than a frozen historical backtest window, so it's the only way to check whether an edge found in past data still exists right now. It does **not** simulate real order-book depth, slippage, or how other participants might react to a real order — a simulated fill at the observed price is still an optimistic assumption, just a live one instead of a historical one. Treat divergence between shadow and real-money results (once the real bot has enough size to compare meaningfully) as a direct, measurable read on how much that gap actually costs.

```
python dashboard.py
```

Then open `http://localhost:5000`. Polls every 3 seconds. Read-only — never touches the Kalshi API or your credentials, only the files `bot.py` already writes. Safe to run alongside the bot at all times, including from a different machine pointed at a shared folder.

For a shadow session, use `shadowdashboard.py` instead — same tool, pointed at the `shadow_*` files, running on port 5001 so it can sit alongside the real dashboard without conflict:

```
python shadowdashboard.py
```

Then open `http://localhost:5001`. Visually distinct on purpose (violet accent, a persistent "SHADOW MODE — SIMULATED, NO REAL MONEY" badge in the header) so the two are never confusable at a glance.

## Configuration

All in the `CONFIG` section at the top of `bot.py`:

| Constant | Current value | Meaning |
|---|---|---|
| `ENTRY_THRESHOLD` | 93 | Minimum cents-scale bid to consider for entry |
| `MAX_ENTRY_THRESHOLD` | 97 | Maximum cents-scale bid to consider — 98-99¢ touches are skipped |
| `STOP_LOSS_THRESHOLD` | 0.20 | Exit if bid drops this fraction below entry (entries below `HIGH_ENTRY_STOP_THRESHOLD`) |
| `HIGH_ENTRY_STOP_THRESHOLD` | 97 | Entries at/above this price use the widened stop below instead of `STOP_LOSS_THRESHOLD` |
| `HIGH_ENTRY_STOP_LOSS_PCT` | 0.375 | Widened stop-loss fraction for those high-probability entries |
| `HIGH_ENTRY_STOP_FLOOR_CENTS` | 67 | The widened stop never triggers above this hard cents floor |
| `HIGH_ENTRY_STOP_DISABLE_THRESHOLD` | 96 | Entries at/above this price get the stop fully disabled near expiry |
| `HIGH_ENTRY_STOP_DISABLE_TIME_LEFT_MIN` | 1.75 | ...once `time_left` (minutes) drops below this |
| `STOP_CONFIRM_LOOPS` | 3 | Consecutive ~1s polls the stop condition must hold before it actually fires |
| `BTC_STOP_CONFIRM_FRACTION` | 0.5 | Live BTC must have reversed this fraction of its original entry-time distance from window-open before a stop is allowed to fire |
| `RISK_PCT` | 0.01 | Flat fraction of cash risked per trade (all trading windows) |
| `MAX_POSITION_DOLLARS` | 500.0 | Hard cap on position size regardless of `RISK_PCT × cash` |
| `SAFETY_FLOOR_PCT` | 0.75 | Bot halts if cash falls to this fraction of the highest balance ever reached (trailing, not a fixed dollar amount) |
| `STRIKE_LIMIT` | 3 | Bot halts after this many non-winning trades in a row |
| `MAX_SLIPPAGE` | 1 | Cents of slippage tolerance on order placement |
| `ORDER_POLL_SECONDS` | 3 | How long to wait for a fill before canceling the remainder |
| `WICK_MIN_PCT` | 0.00015 | Minimum wick size (as % of BTC price) to count as a rejection |

Trading windows (which hours of the week the bot is active at all) are set in `in_trading_window()`, just below the config block. Editing a window's hours doesn't require touching anything else — `RISK_PCT` applies uniformly across every window now (see `Mystic-Bot.md` for why this used to be a per-window value and isn't anymore).

**A shutdown (`SAFETY_FLOOR` or `STRIKE_LIMIT`) is not self-recovering.** The bot exits its loop entirely and stays down until you notice and restart it manually. There is currently no auto-restart or external alerting beyond the dashboard's `ONLINE`/`OFFLINE` indicator (driven by `heartbeat.txt`) — worth having some way of noticing this yourself, especially early on.

## Files it reads / writes

All in the bot's own folder, alongside `bot.py`:

| File | Written by | Purpose |
|---|---|---|
| `state.json` | bot | Current position + strike count. Source of truth across restarts. |
| `trades.json` | bot | Full trade history (append-only) |
| `log.txt` | bot | Human-readable event log (entries, exits, errors) — not a liveness signal, see below |
| `heartbeat.txt` | bot | Rewritten every loop pass; this is what the dashboard uses to determine online/offline, since `log.txt` only updates on specific events and can go quiet for long stretches even while the bot is running fine |
| `status.json` | bot | Live snapshot: market being watched, current bid prices, last filter evaluation, whether currently in a trading window |
| `apikey.txt` / `private.txt` | you | Kalshi credentials — never written by the bot, never read by the dashboard |

The dashboard only ever reads these files; it has no write access and makes no Kalshi API calls of its own.

## Safety notes

- The bot reconciles `state.json` against your actual live Kalshi positions on startup (`reconcile_state_with_positions`) — if it finds an untracked position, it logs a warning rather than guessing; check `log.txt` after any restart.
- `place_order` handles partial fills — the entry logic will size and log accordingly rather than assume a full fill.
- Stop-loss is explicitly disabled in a position's final 30 seconds (`time_left <= 0.5`), to avoid selling into a wide spread right before settlement — and, for high-probability entries specifically, disabled earlier still (see `HIGH_ENTRY_STOP_DISABLE_*` above).
- A stop-loss breach must persist for `STOP_CONFIRM_LOOPS` consecutive polls and be confirmed by live BTC price movement (`BTC_STOP_CONFIRM_FRACTION`) before it's allowed to fire — a single 1-second spike in Kalshi's own order book will not, on its own, trigger an exit.
