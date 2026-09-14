"""
Sideways Range strategy -- detection logic (price action + volume only).

Gated behind a confirmed WEEKLY sideways regime (trend_regime.py).
Unlike Downtrend Short (short-only), this trades BOTH directions, since
a range by definition has two tradeable edges:

  - LONG off support, via a Spring: price dips below the established
    range's support intra-week but closes back above it -- trapped
    sellers, not real supply. Confirmed by a Sign of Strength (SOS):
    within the following weeks, price breaks above the prior reaction
    high on volume higher than the recent up-week average.

  - SHORT off resistance, via an Upthrust: price pokes above the range's
    resistance intra-week but closes back below it. Confirmed by a Sign
    of Weakness (SOW): breaks the prior reaction low on rising volume.

Both are Wyckoff Trading Range schematics -- Spring/SOS is the accepted
mirror image of Upthrust/SOW. Pre-oscillator, 100% price/volume.

IMPORTANT DESIGN NOTE: the range boundary each week is computed from a
ROLLING prior-N-week window (RANGE_LOOKBACK_WEEKS), not from the
zigzag's "last swing pivot ever seen". An early version of this file
used the latter and it could reference a swing point many weeks stale
once no new pivot had formed for a while -- flagging a "test" against a
level that was no longer the market's actual current range boundary.
The rolling window fixes that and is also more semantically correct:
a Spring/Upthrust is specifically a test of the CURRENT range edge.

ENTRY / STOP / EXIT -- decided the same way as Downtrend Short's, with
one deliberate difference on exit:

  - ENTRY: next week's open (SOW/SOS only confirms at Friday's close --
    same PENDING_ENTRY / ENTERED two-state contract as downtrend_short.py).
  - STOP-LOSS: just beyond the Spring/Upthrust extreme itself (the
    week's low for a Spring, the week's high for an Upthrust),
    buffered by STOP_BUFFER_PCT.
  - EXIT: a FIXED target at the OPPOSITE side of the range (support
    trade targets resistance, resistance trade targets support) -- NOT
    a trailing stop. A range's whole edge is "reverts to the other
    boundary", so a fixed target based on the range itself IS the
    correct exit here, unlike a trend trade which has no natural point
    to target. If the regime stops being SIDEWAYS (the range actually
    breaks), close the position regardless of target/stop -- call
    range_still_active() each week to check.
"""

import pandas as pd

from config import (
    STOP_BUFFER_PCT,
    RANGE_LOOKBACK_WEEKS,
    RANGE_MAX_WIDTH_PCT,
    RANGE_CONFIRM_LOOKAHEAD_WEEKS,
    RANGE_MIN_AVG_VOLUME,
)
from trend_regime import classify_regime


def _range_bounds(weekly_df: pd.DataFrame, upto_pos: int, lookback: int = RANGE_LOOKBACK_WEEKS):
    "Resistance/support from the `lookback` weeks strictly BEFORE upto_pos -- no lookahead."
    start = max(0, upto_pos - lookback)
    window = weekly_df.iloc[start:upto_pos]
    if window.empty:
        return None, None, None
    resistance = float(window["High"].max())
    support = float(window["Low"].min())
    width_pct = (resistance - support) / support if support else float("inf")
    return resistance, support, width_pct


def _detect_range_tests(weekly_df: pd.DataFrame, lookback: int = RANGE_LOOKBACK_WEEKS) -> pd.DataFrame:
    """
    For each week (after the first `lookback` weeks), computes the
    range from the PRIOR `lookback` weeks only, then checks whether
    THIS week is an Upthrust (poked above resistance, closed back
    below) or a Spring (poked below support, closed back above).
    """
    df = weekly_df.reset_index(drop=True).copy()
    df["is_upthrust"] = False
    df["is_spring"] = False
    df["ref_resistance"] = float("nan")
    df["ref_support"] = float("nan")
    df["range_width_pct"] = float("nan")

    for i in range(lookback, len(df)):
        resistance, support, width_pct = _range_bounds(df, i, lookback)
        if resistance is None:
            continue
        df.at[i, "ref_resistance"] = resistance
        df.at[i, "ref_support"] = support
        df.at[i, "range_width_pct"] = width_pct

        week_high = df["High"].iloc[i]
        week_low = df["Low"].iloc[i]
        week_close = df["Close"].iloc[i]

        if week_high > resistance and week_close < resistance:
            df.at[i, "is_upthrust"] = True
        if week_low < support and week_close > support:
            df.at[i, "is_spring"] = True

    return df


