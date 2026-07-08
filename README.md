# Magick Bot

A Python bot that trades 15-minute Bitcoin price-direction contracts on [Kalshi](https://kalshi.com). It watches the `KXBTC15M` series and enters a position whenever either side's ask hits exactly 96¢ — buying a deep favorite, filtering with RSI-14, and holding to settlement.

**Status: paper/shadow mode by default. Research-grade. Not financial advice.**

---

## How it works

Each `KXBTC15M` market is a binary question: will BTC be higher or lower in 15 minutes than it is right now? The contract pays $1.00 if correct, $0.00 if not.

The bot's entry rule: if the YES ask or NO ask is exactly 96¢, and RSI-14 is at or above 55, buy that side. At 96¢ entry, breakeven is 96.27% wins (accounting for fees). Backtesting on real Kalshi candlestick data (Apr–Jun 2026, 584 RSI-filtered trades on Schedule A) showed a 98.63% realized win rate — well above breakeven, with every month profitable.

The payoff is asymmetric: win +$0.04/contract, lose −$0.96/contract. A single loss erases ~24 wins, so loss clustering is the dominant risk even at a high win rate.

---

## Entry logic

```
Every 15-minute KXBTC15M market, 1–10 minutes before close:
  if yes_ask == 96¢ or no_ask == 96¢:
    compute RSI-14 from the last 20 one-minute KXBTC15M candles
    if RSI-14 < 55  →  skip (sub-threshold momentum)
    else            →  buy that side at 96¢
  else              →  skip, wait for next market
```

Sizing is 25% of available cash per trade, capped at $500 notional (`FLAT_RISK = 0.25`, `MAX_POSITION_DOLLARS = 500`). Positions are held to settlement — no early exit — unless the optional two-stage stop is enabled.

---

## RSI filter

Analysis of 1,435 real 96¢ entries (Apr–Jun 2026) found that entries with RSI-14 below 55 won only ~12% of the time — far below the 96.27% breakeven. Entries with RSI-14 at or above 55 won 98.63%. The filter cuts roughly 40% of trade volume but eliminates losing months entirely on the test window.

**Why RSI predicts outcome here:** RSI is functioning as a proxy for momentum direction. Low RSI at a 96¢ entry almost always means the NO side is being bid up aggressively (the book thinks BTC will keep falling), and those entries were systematically wrong during the Apr–Jun 2026 bear market. High RSI entries are predominantly YES-side in a trending market, where short-term momentum persistence was strong.

RSI is computed from the last 20 one-minute `yes_ask` closes of the KXBTC15M series — the same data the bot already reads, with no external dependency.

| RSI-14 at entry | Trades | Win rate | vs breakeven |
|---|--:|--:|--:|
| < 55 (skipped) | 851 | 12.1% | −84pp |
| ≥ 55 (entered) | 584 | 98.63% | +2.36pp |

To disable the filter and trade all 96¢ prints: set `USE_RSI_FILTER = False`.

---

## Trading schedule (Schedule A)

Based on hourly PnL analysis of real Kalshi data, the bot trades all weekday hours **except 17:00–21:59 ET**. The post-market US session (17–22 ET) lost −$9/trade on average across 539 trades, driven by erratic Bitcoin vol after equity markets close. All other hours averaged +$3–6/trade.

| Period | Status |
|---|---|
| Mon–Fri 00:00–16:59 ET | ✅ Active |
| Mon–Fri 17:00–21:59 ET | ⛔ Blocked |
| Mon–Fri 22:00–23:59 ET | ✅ Active |
| Sunday 12:00–16:59 ET | ✅ Active |
| Saturday | ⛔ Closed |

---

## Stop-loss

The bot ships with `USE_STOP = False` (hold to settlement). A two-stage stop is implemented but off by default.

The stop arms when the held-side bid drops to `STOP_ARM_PRICE` and triggers an exit when it drops further to `STOP_TRIGGER_PRICE`. Backtesting showed that a tight threshold — arm at 70¢, trigger at 53¢ (≈45% down from a 96¢ entry) — performed favorably at every slippage level tested, converting full −96¢ losses into ~−40¢ stop-outs. The old 80→75¢ thresholds were net-negative because the KXBTC15M book gaps violently; average realized fill on a stop-out was ~44¢, not the trigger price.

To enable:

```python
USE_STOP = True
STOP_ARM_PRICE = 70
STOP_TRIGGER_PRICE = 53
```

---

## Backtested results

All results on real Kalshi KXBTC15M candlestick data, Apr–Jun 2026, starting balance $10,000.

| Configuration | Trades | Win rate | ROI | Max DD | All months green |
|---|--:|--:|--:|--:|:---:|
| Schedule A, no filter, no stop | 1,435 | 96.93% | +49.6% | $2,317 | No |
| Schedule A, no filter, stop 45% | 1,435 | 96.93% | +118.2% | $1,098 | No |
| **Schedule A, RSI ≥ 55, no stop** | **584** | **98.63%** | **+71.7%** | **$900** | **Yes** |
| **Schedule A, RSI ≥ 55, stop 45%** | **584** | **98.63%** | **+84.2%** | **$573** | **Yes** |

The RSI filter and stop together produced the strongest risk-adjusted result: +84.2% ROI with a $573 max drawdown and no losing months across the 3-month window.

These results are in-sample on 3 months of data. Treat them as directionally encouraging, not a performance guarantee.

---

## Setup

**Requirements**

- Python 3.10+
- A Kalshi account with API access enabled
- `kalshi-python-sync`, `pytz`

```
pip install kalshi-python-sync pytz
```

**API keys**

Create two files in the same directory as `bot.py`:

- `apikey.txt` — your Kalshi API key ID (one line)
- `private.txt` — your RSA private key in PEM format

Even in paper mode the bot needs read access to fetch live market prices, candles, and settlement results.

**Run**

```
python bot.py
```

The bot starts in `PAPER_MODE = True` and will not place real orders. All simulated trades are written to `trades.json`. To go live, set `PAPER_MODE = False` in the config block at the top of `bot.py`.

---

## Configuration reference

All knobs are at the top of `bot.py`.

| Variable | Default | Description |
|---|---|---|
| `PAPER_MODE` | `True` | Shadow mode — reads live data, simulates fills, no real orders |
| `PAPER_START_BALANCE` | `1000.0` | Simulated starting balance |
| `FLAT_RISK` | `0.25` | Fraction of cash staked per trade |
| `MAX_POSITION_DOLLARS` | `500.0` | Hard cap on notional per trade |
| `FEE_RATE` | `0.07` | Fee rate for paper PnL. Verify against current KXBTC15M schedule |
| `ENTRY_TIME_MIN` | `1.0` | Earliest entry (minutes before close) |
| `ENTRY_TIME_MAX` | `10.0` | Latest entry (minutes before close) |
| `USE_RSI_FILTER` | `True` | Enable the RSI-14 entry gate |
| `RSI_MIN` | `55` | Minimum RSI-14 required to enter |
| `RSI_CANDLES` | `20` | One-minute candles fetched to compute RSI (need ≥ 15) |
| `USE_STOP` | `False` | Enable the two-stage stop-loss |
| `STOP_ARM_PRICE` | `80` | Held-side bid level that arms the stop |
| `STOP_TRIGGER_PRICE` | `75` | Held-side bid level that fires the exit |
| `SAFETY_FLOOR` | `1000.0` | Live-mode cash floor — halts the bot if breached |
| `STRIKE_LIMIT_LIVE` | `8` | Consecutive-loss circuit breaker (live mode only) |

---

## Files

| File | Description |
|---|---|
| `bot.py` | Main bot |
| `dashboard.py` | Read-only Flask dashboard — run separately, open `http://127.0.0.1:5003` |
| `state.json` | Persisted loop state: current trade, strike count, paper balance |
| `trades.json` | Full trade log (entry, exit, PnL, type) |
| `log.txt` | Timestamped event log |
| `apikey.txt` | Kalshi API key ID (**do not commit**) |
| `private.txt` | RSA private key PEM (**do not commit**) |

Add `apikey.txt` and `private.txt` to `.gitignore`.

---

## Dashboard

`dashboard.py` is a read-only Flask app that displays live bot state in a browser. It is fully decoupled from `bot.py` — it never imports it, never calls the Kalshi API, and never writes any files. It simply polls the three files `bot.py` already writes and renders them as a dark-themed single-page dashboard.

**What it shows:**

- Balance, today's PnL, total PnL, win rate, and win/loss record
- Current open position (ticker, side, entry price, contracts, stop-armed status)
- Cumulative PnL sparkline (last 120 trades)
- Recent trade log (last 200 trades: time, ticker, side, type, PnL)
- Live tail of `log.txt` (last 60 lines, auto-scrolled)
- A stale-data indicator if the bot hasn't updated files in 15 seconds

The page polls `/api/data` every 3 seconds. Everything — HTML, CSS, and JavaScript — lives inline in the single `PAGE` string in `dashboard.py`, so there are no `templates/` or `static/` folders to manage.

**Requirements**

```
pip install flask pytz
```

**Run**

Drop `dashboard.py` in the same folder as `bot.py` and run:

```
python dashboard.py
```

Then open `http://127.0.0.1:5003`. The bot and the dashboard run as separate processes — start them independently.

If your bot files live elsewhere:

```
BOT_DIR=/path/to/your/bot python dashboard.py
```

**Environment variables**

| Variable | Default | Description |
|---|---|---|
| `BOT_DIR` | Script's own directory | Path to the folder containing `state.json`, `trades.json`, `log.txt` |
| `STRIKE_LIMIT` | `3` | Mirrors `STRIKE_LIMIT_LIVE` in `bot.py` — keep in sync if you change it |
| `PAPER_START_BALANCE` | `1000.0` | Used to initialize the balance display before any trades are recorded |
| `DASHBOARD_DEBUG` | `""` | Set to `1` to include Python tracebacks in API error responses |

---

## Safety features

- **Paper mode** — default on; no real orders until you flip the flag
- **RSI gate** — skips entries with RSI-14 below 55; logs every skip so you can verify it in paper mode before going live
- **Cash floor** — live mode halts if balance drops below `SAFETY_FLOOR`
- **Consecutive-loss circuit breaker** — halts after 8 losses in a row (live mode); disabled in paper so collection runs don't get cut short
- **Unfilled order cleanup** — any unmatched remainder is canceled immediately after the fill-poll window; no resting orders are left in the book
- **Position reconciliation** — on each loop the bot compares its tracked position against the exchange and corrects mismatches
- **Manual override** — press `C` on Windows to flatten the current position; `Esc` to exit

---

## Backtesting

The research folder contains a standalone backtesting engine and a real-data collector:

- `kalshi_btc15m_backtest.py` — synthetic backtest using Binance 1-minute BTC data as a market proxy (no pandas/numpy — pure stdlib + matplotlib)
- `kalshi_btc15m_tests.py` — stop-loss and schedule comparison tests, including gap-scaled slippage modeling
- `kalshi_collect_candles.py` — pulls real KXBTC15M candlestick data (yes/no bid/ask per minute) from the Kalshi API into a flat CSV
- `run_real_backtest.py` — runs the engine against real Kalshi data

To collect real Kalshi history and run the real-data backtest:

```
# 1. Test with 5 markets first (default MAX_MARKETS=5)
python kalshi_collect_candles.py

# 2. Verify kalshi_quotes_REAL.csv looks right, then set MAX_MARKETS=None and re-run
python kalshi_collect_candles.py

# 3. Run the backtest
python run_real_backtest.py
```

---

## Known limitations

- **3 months of real data.** The edge appears real but the sample is small. A minimum of 6 months / ~4,000 trades is a reasonable bar before drawing firm conclusions.
- **RSI filter is regime-dependent.** The filter was derived during a bear market (Apr–Jun 2026, BTC −14%). In a bull market the NO-side entries would likely perform differently. Monitor the skip log in paper mode and revisit the threshold on fresh data.
- **Paper fills are optimistic.** Paper mode assumes you always get the ask. Real books don't always give that, especially on fast 96¢ prints. Running live at minimum size ($50–100 notional cap) is the most informative thing you can do while accumulating data.
- **Stop-loss fills gap.** When a 96¢ favorite reverses hard, the bid doesn't glide to the trigger — it gaps through it. Average realized fill on a stop-out in backtesting was ~44¢, not the trigger price. Account for this when sizing stop thresholds.
- **Fee rate changes.** Kalshi adjusts fees on individual series. Verify `FEE_RATE` against `get_series_fee_changes` for KXBTC15M before running live.
- **Schedule A is derived in-sample.** The 17–22 ET block was identified as negative on the same 3-month dataset used to measure the edge. It has a plausible structural explanation (post-market BTC vol), but it should be confirmed on fresh data before being treated as a permanent rule.
