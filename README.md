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
- Two-stage stop-loss: arm at 70¢ bid, trigger at 53¢ (converts -96c loss to ~-40c)

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
PAPER_START_BALANCE = 500.0    # Starting cash for paper mode
PAPER_SAFETY_FLOOR = 0.0       # Don't fall below this in paper
SAFETY_FLOOR = 1000.0          # Live-mode cash floor (blocks trading if breach)
```

### Entry
```python
MAX_SLIPPAGE = 0               # Pay up to ask (no slippage tolerance)
ENTRY_TIME_MIN = 1.0           # Earliest entry: 1 min before close
ENTRY_TIME_MAX = 10.0          # Latest entry: 10 min before close
FLAT_RISK = 0.05               # Stake 5% of available cash per trade
```

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
```

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
- Multi-market merge: current market only has ~20 min of data, so we span 60+ min by including recent settlements
- Computes Wilder's RSI-14 (true exponential smoothing, not simple MA)
- Falls back to yes_bid if yes_ask candle close is None (happens on still-open current candle)
- Returns None if < 15 valid closes (insufficient data, skip entry)

### Stop-Loss Logic (Line 418+)
- **Armed state:** Once held-side (YES or NO) bid falls to STOP_ARM_PRICE (75¢)
- **Triggered:** If bid falls further to STOP_TRIGGER_PRICE (60¢), exit immediately
- **Backtest result:** Converts full -96¢ loss to ~-40¢, beats holding to -100¢ at every slippage level

---

## API Setup

