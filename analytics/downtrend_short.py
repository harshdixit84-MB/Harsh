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
     trapped breakout buyers, not real demand. Volume on the push above
     is typically unremarkable, not expanding (effort didn't match
     result).
  2. Sign of Weakness: within the following weeks, price breaks the
     prior reaction low on volume higher than the recent down-week
     average -- confirms the failed rally is turning into renewed
     selling, not just a pause.

IMPORTANT -- entry rule is a first-pass default, not a final decision:
this returns entry_price/stop_loss/target (matching the other
strategies' output shape so it plugs into strategy_confluence.py /
notify.py the same way) using "enter short on the SOW break, stop above
the Upthrust high, target via RISK_REWARD_MULT" -- the same mechanical
pattern the other 4 strategies use for their own entries. Swap this out
once the entry rule is actually decided; nothing else in this file
should need to change.
"""

import pandas as pd

from config import (
    RISK_REWARD_MULT,
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


def evaluate(df: pd.DataFrame):
    """
    df must have columns: Open, High, Low, Close, Volume (most recent
    row last) -- same daily df every other strategy in this repo
    receives. Internally resamples to WEEKLY (never acts on daily bars
    directly, per project rule) and needs at least ~10 weekly bars.

    Returns a dict if the LAST completed week is a confirmed SOW break
    inside a confirmed downtrend, otherwise None -- mirrors the other
    strategies' "signal fired on the last bar, or nothing" contract.
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

    last_idx = len(scored) - 1
    last_row = scored.iloc[last_idx]

    # Signal fires on the week the SOW break itself happens, not the
    # upthrust week -- that's the actual short trigger.
    sow_rows = scored[(scored["sow_confirmed"]) & (scored["sow_week_end"] == weekly_df["week_end"].iloc[last_idx])]
    if sow_rows.empty:
        return None

    setup = sow_rows.iloc[0]
    entry_price = last_row["Close"]
    stop_loss = setup["High"] * (1 + STOP_BUFFER_PCT / 100)  # Upthrust week's high, buffered
    risk_per_share = stop_loss - entry_price
    if risk_per_share <= 0:
        return None

    target = entry_price - risk_per_share * RISK_REWARD_MULT

    return {
        "entry_price": round(float(entry_price), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target": round(float(target), 2),
        "risk_per_share": round(float(risk_per_share), 2),
        "reward_risk_ratio": round(float((entry_price - target) / risk_per_share), 2),
        "pattern": "Wyckoff Downtrend Short (Upthrust + Sign of Weakness)",
        "direction": "SHORT",
        "upthrust_week": str(setup["week_end"]),
        "upthrust_resistance": round(float(setup["reference_resistance"]), 2),
        "regime_structure": regime_info["structure"]["structure"],
        "distribution_week_count": regime_info["distribution"]["distribution_week_count"],
        "volume_trend_read": regime_info["volume_trend"]["read"],
        "avg_volume_8w": int(avg_vol_8w),
        "timeframe": "weekly",
    }
