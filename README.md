# Mystic-Bot 1.0

Automated trading bot for Kalshi's `KXBTC15M` 15-minute Bitcoin price markets.
Runs 24/7, enters near the close of each window when "done-deal" filters
agree the outcome is effectively decided, and manages the position with a
stop-loss until settlement.

> No README existed in this repo before this change, so this file was
> written from scratch based on the current source. If you already keep
> docs elsewhere, treat this as a starting point to merge in rather than
> the source of truth.

## Requirements

- Python 3.9+
- `pytz`, `requests`, `kalshi_python_sync`
- `apikey.txt` and `private.txt` in the same directory as `bot.py` (Kalshi
  API key ID and private key PEM). These are read in both live and shadow
  mode, since shadow mode still needs real market data.
- Windows only, for the beep-on-event / hotkey-override features
  (`winsound`, `msvcrt`); the bot still runs on other platforms with those
  disabled.

## Running

```bash
# Live trading
python bot.py

# Shadow mode: simulated fills, no real orders, separate shadow_*.json/log files
python bot.py --shadow --shadow-cash 1000

# Shadow mode with pessimistic fills: re-quotes the book at order time and
# only "fills" if the order would actually still cross it, at the refreshed
# price. Requires --shadow. This is the fill model closest to live behavior;
# plain shadow mode fills unconditionally at the signal price and overstates
# performance.
python bot.py --shadow --shadow-cash 1000 --shadow-pessimistic
```

Shadow mode writes to `shadow_state.json` / `shadow_log.txt` /
`shadow_trades.json` / `shadow_status.json`, entirely separate from the live
instance's files, so both can point at the same directory without
colliding — but note `--shadow-cash` only seeds `shadow_cash` the *first*
time (when the key is absent from `shadow_state.json`). If you want a clean
balance for a new test, delete or move the old state file first, or the
flag is silently ignored.

If you want two shadow variants running at once for comparison (e.g.
optimistic vs. pessimistic fill models), give each its own directory with
its own copy of `bot.py` and the credential files — `_prefix` is
`"shadow_"` for both, so two instances in the same folder will read and
write each other's state.

## Entry logic

- Only considers the nearest-expiry open `KXBTC15M` market.
- Entry window: 1.75–4.5 minutes before close.
- Entry price is read off the **ask** (what you'd actually pay), not the
  bid — `market_asks()` derives it from the API's ask field when present,
  falling back to `100 - opposite_side_bid` since yes/no share one book.
- Qualifies only if that ask is between `ENTRY_THRESHOLD` (93c) and
  `MAX_ENTRY_THRESHOLD` (95c) inclusive.
- Must also pass the "done-deal" filters: BTC has moved far enough from the
  window's open price for the time remaining (`required_distance_pct`),
  and recent momentum/wick shape confirms the move isn't about to reverse
  (`check_momentum_and_wick`).
- Position size: `min(MAX_POSITION_DOLLARS, cash * RISK_PCT)` dollars,
  converted to contracts at the entry price. At $1000 cash and 1% risk
  that's ~10 contracts per trade; scales with cash as it grows or shrinks.

## Exit logic

- **Stop-loss**: normally 20% below entry (`STOP_LOSS_THRESHOLD`). Requires
  the price breach to hold for `STOP_CONFIRM_LOOPS` consecutive ~1s polls,
  and requires live BTC price to have actually reversed
  (`btc_confirms_stop`) before firing — filters out pure order-book flash
  spikes that aren't backed by the underlying moving.
- **Settlement**: when the market rolls to a new ticker, waits ~35s for the
  prior window to finalize and records the result.
- `HIGH_ENTRY_STOP_*` constants widen or fully disable the stop for
  entries at 96–97c. **These are currently dormant** — with
  `MAX_ENTRY_THRESHOLD = 95`, no entry can ever reach that price, so every
  position uses the plain 20% stop. Left in place (not deleted) so raising
  `MAX_ENTRY_THRESHOLD` back up later restores the old behavior without
  re-deriving these values.

## Risk controls

| Control | Behavior |
|---|---|
| `SAFETY_FLOOR_PCT` (75%) | Bot **shuts down entirely** (process exits) if cash drops to 75% of the highest balance ever reached (trailing peak, not a fixed dollar figure). |
| `STRIKE_LIMIT` (3) | Bot shuts down after 3 consecutive losing trades (win resets the counter). |
| `MAX_DAILY_DRAWDOWN_PCT` (10%) | **New.** If today's cash drops 10% below today's opening cash, new entries pause until midnight ET. Any position already open keeps being monitored and can still stop out or settle normally — only new entries are blocked. |

The daily drawdown pause is intentionally softer than the safety floor and
strike limit: those two stop the bot outright, this one just waits out the
rest of a bad day and resumes automatically. Baseline resets each ET
calendar day; if the bot restarts mid-day, it keeps the existing baseline
and pause state from `state.json` rather than treating the restart as a
fresh day.

`status.json` exposes `daily_start_cash`, `daily_drawdown_pct`,
`daily_drawdown_limit_pct`, `is_daily_paused`, and `daily_paused_until` for
any external dashboard, and the console heartbeat line shows the current
daily drawdown and a `[DAILY DD PAUSE]` tag while paused.

## Known limitations

- Shadow mode (even `--shadow-pessimistic`) fills on a book-crossing check,
  not real queue position — live orders are passive limits that wait to be
  hit, which is a different and generally worse mechanic. Treat shadow
  results, pessimistic or not, as an upper bound on live performance, not
  an estimate of it.
- `place_order`'s live path reports the requested limit price as the fill
  price, not the realized average fill, since the current client doesn't
  surface it. True fills are at or better than that price on a crossing
  order, so live PnL bookkeeping is if anything conservative here.
- At 93–95c entries, breakeven requires a win rate in the mid-90s. A
  moderate number of settled trades (dozens) is an operational smoke test,
  not a statistically meaningful performance read — expect several hundred
  trades before the win rate is trustworthy.
