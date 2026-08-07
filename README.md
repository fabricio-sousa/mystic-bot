# Mystic-Bot

Automated trading bot for Kalshi's `KXBTC15M` series (15-minute Bitcoin up/down binary contracts), using a late-window "Done-Deal" entry confirmed by BTC momentum filters. Includes a read-only web dashboard for monitoring.

For the full history of how the current configuration was chosen — backtesting methodology, parameter sweeps, the forensic investigation into specific losing streaks, and the walk-forward validation — see **`Mystic-Bot.md`**. This file only covers running the bot itself.

## Strategy, in brief

Every 15-minute BTC window, the bot watches for the yes/no bid to touch exactly `ENTRY_THRESHOLD` cents (currently 93¢) with 2-4 minutes left before close. If it does, three additional filters must all pass before entering:

- **Distance**: BTC has moved far enough from the window's opening price (threshold scales with time remaining)
- **Momentum**: the last 3 completed 1-minute candles are trending in the same direction
- **Wick**: no recent candle has a large opposing wick (a rejection signal)

One position at a time. A stop-loss exits early if the position's bid drops `STOP_LOSS_THRESHOLD` (currently 20%) below entry, disabled in the final 30 seconds. The bot halts entirely if cash falls to `SAFETY_FLOOR` or three non-winning trades happen in a row with no win in between (`STRIKE_LIMIT`).

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

## Configuration

All in the `CONFIG` section at the top of `bot.py`:

| Constant | Current value | Meaning |
|---|---|---|
| `ENTRY_THRESHOLD` | 93 | Cents-scale bid touch that triggers entry |
| `STOP_LOSS_THRESHOLD` | 0.20 | Exit if bid drops this fraction below entry |
| `RISK_PCT` | 0.01 | Flat fraction of cash risked per trade (all trading windows) |
| `MAX_POSITION_DOLLARS` | 500.0 | Hard cap on position size regardless of `RISK_PCT × cash` |
| `SAFETY_FLOOR` | 71 | Bot halts if cash falls to or below this |
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
- Stop-loss is explicitly disabled in a position's final 30 seconds (`time_left <= 0.5`), to avoid selling into a wide spread right before settlement.
