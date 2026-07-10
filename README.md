# Magick Bot v6.0.0 — Binary Prediction Market Trading Bot

A Python bot that trades Kalshi 15-minute Bitcoin KXBTC15M contracts using a thin-edge strategy: **buy whichever side's ask is exactly 96¢, hold to settlement**. Edge comes from RSI-14 filtering, FOMC avoidance, and a two-stage stop-loss.

## Quick Start

```bash
python bot.py
```

Paper trading enabled by default (`PAPER_MODE = True`). Set to `False` + valid API keys to trade live.

---

## Core Strategy

**Entry Rule:**
- Buy YES or NO at exactly 96¢ ask price
- Only during 1–10 minutes before KXBTC15M 15m contract settlement
- Only when RSI-14 ≥ threshold (default 60, tunable by regime)
- Skip FOMC decision days (only macro day that hurts this edge)

**Exit Rule:**
- Hold to settlement (auto-settle on Kalshi at 0 or 100¢)
- Two-stage stop-loss: arm at 75¢ bid, trigger at 60¢ (converts -96c loss to ~-40c)

**Position Size:**
- Flat 5% of available cash per trade (tunable; default reduced from 10% for consolidation)
- Max position: $500 or safety floor, whichever is stricter (live mode)

---

## Edge & Backtest Results

**3-month backtest (Apr–Jun 2026, Accumulation regime):**
- RSI ≥ 55 filter: +82.8% ROI, 99.11% win rate, 561 trades, $768 max drawdown
- Without RSI filter: +71.7% ROI, 98.63% win rate (edge disappears without filter)
- FOMC skip alone: +11.1% ROI improvement, $132 DD reduction
- Stop-loss: converts full -96c losses to ~-40c, beats no-stop across all slippage levels

**Starting balance for $500/month profit target:** ~$1,800 (scales linearly with ROI)

### Important: Edge Varies by Bitcoin Regime

**This backtest was run during Accumulation** (strong uptrend, Bitcoin climbing). Current market (Jul 2026) is **Consolidation** (down 40% YoY, bouncing in range). Edge characteristics change significantly:

| Regime | RSI Threshold | Position Size | Win Rate | Use Case |
|--------|---------------|---------------|----------|----------|
| **Euphoria** (ATH chasing, RSI 70+) | 65–70 | 5% | 92% | Exit, reduce exposure |
| **Accumulation** (strong uptrend, RSI 50–70) | 55 | 10% | 99% | Optimal, full throttle |
| **Consolidation** (bouncing lower, RSI 30–50) | 60 | 5% | 75% | **Current regime** — selective entry |
| **Capitulation** (panic selling, RSI <30) | 30–35 | 20% | 88% | Highest edge, high risk |

**See `bitcoin_regimes_strategy.md` for full regime guide**, including detection methods, expected outcomes, and config changes for each cycle.

---

## Config

All settings in top of `bot.py`, lines 41–130:

### Trading Mode
```python
PAPER_MODE = True              # False = live trading (requires API keys)
DEMO_MODE = False              # True = real orders on Kalshi demo sandbox (mock funds)
PAPER_START_BALANCE = 500.0    # Starting cash for paper mode
PAPER_SAFETY_FLOOR = 0.0       # Don't fall below this in paper
SAFETY_FLOOR_LIVE = 300.0      # Real live-mode cash floor (blocks trading if breached)
SAFETY_FLOOR_DEMO = 10.0       # Sandbox floor so demo runs on a small mock balance
SAFETY_FLOOR = SAFETY_FLOOR_DEMO if DEMO_MODE else SAFETY_FLOOR_LIVE
```

### Entry
```python
MAX_SLIPPAGE = 0               # Pay up to ask (no slippage tolerance)
ENTRY_TIME_MIN = 1.0           # Earliest entry: 1 min before close
ENTRY_TIME_MAX = 10.0          # Latest entry: 10 min before close
FLAT_RISK = 0.05               # Stake 5% of available cash per trade
MAX_POSITION_DOLLARS = 500.0   # Dollar ceiling per entry
MAX_CONTRACTS_PER_MARKET = 10  # Hard cap on combined contracts held in one market (0 = off)
```

