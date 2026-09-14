# Downtrend Short strategy — `trend_regime.py` + `downtrend_short.py`

Added alongside the existing 4 strategies. Same `evaluate(df)` contract
as `breakout.py` / `ema_crossover.py` / `strategy.py` (df in: daily
Open/High/Low/Close/Volume, most recent row last → dict or None out),
so it can be dropped into `strategy_confluence.py`'s `STRATEGIES` dict
or `ema_signals.py`-style scanning the same way the others are, once
you decide you want it wired into the live pipeline.

## What's different about this one
- **It's a SHORT setup.** The other 4 are all long.
- **It only acts on WEEKLY bars.** Every other strategy here evaluates
  daily closes; this one resamples to weekly internally first (per
  project rule: no trend decision off a single day — a trend needs at
  least a week to count) and only fires on the last *completed week*.
- **Price action + volume only.** No RSI/MACD/ADX — matches the project
  constraint. (`rsi_divergence.py` predates this constraint and is left
  as-is, not touched.)
- **Gated behind a confirmed regime.** `trend_regime.py` has to return
  `DOWNTREND` (not `DOWNTREND_UNCONFIRMED`) before the pattern scanner
  even runs. A downtrend call requires bearish swing structure (Dow
  Theory Lower-Highs/Lower-Lows) *and* at least one volume confirmation
  (a cluster of "distribution weeks", or expanding volume on down-weeks
  vs up-weeks) — structure alone doesn't clear the bar. This exists
  specifically to avoid calling a downtrend on price shape alone, which
  is the most common way naive trend-following gets whipsawed.

## The pattern: Wyckoff Upthrust + Sign of Weakness
1. **Upthrust (UT)** — price rallies to a recent swing-high resistance,
   pokes above it intra-week, closes back below it. Trapped breakout
   buyers, not real demand.
2. **Sign of Weakness (SOW)** — within the next few weeks, price breaks
   the prior reaction low on volume higher than the recent down-week
   average. Confirms the failed rally is turning into renewed selling.
3. The signal fires on the **SOW break week** — that's the short
   trigger this module reports.

This is pre-oscillator technical analysis (Wyckoff, 1930s) — chosen
specifically because it's 100% price/volume, no derived indicators.

## Entry/stop/target — first-pass default, not a final decision
Per project notes, exact entry rules are still to be discussed. To keep
this consistent with how the other 4 strategies report (`entry_price`,
`stop_loss`, `target`, `risk_per_share`, `reward_risk_ratio`), this
module ships a working default:
- **Entry**: close of the SOW break week
- **Stop**: the Upthrust week's high (+ `STOP_BUFFER_PCT`, same buffer
  the other strategies use)
- **Target**: `RISK_REWARD_MULT` applied to the resulting risk, same as
  the other strategies

Swap this logic out once the entry rule is actually decided — nothing
else in `downtrend_short.py` or `trend_regime.py` needs to change, they
just detect the setup and hand back a dict.

## Config
New tunables live in `config.py` under `# ---- Downtrend Short settings
----`: `ZIGZAG_THRESHOLD`, `DISTRIBUTION_DECLINE_THRESHOLD`,
`DISTRIBUTION_WINDOW_WEEKS`, `DISTRIBUTION_ALERT_COUNT`,
`VOLUME_TREND_WINDOW_WEEKS`, `DOWNTREND_SOW_LOOKAHEAD_WEEKS`,
`DOWNTREND_MIN_AVG_VOLUME`. All first-pass defaults — not yet validated
against real NSE weekly data. Revisit once that's available.

## Tested so far
Smoke-tested against synthetic OHLCV shaped exactly like
`strategy_confluence.py`'s `fetch_ohlcv` output (same column names,
same string-formatted date index) with a deliberate uptrend→downtrend
shift and two fake "failed rally" patterns injected. `evaluate()`
correctly stayed silent through the uptrend and the unconfirmed part of
the downtrend, then fired exactly once, on the injected SOW break week.
**This validates the plumbing, not the strategy's real-world edge** —
next step is running it against actual NSE weekly data.

## Open items (unchanged from the project notes)
- Final entry trigger, stop placement, and position sizing rule
- Uptrend and sideways counterparts (only downtrend is built so far)
- Whether/how this gets wired into `strategy_confluence.py`,
  `notify.py`, or a new GitHub Actions workflow — needs a decision on
  which symbol universe to scan and how alerts should surface
- Parameter tuning against real data
