"""
Downtrend Short strategy -- detection logic (price action + volume only).

Unlike the other 4 strategies in this repo (EMA Pullback, EMA Crossover,
Volume Breakout, RSI Divergence), which are all LONG setups, this is a
SHORT setup, gated behind a confirmed WEEKLY downtrend (trend_regime.py).

Research basis: Wyckoff Distribution schematics -- specifically the
Upthrust (UT) and Sign of Weakness (SOW) markers, which predate
oscillator-based TA entirely and are pure price/volume:

  1. Upthrust: price rallies back up to a recent swing-high resistance
     level, pokes above it intra-week, but closes back below it --
     trapped breakout buyers, not real demand.
  2. Sign of Weakness: within the following weeks, price breaks the
     prior reaction low on volume higher than the recent down-week
     average -- confirms the failed rally is turning into renewed
     selling, not just a pause.

ENTRY / STOP / EXIT RULE (decided, not a placeholder anymore):

  - ENTRY: the open of the week AFTER the SOW break week. The SOW break
    is only knowable once that week's bar is complete (Friday close), so
    the earliest realistic execution is the following week's open -- not
    the SOW week's own close, which isn't tradeable in real time.
    Because of this one-bar-forward entry, evaluate() can return two
    different states:
      * "PENDING_ENTRY" -- the SOW break IS the most recent completed
        week. Entry price isn't known yet; watch for next week's open
        and enter then.
      * "ENTERED" -- the week after the SOW break has already completed
        (this only happens when evaluate() is called retrospectively,
        e.g. in a backtest), so the actual entry price (that week's
        open) is known and reported.

  - STOP-LOSS: above the most recent confirmed swing-high pivot at or
    before the SOW break week (tighter than just using the Upthrust
    high -- if a smaller lower-high pivot formed between the Upthrust
    and the SOW break, that tighter level is used instead; if not, it
    falls back to the Upthrust high itself).

  - EXIT: no fixed target. The stop trails down using weekly swing
    structure -- as each new (lower) confirmed swing-high pivot forms
    while the downtrend continues, the stop moves down to just above
    it. Position closes when price closes back above the current
    trailing stop. Call `current_trailing_stop(weekly_df)` on each new
    completed week to get the level to move the stop to (matches this
    repo's existing pattern of updating stops via the dashboard --
    see api/set-stoploss.js -- rather than a fixed pre-computed target).
"""

import pandas as pd

from config import (
    STOP_BUFFER_PCT,
    DOWNTREND_MIN_AVG_VOLUME,
    DOWNTREND_SOW_LOOKAHEAD_WEEKS,
)
from trend_regime import classify_regime, find_swings


def _detect_upthrusts(weekly_df: pd.DataFrame) -> pd.DataFrame:
    df = find_swings(weekly_df).copy()
    df["is_upthrust"] = False
    df["reference_resistance"] = float("nan")

    last_swing_high = None
    for i in range(len(df)):
        if df["swing_high"].iloc[i]:
            last_swing_high = df["High"].iloc[i]
            continue
        if last_swing_high is not None:
            week_high = df["High"].iloc[i]
            week_close = df["Close"].iloc[i]
            if week_high > last_swing_high and week_close < last_swing_high:
                df.at[df.index[i], "is_upthrust"] = True
                df.at[df.index[i], "reference_resistance"] = last_swing_high
    return df


def _detect_sign_of_weakness(df: pd.DataFrame, lookahead_weeks: int = DOWNTREND_SOW_LOOKAHEAD_WEEKS) -> pd.DataFrame:
    df = df.copy()
    df["sow_confirmed"] = False
    df["sow_week_end"] = None
    df["sow_break_low"] = float("nan")
    df["sow_break_volume"] = float("nan")

    for pos in df.index[df["is_upthrust"]]:
        if pos < 2:
            continue
        prior_low = df["Low"].iloc[max(0, pos - 4):pos].min()
        prior_down_weeks = df.iloc[max(0, pos - 4):pos]
        prior_down_weeks = prior_down_weeks[prior_down_weeks["Close"] < prior_down_weeks["Open"]]
        avg_down_vol = prior_down_weeks["Volume"].mean() if len(prior_down_weeks) else df["Volume"].iloc[pos]

        window_end = min(len(df), pos + 1 + lookahead_weeks)
        for j in range(pos + 1, window_end):
            if df["Low"].iloc[j] < prior_low and df["Volume"].iloc[j] > avg_down_vol:
                df.at[pos, "sow_confirmed"] = True
                df.at[pos, "sow_week_end"] = df["week_end"].iloc[j]
                df.at[pos, "sow_break_low"] = df["Low"].iloc[j]
                df.at[pos, "sow_break_volume"] = df["Volume"].iloc[j]
                break
    return df