**Position cap (safety layer):** `MAX_CONTRACTS_PER_MARKET` is an independent hard
ceiling on how many contracts the bot will ever hold in a single market, combined
across entries. It trims an order to the remaining room, or skips once the cap is
reached. Critically, it uses a **fail-safe position read**: if the exchange position
can't be read, the bot **skips the entry rather than assuming it's flat** — this
prevents the over-buy failure mode where a read-back error let the bot re-enter and
stack a large position. This sits on top of `FLAT_RISK`/`MAX_POSITION_DOLLARS` sizing
as a last line of defense, alongside the drawdown breaker.

### RSI Filter (Regime-Sensitive)
```python
USE_RSI_FILTER = True          # Set False to disable
RSI_MIN = 60                   # Skip entries where RSI-14 < 60
                               # Consolidation default (55 for Accumulation, 65-70 for Euphoria)
RSI_LOOKBACK_MIN = 60          # Minutes of 1-min candle history to fetch
```

### FOMC Skip
```python
SKIP_FOMC_DAYS = True          # Skip FOMC decision days (improves ROI +11%)
FOMC_DECISION_DATES = {        # Update this list annually
    "2026-01-28",
    "2026-03-18",
    "2026-04-29",
    "2026-06-17",
    "2026-07-29",
    "2026-09-16",
    "2026-10-28",
    "2026-12-09",
}
```

### Stop-Loss (Two-Stage)
```python
USE_STOP = True
STOP_ARM_PRICE = 75            # Consolidation default (70 for Accumulation)
STOP_TRIGGER_PRICE = 60        # Consolidation default (53 for Accumulation)
                               # Arm at 75c, exit if it drops to 60c (converts -96c to -40c loss)
```

### Risk & Circuit Breaker
```python
STRIKE_LIMIT_LIVE = 8          # Live mode: halt after 8-loss streak
STRIKE_LIMIT = None            # Paper mode: disabled (collect data)
FILL_POLL_TRIES = 4            # Seconds to wait for order fill before cancel

# --- Drawdown circuit breaker (LIVE MODE ONLY) ---
USE_DRAWDOWN_LIMIT = True      # No-op in paper mode
MAX_DRAWDOWN_PCT = 0.10        # Halt if settled balance falls 10% below its peak
```

### Drawdown Circuit Breaker (Live Only)

A **relative** risk guard that complements the absolute cash floor. It tracks a
**high-water mark** (the highest settled balance ever seen, stored in `state.json`)
and halts the bot if the settled balance falls more than `MAX_DRAWDOWN_PCT` below
that peak.

- **Peak-to-current drawdown:** halts when `balance <= peak * (1 - MAX_DRAWDOWN_PCT)`.
  With a 10% limit and a $2,000 peak, the bot stops at $1,800.
- **Scales with the account:** unlike the fixed `SAFETY_FLOOR`, it tightens as the
  account grows (a 10% giveback from a new high always triggers).
- **Sticky by design:** when tripped, it writes a persistent `halted` flag to
  `state.json` and exits. **The bot will NOT auto-resume on restart** — this
  prevents a cron job or accidental restart from trading back into a drawdown.
- **Manual re-arm required:** clear the halt with:
  ```bash
  python bot.py --reset-halt
  ```
  This removes the flag and resets the high-water mark to the *current* balance,
  so drawdown is measured from where you actually stand on re-entry (not the old
  pre-loss peak).
- **Paper mode ignores it entirely** — data-collection runs never get cut short.

**Tuning:** `0.05` = tighter (5% giveback stops), `0.20` = looser (ride out more
volatility). Match it to your regime — consolidation warrants a tighter limit than
a strong accumulation uptrend.

---

## How It Works