def _detect_sign_of_weakness(df: pd.DataFrame, lookahead_weeks: int = RANGE_CONFIRM_LOOKAHEAD_WEEKS) -> pd.DataFrame:
    "After an Upthrust: break of the prior reaction low on rising volume."
    df = df.copy()
    df["sow_confirmed"] = False
    df["sow_week_end"] = None

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
                break
    return df


def _detect_sign_of_strength(df: pd.DataFrame, lookahead_weeks: int = RANGE_CONFIRM_LOOKAHEAD_WEEKS) -> pd.DataFrame:
    "After a Spring: break of the prior reaction high on rising volume -- mirror of SOW."
    df = df.copy()
    df["sos_confirmed"] = False
    df["sos_week_end"] = None

    for pos in df.index[df["is_spring"]]:
        if pos < 2:
            continue
        prior_high = df["High"].iloc[max(0, pos - 4):pos].max()
        prior_up_weeks = df.iloc[max(0, pos - 4):pos]
        prior_up_weeks = prior_up_weeks[prior_up_weeks["Close"] > prior_up_weeks["Open"]]
        avg_up_vol = prior_up_weeks["Volume"].mean() if len(prior_up_weeks) else df["Volume"].iloc[pos]

        window_end = min(len(df), pos + 1 + lookahead_weeks)
        for j in range(pos + 1, window_end):
            if df["High"].iloc[j] > prior_high and df["Volume"].iloc[j] > avg_up_vol:
                df.at[pos, "sos_confirmed"] = True
                df.at[pos, "sos_week_end"] = df["week_end"].iloc[j]
                break
    return df


def range_still_active(daily_df: pd.DataFrame) -> bool:
    """
    Call this each new completed week while holding a position from this
    strategy. Returns False once the regime stops being SIDEWAYS (i.e.
    the range has actually broken) -- close the position regardless of
    where price sits relative to target/stop when this flips False.
    """
    regime_info = classify_regime(daily_df)
    return regime_info["regime"] in ("SIDEWAYS", "SIDEWAYS_UNCONFIRMED")


