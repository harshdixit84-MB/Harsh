# Uptrend Long strategy — `uptrend_long.py`

The LONG mirror of `downtrend_short.py`. Same `evaluate(df)` contract as
every other strategy here. Wired into the live pipeline via
`uptrend_signals.py` + `.github/workflows/uptrend-signals.yml` +
`notify.py`, with its own dashboard page (`uptrend.html` +
`api/uptrend.js`).

With this, all 3 regimes are built: Uptrend, Downtrend, Sideways.

## The pattern: Sign of Strength + Last Point of Support
Wyckoff accumulation/markup schematics — the bullish mirror of the
Upthrust/SOW pair used in Downtrend Short.

1. **Sign of Strength (SOS)** — a week that breaks above the prior
   reaction high on volume HIGHER than the recent up-week average, with
   an up close. Demand is real; effort matches result. This is the
   markup leg.
2. **Last Point of Support (LPS)** — the first pullback after that SOS
   which (a) holds ABOVE the SOS leg's origin (doesn't give the leg
   back) and (b) prints volume BELOW `UPTREND_PULLBACK_VOL_MULT` × the
   SOS week's volume. Supply drying up, not real distribution.

The volume asymmetry is the whole edge: an advance on expanding volume
followed by a pullback on contracting volume is Wyckoff's classic
effort-vs-result signature of a healthy trend. A pullback on EXPANDING
volume fails the test and is skipped — that's distribution, not
support. The practical effect is that this buys the quiet pullback
rather than chasing the loud breakout.

## Entry / Stop / Exit
Mirrors Downtrend Short's decided rules:
- **Entry**: the open of the week AFTER the LPS pullback week completes
  (same `PENDING_ENTRY` / `ENTERED` two-state contract).
- **Stop-loss**: below the LPS pullback week's own low, buffered by
  `STOP_BUFFER_PCT`. If that low breaks, the "support" wasn't support
  and the setup is invalidated.
- **Exit**: no fixed target. The stop trails UP under weekly swing
  structure — call `current_trailing_stop(weekly_df)` each new
  completed week; it ratchets up as new higher swing lows confirm.
  Same reasoning as Downtrend Short: a trend has no natural target,
  unlike a range (which is why Sideways Range uses a fixed target
  instead).

## A real bug this work surfaced in `trend_regime.py` (fixed)
Building this exposed a flaw affecting **all three** strategies, not
just this one. `find_swings()` is a zigzag: it only registers a pivot
once price reverses by `ZIGZAG_THRESHOLD` (3%). A clean, steady trend
that never pulls back 3% therefore produces only ONE swing high and ONE
swing low — but `classify_structure()` required at least 2 of each to
compare HH/HL vs LH/LL, so it fell through to `SIDEWAYS`.

The effect was exactly backwards: **the cleanest, most persistent
trends were being classified as ranges**, which would have suppressed
uptrend and downtrend signals precisely on the strongest-trending
symbols. A synthetic 16-week straight-up series was classified
`SIDEWAYS_UNCONFIRMED` before the fix.

Fix: `classify_structure()` now falls back to `_persistent_direction()`
when there aren't enough pivots for a swing-sequence read — it checks
whether the large majority (default 70%) of recent weekly closes moved
one way AND the window's net move exceeds 5%. Still pure price action,
no oscillators. The returned dict now includes `resolved_by`
(`"zigzag"` or `"persistence_fallback"`) so it's auditable which path
produced the call.

## Wired into the live pipeline
- `uptrend_signals.py` → new `Uptrend_Signals` sheet tab
- `.github/workflows/uptrend-signals.yml` — Friday 5:10 PM IST
  (staggered after the sideways scan), + manual dispatch. Same
  `GOOGLE_SERVICE_ACCOUNT_KEY` secret, no new secret needed.
- `notify.py` — new `uptrend_long` filter (`📈 Uptrend Long (SOS + LPS
  pullback)`). 9 filters total now.
- `uptrend.html` + `api/uptrend.js` — dashboard page, button added to
  `index.html` next to Sideways Range.

## Tested so far
- `evaluate()` on a daily series mapped exactly onto weekly boundaries
  with an explicit SOS leg and a quiet LPS pullback: correctly returned
  `PENDING_ENTRY` at the LPS week and `ENTERED` with the right entry
  price (next week's open) one week later.
- `current_trailing_stop()` returns a sane swing-low-based level.
- **Regression tests after the `trend_regime.py` fix**: both
  `downtrend_short.py` and `sideways_range.py` still fire correctly on
  their known test cases — the fix didn't break the existing
  strategies.
- Selectivity check: ran `uptrend_long.evaluate()` across the full
  uptrend→downtrend synthetic series. It fired only in the genuine
  uptrend portion (rows 130–137, where the uptrend ends at day 130) and
  stayed silent through all 200+ checks of the downtrend portion.

**This validates the plumbing and pattern math, not real-world edge.**
Same next step as the other two: trigger the workflow and check real
NSE output.

## Open items
- Parameter tuning (`UPTREND_SOS_LOOKBACK_WEEKS`,
  `UPTREND_LPS_LOOKAHEAD_WEEKS`, `UPTREND_PULLBACK_VOL_MULT`,
  `UPTREND_MIN_AVG_VOLUME`, plus the new `_persistent_direction`
  window/threshold) against real NSE weekly data — all first-pass
  defaults.
- `current_trailing_stop()` isn't called automatically anywhere — same
  situation as Downtrend Short's: a helper for a manual weekly check or
  a future automation, undecided which.
- No cross-strategy conflict handling: nothing currently prevents, say,
  a `Downtrend_Signals` SHORT and an `Uptrend_Signals` LONG appearing
  for the same symbol in the same week if the regime is flipping. The
  regime gates make it unlikely but not impossible. Worth deciding how
  (or whether) to arbitrate.