### Main Loop (Line 497+)
1. **Fetch KXBTC15M markets** from Kalshi API (only open markets with time left)
2. **Check schedule & skip conditions:** Is it Schedule A hours? FOMC day? Macro news day?
3. **Compute RSI-14** from 1-min candlestick history (spanning current + 2 settled markets)
4. **Check entry window:** Is close time 1–10 min away?
5. **Determine side & check bid-ask:** Is ask price exactly 96¢ on either YES/NO?
6. **Enter:** Place a limit order at 96¢ for the appropriate side
7. **Hold:** Monitor stop-loss if armed. Kalshi auto-settles at close.
8. **Exit:** On settlement or stop-loss trigger
9. **Loop:** Repeat every ~5 seconds during trading hours

### RSI Computation (Line 159+)
- Fetches 1-min OHLC from current market + 2 recently-settled KXBTC15M markets
- Merges 60+ minutes of price history (current market only has ~20 min)
- Computes Wilder's RSI-14 (exponential smoothing, not simple MA)
- **RSI data source:** Uses pure ask-price close data by default; only blends in bid data as a fallback if ask is unavailable (logged when it happens)
- Handles null close prices (falls back to yes_bid if yes_ask is None)
- Returns None if < 15 valid closes (insufficient data)
- Field parsing: tries `close_dollars` (new SDK) then `close` (old SDK)
- Deduplicates timestamps across markets

---

## Safety Improvements (v6.0.0 Security Release)

### Fix #1: Strict Position Reads on Exit
The `flatten()` helper now uses a strict position read before assuming an exit is complete. If it can't confirm the position is closed, it continues monitoring instead of silently assuming flat. This prevents the same over-buy failure mode from occurring on the exit side.

