<!-- README update for v6.1.0 — paste-ready sections.
     The current README wasn't attached, so these are drop-in blocks:
     replace your existing "Strategy" / "Risk Management" (or equivalent)
     sections with the ones below, and bump any version references to v6.1.0. -->

## Strategy (v6.1.0)

Magick Bot trades Kalshi **KXBTC15M** 15-minute Bitcoin binary contracts. It buys
whichever side's ask prints **exactly 96c** with **1–10 minutes** left before
settlement and **holds to settlement** (no price stop as of v6.1.0 — see Risk
Management). One entry per market per session.

Entry gates, in order:
- **Schedule A** — weekdays all hours except 17:00–21:59 ET; Sunday 12:00–16:59 ET; Saturday closed.
- **FOMC skip** — no entries on FOMC decision days.
- **RSI-14 filter** — entry requires RSI ≥ 55 (Wilder, 1-min closes stitched
  across the current + recently settled markets). ⚠️ This filter is also the
  bot's *side selector*: because RSI is computed on the YES price series, it
  structurally suppresses NO-side entries, which lost badly in backtesting.
  Do not disable it without re-running `backtest.py`.

## Risk Management (v6.1.0)

- **Sizing:** flat **5%** of balance per trade (was 10%), capped at $500 and
  150 contracts per market.
- **No price stop:** the two-stage stop was removed in v6.1.0 after a 434-trade
  backtest on real candle data showed all 35 stop-outs were whipsaws (every
  stopped market settled as a winner; the stop cost $773 vs. holding). Stop
  params remain in `bot.py` (`USE_STOP`, `STOP_ARM_PRICE`, `STOP_TRIGGER_PRICE`)
  for re-enable if the regime changes.
- **Drawdown circuit breaker:** sticky halt at **10%** below the high-water
  mark; requires `python bot.py --reset-halt` to re-arm.
- **Other rails:** 8-consecutive-loss strike halt, $0.01 cash floor,
  per-session re-entry guard, `reduce_only` exits, fail-safe position reads.

## Backtesting tooling (new in v6.1.0)

- `kalshi.py` — pulls ~90 days of settled KXBTC15M markets + 1-minute candles
  via the authenticated SDK (same `apikey.txt` / `private.txt` as the bot);
  writes `kalshi_KXBTC15M_3months_1m_candles.csv`.
- `backtest.py` — replays the full live strategy against that CSV. Flags:
  `--no-rsi`, `--no-stop`, `--risk 0.05`, `--start-balance`, `--csv`, `--out`.
  Outputs a per-trade log with RSI, exit type, and running drawdown.
- Recommended cadence: re-pull and re-run monthly as a regime check.
