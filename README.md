# Mystic-Bot

A buy-only rebalancing bot for an Alpaca account. It holds 10 stocks at an equal
**10% target each** and, whenever you have idle cash, buys the names that are
furthest *below* their target. It never sells.

| Ticker | Company |
|--------|---------|
| KO | Coca-Cola Co |
| MCD | McDonald's Corp |
| AAPL | Apple Inc |
| TSLA | Tesla Inc |
| CL | Colgate-Palmolive Co |
| PG | Procter & Gamble Co |
| WM | Waste Management Inc |
| WMT | Walmart Inc |
| NVDA | NVIDIA Corp |
| JNJ | Johnson & Johnson |

## How the rebalancing works

When there is investable cash, the bot computes, for each name:

```
base       = value of current holdings + cash to deploy
target_i   = 10% of base
deficit_i  = max(0, target_i - current_value_i)     # how far below target
buy_i      = cash * deficit_i / sum(all deficits)    # pro-rata share of cash
```

So a laggard like a dipping Coca-Cola gets the largest slice of new money, an
over-target name like a surging NVIDIA gets little or nothing, and **nothing is
ever sold**. Because it only buys, a very overweight name isn't forced back to
10% in one go — the portfolio converges toward equal weight across successive
deposits. All orders are fractional **notional** (dollar-amount) market orders,
so any deposit size works.

It treats *any* idle cash the same way, so dividends get reinvested into the
laggards too, not just fresh deposits.

## Setup

1. Put this folder on your Desktop as `mystic-bot` (you've already got
   `private/key.txt` and `private/secret.txt` inside it).
2. Install Python 3.10+ and the dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Confirm `private/key.txt` holds your Alpaca **API key id** and
   `private/secret.txt` holds your **secret**, each on its own with no extra
   text. Use **paper** keys to start.

## Paper vs. live

`config.py` ships with `PAPER = True` — simulated money, zero risk. Leave it
there until you've watched it behave. To go live, set `PAPER = False` **and**
swap in your live keys; the runner will also make you type a confirmation
phrase before placing any real order.

## Running the bot

```
python bot.py            # invest available cash now, then exit
python bot.py --dry-run  # show what it WOULD buy, place no orders  (start here)
python bot.py --status   # print account + holdings, trade nothing
python bot.py --watch    # stay running; invest deposits as they land
```

Two natural workflows:

- **Manual:** deposit money, then run `python bot.py` once.
- **Automatic:** leave `python bot.py --watch` running. It checks for new cash
  every 60s (configurable) and deploys it.

To schedule instead of `--watch`:

- **macOS/Linux (cron, every 30 min):**
  ```
  */30 * * * * cd ~/Desktop/mystic-bot && /usr/bin/python3 bot.py >> logs/cron.log 2>&1
  ```
- **Windows:** Task Scheduler → run `python C:\Users\you\Desktop\mystic-bot\bot.py`.

## Dashboard

```
python dashboard.py
```

Then open <http://127.0.0.1:5000>. It auto-refreshes and shows total value,
today's move, idle cash, unrealized P/L, and a per-holding **balance meter**
showing each name's drift from its 10% target (left of center = needs buying).
Recent buys appear at the bottom, pulled from `logs/activity.csv`. The dashboard
is read-only — it never trades.

## Things you can tune in `config.py`

- `PORTFOLIO` — the tickers and weights (must sum to 1.0).
- `CASH_RESERVE_USD` — cash to always leave uninvested.
- `MIN_DEPLOY_USD` — don't act until at least this much cash has built up.
- `WATCH_INTERVAL_SECONDS` — how often `--watch` checks for deposits.
- `ALLOW_OUTSIDE_REGULAR_HOURS` — allow extended/overnight session orders.

## Notes & safety

- Fractional/notional orders are market orders. During market hours they fill
  near the quoted price; outside regular hours fills can be partial or delayed.
- The bot skips a name whose computed buy is under Alpaca's $1 minimum; that
  money simply rolls into the next deposit.
- Keep `private/` out of any git repo (a `.gitignore` is included). Never share
  your keys.
- This is a personal tool, not investment advice. Equal-weight, buy-the-dip
  rebalancing can underperform, and concentrating in 10 names carries real risk.
  You are responsible for any trades it places — test on paper first.