### Fix #2: Accurate Fee Calculation
Fee calculation now multiplies `average_fee_paid` (per-contract per Kalshi's V2 spec) by the actual filled contract count. Previously undercounted fees by the contract multiplier on every multi-contract fill.

### Fix #3: Settlement Result None-Handling
Settlement parsing no longer crashes if the result isn't posted yet (result is None). Gracefully degrades to monitoring state instead of hard-erroring.

### Fix #4: Pure-Ask RSI Series (Prefer-Ask Logic)
RSI now prefers a pure ask-price series and only blends in bid data as a last resort (logged when it happens). This prevents silent mixing of ask/bid quotes that could skew RSI calculations.

### Fix #5: Interruptible Settlement Wait
The 35-second settlement wait is now interruptible instead of a flat blocking sleep. Allows graceful shutdown without hanging.

### Fix #6: UTF-8 Encoding on Trade Logging
`trades.json` writes now explicitly specify UTF-8 encoding, consistent with the rest of the codebase.

### Fix #7: Reduce-Only on Exit Orders
All sell orders now carry `reduce_only=True`, telling the exchange itself to cap a sell at whatever you actually hold. Even if the bot's local contract count were ever wrong, it physically can't sell more than it owns and flip into an unintended opposite position.

### Fix #8: Explicit No-Re-Entry Guard (Main Ask)
**New state["entered_tickers"] list** tracks every ticker that has had a fill this session. Once a fill is confirmed—fresh buy or adopted orphaned position—that ticker gets permanently recorded. The fresh-buy path refuses it outright afterward, no matter what.

**Why this matters:** Relying on current trade/position reads to prevent re-entry has a real gap. If a stop-loss fires and closes the position with time still left before contract close, current_trade goes back to None, and the bot would happily buy the same 15-minute contract a second time. The new guard closes that specific hole unconditionally.

**Critically**, this doesn't interfere with the existing, correct behavior of retrying an unfilled order at 96¢ every few seconds until it lands—the ticker only gets locked after a real fill, never before. Tested both directions.

---

## Files & Directory Structure

```
mystic-bot/
├── bot.py                        # Main bot script
├── dashboard.py                  # Optional local Flask dashboard
├── apikey.txt                    # Prod API key ID (not in git)
├── private.txt                   # Prod private key PEM (not in git)
├── apikey_demo.txt               # Demo API key ID — used when DEMO_MODE=True (not in git)
├── private_demo.txt              # Demo private key PEM — used when DEMO_MODE=True (not in git)
├── log.txt                       # Full event log (generated)
├── state.json                    # Current session state incl. mode field (generated)
├── trades.json                   # Full trade history (generated)
├── bitcoin_regimes_strategy.md   # Regime tuning guide
├── Mystic-Bot.md                 # Development log / memory dump
└── README.md                     # This file
```

---

## Troubleshooting

### "RSI: only 0 candles" warning
- Happens on bot startup or if Kalshi API candlestick endpoint is slow
- Normal; resolves once market opens or API recovers
- If persistent: check `compute_rsi()` debug logs or verify API key permissions

### "Loop Error: fractional_trading_enabled validation error"
- Kalshi API partial outage (SDK expects bool, API returns null)
- Bot recovers automatically via SDK resilience patch
- Monitor Kalshi status page; usually resolves in 15–30 min

### "Order submit error: (410) Gone — deprecated_v1_order_endpoint"
- Kalshi retired the V1 order endpoint; your SDK is placing V1 orders
- Fix: upgrade the SDK to 3.23.0+ (`pip install --upgrade kalshi_python_sync`)
- v6.0.0+ already uses V2 (`create_order_v2`); if you still see this, your
  installed SDK is older than 3.23.0 or an old bot.py is running
- Verify: `python -c "import kalshi_python_sync as k; print(k.__version__)"`

### "No open KXBTC15M markets found"
- Market is closed (off-hours)
- Schedule A only trades 5 PM–10 PM ET drop window
- Check Kalshi calendar; markets publish on a rolling 15-min schedule

### "Order rejected: invalid price"
- Ask price is not exactly 96¢ (might be 95.5¢ or 97¢ due to market movement)
- Bot re-checks every 3 sec; will catch the next 96¢ print
- Normal; no action needed

### Paper balance going negative
- PAPER_SAFETY_FLOOR is 0 (allows deficit)
- Set to positive value to halt trading if underwater
- Check stop-loss: if triggering too late, losses accumulate faster than expected

### Bot exits immediately with "Bot is HALTED by drawdown stop"
- The live drawdown breaker tripped on a prior run (balance fell ≥10% below peak)
- This is intentional and sticky — it won't resume until you clear it
- Review recent trades in `trades.json` to understand the drawdown before re-arming
- When ready, run `python bot.py --reset-halt` to clear the flag and reset the peak
- If you want a looser limit, raise `MAX_DRAWDOWN_PCT` before restarting

### Drawdown breaker tripping too easily / too late
- Too easily: raise `MAX_DRAWDOWN_PCT` (e.g., 0.15 or 0.20) for more room
- Too late: lower it (e.g., 0.05) for a tighter stop
- Remember it measures from the **peak**, not your starting balance — after a run-up,
  the halt level rises with it

### Dashboard shows PAPER when the bot is running live/demo
- Caused by a stale `paper_balance` key left in `state.json` from an earlier paper run
- Fixed: the bot now writes an explicit `mode` field and clears the stale key on its
  next run — just restart the bot and it self-heals
- To fix the dashboard immediately without restarting, delete the `"paper_balance"`
  line from `state.json`

### Demo mode: "Shutdown (cash floor): Cash $0.00"
- The demo account has no mock funds yet (not a bug — auth and balance read are working)
- Fund it at https://demo.kalshi.co (the `.co` domain): deposit → pick any bank from the
  mock list → wait for the mock ACH to settle (not instant)
- Demo cash floor is `SAFETY_FLOOR_DEMO` ($10), so a small mock balance is enough
- Verify balance directly: read `client.get_balance().balance` against the demo host;
  once it's non-zero, restart the bot
- Note: demo market prices/liquidity aren't representative of production — demo proves
  the order request/response works, not real fill behavior

### "'Fill' object has no attribute 'count'" / "'MarketPosition' object has no attribute 'position'"
- V1→V2 field-name mismatch in the fill/position read-back (fixed in v6.0.0)
- V2 uses `count_fp` + `yes_price_dollars`/`no_price_dollars` on `Fill`, and
  `position_fp` on `MarketPosition`
- These errors are dangerous, not cosmetic: they blind the bot to its own position
  and can cause repeated re-entry / over-buying. If you see them, you're on old code —
  update `bot.py`
- Protection: the per-market cap now uses a fail-safe position read (skips entry if
  position can't be confirmed), and the drawdown breaker backstops the rest

### Bot bought far more than expected / stacked a large position
- Root cause was the parsing bug above (bot couldn't see its own position, re-entered)
- Fixed, plus `MAX_CONTRACTS_PER_MARKET` now hard-caps combined size per market
- If it still happens: check `MAX_CONTRACTS_PER_MARKET` isn't 0 (disabled), and confirm
  `get_positions` is returning readable data (the cap skips entries when it can't)

---

## Development & Contributing

**Last updated:** July 10, 2026  
**Python:** 3.9+  
**Dependencies:** `kalshi_python_sync` (>= 3.23.0, needs Python 3.13+), `pytz`, `pydantic`

Install:
```bash
pip install "kalshi_python_sync>=3.23.0" pytz pydantic
```

**Testing changes:**
1. Run in paper mode first: `PAPER_MODE = True`
2. Capture 50+ trades to see edge in action
3. Log to `trades.json` and analyze PnL distribution
4. Compare against backtest baseline before deploying live

---

## Disclaimer

**This is a research/educational bot.** It is not financial advice. Bitcoin trading is high-risk and highly volatile. Past backtest results do not guarantee future performance. Regime changes (Euphoria → Consolidation) shift edge magnitude. Always trade with capital you can afford to lose. Start small (paper mode, then 1–2% live) before scaling. 🪄

---

## License & Attribution

Built for algorithmic trading research. Attribution appreciated if you fork or adapt.

---

## Changelog

**v6.0.0 (Jul 2026, Security Release):**
- ✅ Seven safety fixes + explicit no-re-entry guard (v6.0.1)
  - Fix #1: Strict position reads on exit (flatten)
  - Fix #2: Accurate fee calculation (multiply by fill count)
  - Fix #3: Settlement result None-handling (no crash on unsettled)
  - Fix #4: Pure-ask RSI series (prefer-ask logic, fallback to bid with logging)
  - Fix #5: Interruptible settlement wait (no blocking sleep)
  - Fix #6: UTF-8 encoding on trades.json
  - Fix #7: Reduce-only on exit orders (physical position cap)
  - Fix #8: Explicit no-re-entry guard (entered_tickers state tracking)
- ✅ RSI-14 filter: multi-market candlestick spanning, Wilder's smoothing, 1-min precision
- ✅ FOMC skip: +11.1% ROI improvement, $132 DD reduction
- ✅ Two-stage stop-loss: arm 75, trigger 60 (converts -96 to -40 loss)
- ✅ Throttled skip logging: one log per market ticker (eliminates spam)
- ✅ SDK resilience patch: tolerate null booleans during API outages
- ✅ Consolidation tuning: RSI 60, 5% size, tighter stops (75→60)
- ✅ Bitcoin regimes documentation: Euphoria/Accumulation/Consolidation/Capitulation guide
- ✅ Drawdown circuit breaker (live only): sticky halt at 10% below high-water mark, `--reset-halt` to re-arm
- ✅ V2 order migration: SDK 3.23.0, `create_order_v2` with IOC, YES/NO→bid/ask mapping, DEMO_MODE sandbox flag
- ✅ Demo sandbox: separate demo key files, demo-aware cash floor ($10 vs $300 live)
- ✅ Dashboard mode fix: reads explicit `state["mode"]` instead of stale `paper_balance`; shows DEMO distinctly
- ✅ V2 fill/position parsing fix (`count_fp`/`*_price_dollars`/`position_fp`) — resolves live over-buy bug
- ✅ Per-market contract cap (`MAX_CONTRACTS_PER_MARKET`) with fail-safe position read

**v5.0.0 (May 2026):**
- RSI implementation, backtest validation
- FOMC skip logic

**v4.0.0 (Jan 2026):**
- Core entry/exit, stop-loss framework, paper mode