def evaluate(df: pd.DataFrame):
    """
    df must have columns: Open, High, Low, Close, Volume (most recent
    row last), same as every other strategy in this repo. Internally
    resamples to WEEKLY and needs at least ~60 daily rows.

    Returns a dict (direction LONG or SHORT) in one of two states, or
    None -- same PENDING_ENTRY / ENTERED contract as downtrend_short.py.
    """
    if df is None or len(df) < 60:
        return None

    regime_info = classify_regime(df)
    # Accept SIDEWAYS_UNCONFIRMED too, not just SIDEWAYS: the generic
    # regime-level volume check (trend_regime.py) looks for volume NOT
    # being directional over the recent 4 weeks -- but a genuine Spring
    # or Upthrust confirmation (SOS/SOW) IS a one-week directional
    # volume spike by definition. That spike would otherwise downgrade
    # the very regime this strategy needs, contradicting its own
    # confirmation signal. This module has its own more specific volume
    # check built into the SOS/SOW detection itself, so it doesn't need
    # the generic one too -- it only needs the STRUCTURE to be
    # non-trending (which both SIDEWAYS and SIDEWAYS_UNCONFIRMED share).
    if regime_info["regime"] not in ("SIDEWAYS", "SIDEWAYS_UNCONFIRMED"):
        return None

    weekly_df = regime_info["weekly_df"]
    avg_vol_8w = weekly_df["Volume"].tail(8).mean()
    if pd.isna(avg_vol_8w) or avg_vol_8w < RANGE_MIN_AVG_VOLUME:
        return None

    scored = _detect_range_tests(weekly_df)
    scored = _detect_sign_of_weakness(scored)
    scored = _detect_sign_of_strength(scored)
    n = len(scored)

    def build_result(direction, status, pattern_pos, confirm_week):
        resistance = scored["ref_resistance"].iloc[pattern_pos]
        support = scored["ref_support"].iloc[pattern_pos]
        width_pct = scored["range_width_pct"].iloc[pattern_pos]
        if pd.isna(width_pct) or width_pct > RANGE_MAX_WIDTH_PCT:
            return None

        if direction == "SHORT":
            stop_ref_price = float(scored["High"].iloc[pattern_pos])
            stop_loss = stop_ref_price * (1 + STOP_BUFFER_PCT / 100)
            target = support
        else:
            stop_ref_price = float(scored["Low"].iloc[pattern_pos])
            stop_loss = stop_ref_price * (1 - STOP_BUFFER_PCT / 100)
            target = resistance

        entry_price, entry_week, entry_note, risk_per_share = None, None, None, None
        if status == "PENDING_ENTRY":
            entry_note = "Enter at NEXT week's open. Not known yet."
        else:  # ENTERED
            entry_pos = pattern_pos + 1
            confirm_pos = list(scored["week_end"]).index(confirm_week)
            entry_pos = confirm_pos + 1
            if entry_pos >= n:
                return None
            entry_price = float(weekly_df["Open"].iloc[entry_pos])
            entry_week = str(weekly_df["week_end"].iloc[entry_pos])
            risk_per_share = (stop_loss - entry_price) if direction == "SHORT" else (entry_price - stop_loss)
            if risk_per_share <= 0:
                return None

        return {
            "pattern": f"Wyckoff Range {'Spring + Sign of Strength' if direction == 'LONG' else 'Upthrust + Sign of Weakness'}",
            "direction": direction,
            "status": status,
            "confirm_week": str(confirm_week),
            "pattern_week": str(scored["week_end"].iloc[pattern_pos]),
            "stop_loss": round(float(stop_loss), 2),
            "target": round(float(target), 2),
            "entry_price": round(entry_price, 2) if entry_price is not None else None,
            "entry_week": entry_week,
            "entry_note": entry_note,
            "risk_per_share": round(risk_per_share, 2) if risk_per_share is not None else None,
            "range_resistance": round(float(resistance), 2),
            "range_support": round(float(support), 2),
            "range_width_pct": round(float(width_pct) * 100, 2),
            "exit_rule": "Fixed target at the opposite side of the range, OR close immediately if range_still_active() turns False. No trailing.",
            "regime_structure": regime_info["structure"]["structure"],
            "volume_trend_read": regime_info["volume_trend"]["read"],
            "avg_volume_8w": int(avg_vol_8w),
            "timeframe": "weekly",
        }

    for target_pos, status in [(n - 1, "PENDING_ENTRY"), (n - 2, "ENTERED")]:
        if target_pos < 0:
            continue
        target_week_end = weekly_df["week_end"].iloc[target_pos]

        sos_rows = scored[(scored["sos_confirmed"]) & (scored["sos_week_end"] == target_week_end)]
        if not sos_rows.empty:
            spring_pos = sos_rows.index[0]
            result = build_result("LONG", status, spring_pos, target_week_end)
            if result:
                return result

        sow_rows = scored[(scored["sow_confirmed"]) & (scored["sow_week_end"] == target_week_end)]
        if not sow_rows.empty:
            upthrust_pos = sow_rows.index[0]
            result = build_result("SHORT", status, upthrust_pos, target_week_end)
            if result:
                return result

    return None