Requires Kalshi API key + private key (free to register at https://kalshi.com):

1. Store API key ID in `apikey.txt` (single line)
2. Store private key PEM in `private.txt` (multi-line)
3. Both files in same directory as `bot.py`

Even paper mode requires API keys to read live market data & candles.

---

## Logging & Output

### Log Levels
```
[HH:MM:SS ET] 🪄 Magick Bot v6.0.0 Active [PAPER/SHADOW] | config summary
[HH:MM:SS] [PAPER/SHADOW] Cash: $XXX.XX | Session: $+XXX.XX  # Heartbeat every 5s
[HH:MM:SS] ⛔ RSI filter: KXBTC15M-... RSI=43.5 < 60 — skip   # Skip (logged once per market)
[HH:MM:SS] ✅ RSI filter passed: RSI=62 >= 60                # Entry candidate
[HH:MM:SS] ⚡ Entry at exactly 96c on YES: ask 96c x5        # Order placed
[HH:MM:SS] ✅ Filled 5/5 @ 96c (fees 7c)                     # Order filled
[HH:MM:SS] ⚠️ Loop Error: ...                                # Non-fatal error (retries)
```

Throttled RSI skip logging: only logs the first skip per market ticker (avoids spam from every 3-second loop).

### Output Files
- `log.txt` — Full event log (same as console, timestamped)
- `state.json` — Current session balance, trade count, streak counter
- `trades.json` — Full trade history (entry price, exit price, settlement, PnL)

---

## Resilience & Error Handling

### Kalshi API Partial Outages
SDK resilience patch (lines 10–31) tolerates null booleans in Market model during API outages. If Kalshi returns null for fields like `fractional_trading_enabled`, the bot parses as `None` instead of crashing. Allows graceful survival through degraded API states.

### Network Failures
- Retries up to 3 times on API errors (configurable RETRY_LIMIT)
- Catches and logs non-fatal exceptions (validates orders, fetches markets, computes RSI)
- Fatal errors (bad auth, disk write fails) are logged and halt the bot

### Data Staleness
- RSI computation always spans the last 60 min of candle data
- Checks for sufficient closes (≥ 15) before computing RSI; skips entry if data is sparse
- Candlestick multi-market merge ensures we have enough history even if current market is young

---

## Tuning for Different Regimes

**Current config is tuned for Consolidation (Jul 2026, BTC $63k, down 40% YoY).**

To switch regimes:

### Back to Accumulation (once weekly RSI reclaims 50+):
```python
RSI_MIN = 55              # Loosen filter
FLAT_RISK = 0.10         # Full 10% position size
STOP_ARM_PRICE = 70      # Original stops
STOP_TRIGGER_PRICE = 53
```

### Into Euphoria (if Bitcoin hits new ATH, RSI 70+ on weekly):
```python
RSI_MIN = 65             # Much stricter
FLAT_RISK = 0.05         # Reduce exposure further
# Consider exiting entirely; edge fades in euphoria
```

### Into Capitulation (if Bitcoin breaks $58k support):
```python
RSI_MIN = 30             # Flip to RSI ≤ 30 (oversold bounce)
FLAT_RISK = 0.20         # 2x position size (highest edge)
# Requires strict risk discipline; high leverage
```

**See `bitcoin_regimes_strategy.md` for full regime transition guide.**

---

## Paper vs. Live Mode

### Paper Mode (`PAPER_MODE = True`)
- No real orders placed
- Simulates fills at market prices (yes_ask / no_ask at entry, settlement at 0 or 100)
- Tracks realized PnL against paper balance
- Useful for: backtesting, data collection, tune optimization without risk

### Live Mode (`PAPER_MODE = False`)
- Places real orders on Kalshi
- Requires API key + private key
- Requires `SAFETY_FLOOR = 1000.0` (won't trade if cash < floor)
- Requires `STRIKE_LIMIT = 8` (halts after 8-loss streak)
- First trade is live; test with small size first

**Recommended:** Run 1–2 weeks in paper mode to verify RSI filter and stop-loss behavior in live market data before switching to live.

---

## Performance Metrics

**Paper mode tracks:**
- Session PnL (this run only)
- Cumulative cash (starting balance + realized PnL)
- Trade count
- Win/loss streak
- Max drawdown (peak-to-trough)

**Live mode adds:**
- Realized fees (scaled by 0.07 FEE_RATE)
- Slippage tracking (ask paid vs. 96¢ baseline)
- Strike counter (consecutive losses)

Check `trades.json` for full history: entry/exit prices, settlement, fees, PnL per trade.

---

## Backtest Data & Reproducibility

**Backtest conditions (Apr–Jun 2026):**
- Data source: Kalshi live API, real KXBTC15M settlement prices
- Entry filter: RSI-14 ≥ 55, FOMC skip only
- Position size: 10% flat
- Stop-loss: arm 70, trigger 53
- ~1,435 entries, 561 trades (after filter)
- 3-month rolling window (full quarterly cycle)

**Results:**
- ROI: +82.8% (from $500 starting → $914 final)
- Win rate: 99.11% (5 losses out of 561)
- Max DD: $768
- Estimated monthly: $500–$600 profit on $1,800 starting balance

**Regime context:**
- This was Accumulation (Bitcoin $100k+, strong uptrend)
- Win rate and ROI are regime-specific; will differ in Consolidation/Euphoria/Capitulation

---

## Files & Directory Structure

```
mystic-bot/
├── bot.py                        # Main bot script (this file)
├── apikey.txt                    # API key ID (not in git)
├── private.txt                   # Private key PEM (not in git)
├── log.txt                       # Full event log (generated)
├── state.json                    # Current session state (generated)
├── trades.json                   # Full trade history (generated)
├── bitcoin_regimes_strategy.md   # Regime tuning guide
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

---

## Development & Contributing

**Last updated:** July 2026  
**Python:** 3.9+  
**Dependencies:** `kalshi-python-sdk`, `pytz`, `pydantic`

Install:
```bash
pip install kalshi-python-sdk pytz pydantic
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

**v6.0.0 (Jul 2026):**
- ✅ RSI-14 filter: multi-market candlestick spanning, Wilder's smoothing, 1-min precision
- ✅ FOMC skip: +11.1% ROI improvement, $132 DD reduction
- ✅ Two-stage stop-loss: arm 70, trigger 53 (converts -96 to -40 loss)
- ✅ Throttled skip logging: one log per market ticker (eliminates spam)
- ✅ SDK resilience patch: tolerate null booleans during API outages
- ✅ Consolidation tuning: RSI 60, 5% size, tighter stops (75→60)
- ✅ Bitcoin regimes documentation: Euphoria/Accumulation/Consolidation/Capitulation guide

**v5.0.0 (May 2026):**
- RSI implementation, backtest validation
- FOMC skip logic

**v4.0.0 (Jan 2026):**
- Core entry/exit, stop-loss framework, paper mode

