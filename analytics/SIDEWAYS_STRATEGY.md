# Sideways Range strategy — `sideways_range.py`

Same `evaluate(df)` contract as the other strategies. Wired into the
live pipeline via `sideways_signals.py` + `.github/workflows/sideways-signals.yml`
+ `notify.py`, and has its own dashboard page (`sideways.html` +
`api/sideways.js`), the same way Downtrend Short does.

## What's different about this one
- **Trades BOTH directions.** Downtrend Short is short-only; a range
  has two tradeable edges, so this takes LONGs off support and SHORTs
  off resistance.
- **Weekly bars only**, price action + volume only -- same project
  rules as everything else here.
- **Gated behind a confirmed-or-near-confirmed sideways regime.**
  Accepts both `SIDEWAYS` and `SIDEWAYS_UNCONFIRMED` from
  `trend_regime.py` -- see "A regime-gating bug worth knowing about"
  below for why.
- **Fixed target, not a trailing stop.** This is the one deliberate
  difference from Downtrend Short's exit rule: a range's whole edge is
  "price reverts to the other boundary", so the opposite side of the
  range IS the natural target. Downtrend Short has no such natural
  target, which is why it trails instead.

## The pattern: Wyckoff Spring (long) / Upthrust (short)
The range's resistance/support each week is computed from a **rolling
prior-`RANGE_LOOKBACK_WEEKS`-week window** (not from the zigzag's "last
swing pivot ever seen" -- an early version of this file used that and it
could reference a pivot many weeks stale; see the module's docstring
for the full story).

- **Spring (LONG)**: price dips below the rolling support intra-week,
  closes back above it. Confirmed by a **Sign of Strength (SOS)**:
  within the next few weeks, breaks the prior reaction high on rising
  volume.
- **Upthrust (SHORT)**: mirror image at resistance, confirmed by a
  **Sign of Weakness (SOW)**.

`RANGE_MAX_WIDTH_PCT` (18% default) gates out ranges too wide to count
as a genuine consolidation -- if the rolling window's resistance/support
spread exceeds that, no signal fires even if a Spring/Upthrust pattern
technically triggered.

## A regime-gating bug worth knowing about (fixed, but worth understanding)
First version of this strategy required the FULLY confirmed `SIDEWAYS`
regime (matching how Downtrend Short requires fully-confirmed
`DOWNTREND`). That turned out to be self-defeating here specifically:
`trend_regime.py`'s generic volume confirmation downgrades a regime to
`*_UNCONFIRMED` when volume looks directional over the recent 4 weeks --
but a genuine SOS or SOW confirmation **is itself** a one-week
directional volume spike. So the exact volume evidence that confirms a
Spring/Upthrust pattern would also trip the generic check and downgrade
the regime, blocking the signal it had just confirmed. Fix: this module
accepts `SIDEWAYS_UNCONFIRMED` too -- it only needs the STRUCTURE to be
non-trending, since the SOS/SOW detection already carries its own more
specific volume confirmation. (Downtrend Short doesn't have this
problem: its SOW volume spike reinforces, rather than fights, the
`SELLING_PRESSURE_EXPANDING` reading that confirms `DOWNTREND`.)

## Entry / Stop / Exit
- **Entry**: next week's open (same `PENDING_ENTRY` / `ENTERED`
  two-state contract as `downtrend_short.py`).
- **Stop-loss**: just beyond the Spring/Upthrust week's own extreme
  (that week's Low for a Spring, High for an Upthrust), buffered by
  `STOP_BUFFER_PCT`.
- **Exit**: fixed target at the opposite side of the range. Also close
  immediately, regardless of target/stop, if `range_still_active()`
  returns `False` (the range has actually broken into a trend).

## Wired into the live pipeline
- `sideways_signals.py` -- mirrors `downtrend_signals.py`, writes to a
  new `Sideways_Signals` sheet tab.
- `.github/workflows/sideways-signals.yml` -- Friday 5:05 PM IST
  (staggered 5 min after the downtrend scan), + manual dispatch. Same
  `GOOGLE_SERVICE_ACCOUNT_KEY` secret, no new secret needed.
- `notify.py` -- new `sideways_range` filter (`↔️ Sideways Range
  (Spring/Upthrust)`), alerts on both LONG and SHORT, `PENDING_ENTRY`
  and `ENTERED`. 8 filters total now.
- `sideways.html` + `api/sideways.js` -- new dashboard page, button
  added to `index.html` next to Downtrend Short.

## Tested so far
- Direct unit test of `_detect_range_tests` / SOS / SOW on a hand-built
  weekly table with an unambiguous Spring and an unambiguous Upthrust:
  both detected correctly, including the confirmation week.
- Full `evaluate()` test on a daily series constructed to map exactly
  onto weekly boundaries: correctly returned `PENDING_ENTRY` LONG at
  the SOS confirmation week, `ENTERED` LONG with the right entry price
  one week later, and `PENDING_ENTRY` SHORT at the SOW confirmation
  week.
- Regression check: `downtrend_short.py` still fires correctly after
  the `trend_regime.py` edit (adding the SIDEWAYS confirm/unconfirm
  logic didn't break the existing DOWNTREND/UPTREND logic).
- False-positive check: ran `sideways_range.evaluate()` across the
  full uptrend→downtrend synthetic series from the Downtrend Short
  testing -- zero false signals across 270 weekly checks, as expected
  on genuinely trending (non-ranging) data.
- Row-rendering JS tested against mock LONG and SHORT signal shapes.

**This validates the plumbing and the pattern-detection math, not the
strategy's real-world edge.** Same next step as Downtrend Short:
trigger the workflow manually once it's on `main` and check real
output against real NSE data.

## Open items
- Parameter tuning (`RANGE_LOOKBACK_WEEKS`, `RANGE_MAX_WIDTH_PCT`,
  `RANGE_CONFIRM_LOOKAHEAD_WEEKS`, `RANGE_MIN_AVG_VOLUME`) against real
  NSE weekly data -- still first-pass defaults.
- All 3 regimes are now built (Uptrend was never built -- only
  Downtrend and Sideways were prioritized so far, per the order this
  project actually asked for).
- `range_still_active()` isn't called anywhere automatically -- same
  situation as Downtrend Short's `current_trailing_stop()`: a helper
  for a manual weekly check, or a future automation, undecided which.