def _nearest_swing_high_at_or_before(scored: pd.DataFrame, upto_pos: int):
    """
    Most recent confirmed swing-high pivot at or before `upto_pos`. This
    is what makes the stop tighter than a flat "always use the Upthrust
    high": if a smaller lower-high pivot formed between the Upthrust and
    the SOW break, it'll be the most recent one and gets used instead.
    """
    candidates = scored.iloc[:upto_pos + 1]
    swing_highs = candidates[candidates["swing_high"]]
    if swing_highs.empty:
        return None
    return swing_highs.iloc[-1]


def current_trailing_stop(weekly_df: pd.DataFrame):
    """
    Call this on each new completed week while in an open short from
    this strategy. Returns the swing-high-based trailing stop level to
    move the stop to (buffered by STOP_BUFFER_PCT), using the MOST
    RECENT confirmed swing-high pivot in the data. As the downtrend
    continues and new (lower) swing highs form, this level ratchets
    down with it -- classic weekly-structure trailing, no oscillator
    involved. Returns None if there's no confirmed swing high yet.
    """
    swung = find_swings(weekly_df)
    swing_highs = swung[swung["swing_high"]]
    if swing_highs.empty:
        return None
    last_swing = swing_highs.iloc[-1]
    return {
        "trailing_stop": round(float(last_swing["High"]) * (1 + STOP_BUFFER_PCT / 100), 2),
        "based_on_week": str(last_swing["week_end"]),
    }


def evaluate(df: pd.DataFrame):
    """
    df must have columns: Open, High, Low, Close, Volume (most recent
    row last) -- same daily df every other strategy in this repo
    receives. Internally resamples to WEEKLY and needs at least ~60
    daily rows (~10+ weekly bars).

    Returns a dict in one of two states, or None:
      - status="PENDING_ENTRY": SOW break is the most recent completed
        week. entry_price is None -- watch for next week's open.
      - status="ENTERED": the week after the SOW break has already
        completed (backtest context) -- entry_price is that week's
        actual open.
      Returns None if no qualifying setup is on the last 1-2 weekly bars.
    """
    if df is None or len(df) < 60:
        return None

    regime_info = classify_regime(df)
    if regime_info["regime"] != "DOWNTREND":
        return None

    weekly_df = regime_info["weekly_df"]
    avg_vol_8w = weekly_df["Volume"].tail(8).mean()
    if pd.isna(avg_vol_8w) or avg_vol_8w < DOWNTREND_MIN_AVG_VOLUME:
        return None

    upthrusts = _detect_upthrusts(weekly_df)
    scored = _detect_sign_of_weakness(upthrusts)
    n = len(scored)

    # Find an upthrust whose SOW break lands on the last row (PENDING)
    # or the second-to-last row (ENTERED -- next week's open is known).
    for target_pos, status in [(n - 1, "PENDING_ENTRY"), (n - 2, "ENTERED")]:
        if target_pos < 0:
            continue
        target_week_end = weekly_df["week_end"].iloc[target_pos]
        sow_rows = scored[(scored["sow_confirmed"]) & (scored["sow_week_end"] == target_week_end)]
        if sow_rows.empty:
            continue

        upthrust_pos = sow_rows.index[0]
        stop_ref = _nearest_swing_high_at_or_before(scored, target_pos)
        if stop_ref is None:
            continue
        stop_loss = float(stop_ref["High"]) * (1 + STOP_BUFFER_PCT / 100)

        base = {
            "pattern": "Wyckoff Downtrend Short (Upthrust + Sign of Weakness)",
            "direction": "SHORT",
            "status": status,
            "sow_break_week": str(target_week_end),
            "upthrust_week": str(scored["week_end"].iloc[upthrust_pos]),
            "upthrust_resistance": round(float(scored["reference_resistance"].iloc[upthrust_pos]), 2),
            "stop_loss": round(stop_loss, 2),
            "stop_reference_week": str(stop_ref["week_end"]),
            "exit_rule": "Trail stop using weekly swing structure -- call current_trailing_stop() each new week. No fixed target.",
            "regime_structure": regime_info["structure"]["structure"],
            "distribution_week_count": regime_info["distribution"]["distribution_week_count"],
            "volume_trend_read": regime_info["volume_trend"]["read"],
            "avg_volume_8w": int(avg_vol_8w),
            "timeframe": "weekly",
        }

        if status == "PENDING_ENTRY":
            if stop_loss <= weekly_df["Close"].iloc[target_pos]:
                continue
            base["entry_price"] = None
            base["entry_note"] = "Enter at NEXT week's open. Not known yet."
            base["risk_per_share"] = None
        else:  # ENTERED
            entry_price = float(weekly_df["Open"].iloc[target_pos + 1])
            risk_per_share = stop_loss - entry_price
            if risk_per_share <= 0:
                continue
            base["entry_price"] = round(entry_price, 2)
            base["entry_week"] = str(weekly_df["week_end"].iloc[target_pos + 1])
            base["risk_per_share"] = round(risk_per_share, 2)

        return base

    return None
