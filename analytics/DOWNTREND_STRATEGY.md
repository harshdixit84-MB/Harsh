# Downtrend Short strategy — `trend_regime.py` + `downtrend_short.py`

Same `evaluate(df)` contract as `breakout.py` / `ema_crossover.py` /
`strategy.py` (df in: daily Open/High/Low/Close/Volume, most recent row
last → dict or None out). Now wired into the live pipeline via
`downtrend_signals.py` + `.github/workflows/downtrend-signals.yml` +
`notify.py`, the same way the other strategies are.

## What's different about this one
- **It's a SHORT setup.** The other 4 are all long.
- **It only acts on WEEKLY bars.** Every other strategy here evaluates
  daily closes; this one resamples to weekly internally first (per
  project rule: no trend decision off a single day) and only fires on
  the last 1-2 completed weeks.
- **Price action + volume only.** No RSI/MACD/ADX.
- **Gated behind a confirmed regime.** `trend_regime.py` must return
  `DOWNTREND` (not `DOWNTREND_UNCONFIRMED`) before the pattern scanner
  runs — requires bearish swing structure (Dow Theory Lower-Highs/
  Lower-Lows) *and* at least one volume confirmation (a cluster of
  "distribution weeks", or expanding volume on down-weeks vs up-weeks).

## The pattern: Wyckoff Upthrust + Sign of Weakness
1. **Upthrust (UT)** — price rallies to a recent swing-high resistance,
   pokes above it intra-week, closes back below it.
2. **Sign of Weakness (SOW)** — within the next few weeks, price breaks
   the prior reaction low on volume higher than the recent down-week
   average.
3. The signal fires on the **SOW break week**.

## Entry / Stop / Exit — DECIDED
- **Entry: next week's open.** SOW is only confirmed at Friday's close,
  so the earliest realistic execution is the following week's open —
  not the SOW week's own close (not tradeable in real time).
  Because of this one-bar-forward entry, `evaluate()` returns one of
  two states:
  - `PENDING_ENTRY` — the SOW break IS the most recently completed
    week. `entry_price` is `None` — watch for next week's open.
  - `ENTERED` — the week after the SOW break has already completed
    (only happens when called retrospectively, e.g. a backtest) — the
    actual entry price (that week's open) is reported.
- **Stop-loss: above the nearest confirmed swing-high pivot at or
  before the SOW break week** — tighter than a flat "always use the
  Upthrust high": if a smaller lower-high pivot formed between the
  Upthrust and the SOW break, that tighter level is used instead.
- **Exit: no fixed target.** The stop trails using weekly swing
  structure — call `current_trailing_stop(weekly_df)` on each new
  completed week to get the level to move the stop to, as new (lower)
  swing highs confirm during the continued downtrend. Position closes
  when price closes back above the current trailing stop. This matches
  the repo's existing pattern of updating stops via the dashboard
  (`api/set-stoploss.js`) rather than a pre-computed fixed target.

## Wired into the live pipeline
- **`downtrend_signals.py`** (top-level, mirrors `ema_signals.py`) —
  reads the active symbol list from the `Monthly Breakout Scan` sheet's
  sheet1, fetches 2y daily history per symbol via yfinance, runs
  `downtrend_short.evaluate()`, writes results to a new
  `Downtrend_Signals` worksheet.
- **`.github/workflows/downtrend-signals.yml`** — runs once a week,
  Friday 5:00 PM IST (after market close), plus a manual
  `workflow_dispatch` trigger for on-demand runs. Needs the same
  `GOOGLE_SERVICE_ACCOUNT_KEY` secret the other workflows already use —
  no new secret required.
- **`notify.py`** — reads the new `Downtrend_Signals` tab, adds a
  `downtrend_short` filter (`📉 Downtrend Short (Upthrust + SOW)`) to
  the existing Telegram alert flow. Fires for both `PENDING_ENTRY` and
  `ENTERED` states, alongside the 6 existing filters.

## Tested so far
Smoke-tested `evaluate()` (both `PENDING_ENTRY` and `ENTERED` states)
and `current_trailing_stop()` against synthetic OHLCV shaped exactly
like `fetch_ohlcv`'s / `yfinance`'s output, with a deliberate
uptrend→downtrend shift and an injected failed-rally pattern. Correctly
stayed silent through the uptrend, fired `PENDING_ENTRY` on the
injected SOW break week, and correctly resolved to `ENTERED` with the
right entry price once the following week's data was available.
Row-building logic in `downtrend_signals.py` verified against mocked
signal output (can't reach yfinance or Google Sheets from this
environment — no network access to those services here).

**This validates the plumbing, not the strategy's real-world edge.**
Next real step: trigger `.github/workflows/downtrend-signals.yml`
manually (Actions tab → "Downtrend Short signals (weekly)" → Run
workflow) once it's on `main`, and check the `Downtrend_Signals` tab
and Telegram for real output against real NSE data.

## Open items
- Parameter tuning (`ZIGZAG_THRESHOLD`, `DISTRIBUTION_*`,
  `DOWNTREND_SOW_LOOKAHEAD_WEEKS`, etc. in `config.py`) against real
  NSE weekly data — still first-pass defaults.
- Uptrend and sideways counterparts (only downtrend is built so far).
- `current_trailing_stop()` isn't yet called anywhere automatically —
  it's a helper for you (or a future automation) to check weekly while
  a position from this strategy is open. Whether that becomes its own
  scheduled job or stays a manual weekly check is undecided.
