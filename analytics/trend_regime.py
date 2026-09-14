"""
Weekly market-regime classifier -- price action + volume ONLY (no RSI,
MACD, ADX, or other derived oscillators; rsi_divergence.py in this repo
is the one existing exception and is left as-is).

Locked project rules this module follows:
  - No regime decision is ever made off a single day. A trend needs at
    least a week to count, so this always resamples the incoming daily
    OHLCV to WEEKLY bars before doing anything.
  - India-market oriented (built for NSE symbols via yfinance's `.NS`
    suffix, same as the rest of this repo), but the logic itself is
    generic to any OHLCV series.

Three ingredients, each pure price/volume:
  1. Structure (Dow Theory) -- zigzag swing detector finds real swing
     highs/lows (filtering weekly noise via a % reversal threshold),
     then checks whether the recent ones are Higher-Highs+Higher-Lows
     (uptrend) or Lower-Highs+Lower-Lows (downtrend).
  2. Distribution weeks -- a week closing down >= DECLINE_THRESHOLD on
     volume higher than the prior week. A cluster of these in a rolling
     window is a real warning sign, not noise. (Adapted from IBD's daily
     "distribution day" concept, scaled to weekly bars since this
     project doesn't act on daily data.)
  3. Volume trend -- is volume expanding on down-weeks vs up-weeks (or
     vice versa) over the recent window?

A DOWNTREND (or UPTREND) call requires structure AND at least one volume
confirmation. Structure alone is downgraded to *_UNCONFIRMED -- this is
deliberate, to avoid calling a trend on price shape alone, which is the
most common way naive trend-following whipsaws.
"""

import pandas as pd

from config import (
    ZIGZAG_THRESHOLD,
    DISTRIBUTION_DECLINE_THRESHOLD,
    DISTRIBUTION_WINDOW_WEEKS,
    DISTRIBUTION_ALERT_COUNT,
    VOLUME_TREND_WINDOW_WEEKS,
)


def resample_weekly(df: pd.DataFrame, week_ending: str = "FRI") -> pd.DataFrame:
    """
    df: daily OHLCV with a DatetimeIndex (or a string-formatted date
    index, as produced by strategy_confluence.py's fetch_ohlcv), columns
    Open/High/Low/Close/Volume. NSE's trading week ends Friday.
    """
    d = df.copy()
    d.index = pd.to_datetime(d.index)
    weekly = d.resample(f"W-{week_ending}").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum",
    })
    return weekly.dropna(subset=["Open", "High", "Low", "Close"]).reset_index().rename(
        columns={"index": "week_end", "Date": "week_end"}
    )


def find_swings(weekly_df: pd.DataFrame, threshold: float = ZIGZAG_THRESHOLD) -> pd.DataFrame:
    """
    Zigzag swing detector: a pivot only registers once price has
    reversed by at least `threshold` (default set in config.py) from the
    running extreme. A plain "higher than its neighbors" fractal check
    is too noise-sensitive on weekly bars and rarely produces a clean,
    unbroken run of swings -- this filters that out.
    """
    df = weekly_df.reset_index(drop=True).copy()
    df["swing_high"] = False
    df["swing_low"] = False
    n = len(df)
    if n < 3:
        return df

    trend = None
    extreme_idx = 0
    extreme_price = df["Close"].iloc[0]

    for i in range(1, n):
        price = df["Close"].iloc[i]
        if trend is None:
            change = (price - extreme_price) / extreme_price
            if change >= threshold:
                trend = "up"
                df.at[extreme_idx, "swing_low"] = True
                extreme_idx, extreme_price = i, price
            elif change <= -threshold:
                trend = "down"
                df.at[extreme_idx, "swing_high"] = True
                extreme_idx, extreme_price = i, price
        elif trend == "up":
            if price > extreme_price:
                extreme_idx, extreme_price = i, price
            elif (extreme_price - price) / extreme_price >= threshold:
                df.at[extreme_idx, "swing_high"] = True
                trend = "down"
                extreme_idx, extreme_price = i, price
        elif trend == "down":
            if price < extreme_price:
                extreme_idx, extreme_price = i, price
            elif (price - extreme_price) / extreme_price >= threshold:
                df.at[extreme_idx, "swing_low"] = True
                trend = "up"
                extreme_idx, extreme_price = i, price

    if trend == "up":
        df.at[extreme_idx, "swing_high"] = True
    elif trend == "down":
        df.at[extreme_idx, "swing_low"] = True

    return df


def classify_structure(weekly_df: pd.DataFrame, n_swings: int = 3) -> dict:
    swung = find_swings(weekly_df)
    highs = swung[swung["swing_high"]].tail(n_swings)
    lows = swung[swung["swing_low"]].tail(n_swings)

    def is_rising(s):
        return all(s.iloc[i] < s.iloc[i + 1] for i in range(len(s) - 1))

    def is_falling(s):
        return all(s.iloc[i] > s.iloc[i + 1] for i in range(len(s) - 1))

    structure = "SIDEWAYS"
    if len(highs) >= 2 and len(lows) >= 2:
        if is_rising(highs["High"]) and is_rising(lows["Low"]):
            structure = "UPTREND"
        elif is_falling(highs["High"]) and is_falling(lows["Low"]):
            structure = "DOWNTREND"

    return {
        "structure": structure,
        "recent_swing_highs": highs[["week_end", "High"]].to_dict("records"),
        "recent_swing_lows": lows[["week_end", "Low"]].to_dict("records"),
    }


def count_distribution_weeks(weekly_df: pd.DataFrame,
                              decline_threshold: float = DISTRIBUTION_DECLINE_THRESHOLD,
                              window: int = DISTRIBUTION_WINDOW_WEEKS) -> dict:
    df = weekly_df.copy()
    df["pct_change"] = df["Close"].pct_change()
    df["vol_change"] = df["Volume"].diff()
    df["is_distribution_week"] = (df["pct_change"] <= -decline_threshold) & (df["vol_change"] > 0)

    recent = df.tail(window)
    count = int(recent["is_distribution_week"].sum())
    return {
        "distribution_week_count": count,
        "window_weeks": window,
        "alert": count >= DISTRIBUTION_ALERT_COUNT,
        "distribution_weeks": recent[recent["is_distribution_week"]][["week_end", "Close", "Volume"]].to_dict("records"),
    }


def volume_trend_read(weekly_df: pd.DataFrame, window: int = VOLUME_TREND_WINDOW_WEEKS) -> dict:
    df = weekly_df.copy()
    df["pct_change"] = df["Close"].pct_change()
    recent = df.tail(window)

    up_weeks = recent[recent["pct_change"] > 0]
    down_weeks = recent[recent["pct_change"] < 0]
    avg_up_vol = up_weeks["Volume"].mean() if len(up_weeks) else float("nan")
    avg_down_vol = down_weeks["Volume"].mean() if len(down_weeks) else float("nan")

    read = "INCONCLUSIVE"
    if pd.notna(avg_up_vol) and pd.notna(avg_down_vol):
        if avg_down_vol > avg_up_vol * 1.1:
            read = "SELLING_PRESSURE_EXPANDING"
        elif avg_up_vol > avg_down_vol * 1.1:
            read = "BUYING_PRESSURE_EXPANDING"
        else:
            read = "BALANCED"

    return {"avg_up_week_volume": avg_up_vol, "avg_down_week_volume": avg_down_vol, "read": read}


def classify_regime(daily_df: pd.DataFrame) -> dict:
    """
    Entry point other modules should call. Takes the SAME daily OHLCV df
    every other strategy in this repo receives (Open/High/Low/Close/
    Volume, most recent row last) -- resamples to weekly internally, so
    callers don't need to change how they fetch data.
    """
    weekly_df = resample_weekly(daily_df)
    if len(weekly_df) < 10:
        return {"regime": "INSUFFICIENT_DATA", "reason": "need at least 10 weekly bars", "weekly_df": weekly_df}

    structure = classify_structure(weekly_df)
    distribution = count_distribution_weeks(weekly_df)
    volume_trend = volume_trend_read(weekly_df)

    regime = structure["structure"]
    if regime == "DOWNTREND":
        if not (distribution["alert"] or volume_trend["read"] == "SELLING_PRESSURE_EXPANDING"):
            regime = "DOWNTREND_UNCONFIRMED"
    if regime == "UPTREND":
        if volume_trend["read"] == "SELLING_PRESSURE_EXPANDING":
            regime = "UPTREND_UNCONFIRMED"

    return {
        "regime": regime,
        "structure": structure,
        "distribution": distribution,
        "volume_trend": volume_trend,
        "weekly_df": weekly_df,
    }
