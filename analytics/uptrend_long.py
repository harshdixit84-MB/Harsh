"""
Uptrend Long strategy -- detection logic (price action + volume only).

The LONG mirror of downtrend_short.py, gated behind a confirmed WEEKLY
uptrend (trend_regime.py). Research basis: Wyckoff Accumulation /
markup-phase schematics -- specifically Sign of Strength (SOS) followed
by a Last Point of Support (LPS), which is the accepted bullish mirror
of the Upthrust/Sign-of-Weakness pair used in Downtrend Short.

  1. Sign of Strength (SOS): a week that breaks above the prior
     reaction high on volume HIGHER than the recent up-week average --
     demand is real, effort matches result. This is the "markup leg".

  2. Last Point of Support (LPS): the first pullback after that SOS
     that holds ABOVE the SOS leg's origin (doesn't give the leg back)
     AND happens on volume BELOW the SOS leg's -- supply drying up, not
     real distribution. This is the entry setup: buying the quiet
     pullback, not chasing the loud breakout.

The volume asymmetry is the whole edge and is pure price/volume: an
advance on expanding volume followed by a pullback on contracting
volume is Wyckoff's classic "effort vs result" signature of a healthy
trend. A pullback on EXPANDING volume fails this test and is skipped --
that's distribution, not support.

ENTRY / STOP / EXIT -- same decisions as Downtrend Short, mirrored:

  - ENTRY: the open of the week AFTER the LPS pullback week completes.
    The pullback is only knowable once that week's bar is complete
    (Friday close), so the earliest realistic execution is the
    following week's open. Same two-state contract as the other
    strategies:
      * "PENDING_ENTRY" -- the LPS week IS the most recent completed
        week. Entry price isn't known yet; watch for next week's open.
      * "ENTERED" -- the week after has also completed (backtest
        context), so the actual entry price is known and reported.

  - STOP-LOSS: below the LPS pullback week's own low (buffered by
    STOP_BUFFER_PCT). If the pullback low breaks, the "support" wasn't
    support and the setup is invalidated -- tight, defined risk, same
    logic as Downtrend Short stopping above the Upthrust high.

  - EXIT: no fixed target. The stop trails UP using weekly swing
    structure -- as each new (higher) confirmed swing-low pivot forms
    while the uptrend continues, the stop moves up to just below it.
    Position closes when price closes back below the current trailing
    stop. Call current_trailing_stop(weekly_df) on each new completed
    week. Same reasoning as Downtrend Short: a trend has no natural
    target to aim at, unlike a range.
"""

import pandas as pd

from config import (
    STOP_BUFFER_PCT,
    UPTREND_SOS_LOOKBACK_WEEKS,
    UPTREND_LPS_LOOKAHEAD_WEEKS,
    UPTREND_PULLBACK_VOL_MULT,
    UPTREND_MIN_AVG_VOLUME,
)
from trend_regime import classify_regime, find_swings


def _detect_sos(weekly_df: pd.DataFrame) -> pd.DataFrame:
    """
    Flags weeks that break above the prior reaction high on volume
    higher than the recent up-week average -- the markup leg.
    """
    df = weekly_df.reset_index(drop=True).copy()
    df["is_sos"] = False
    df["sos_leg_origin"] = float("nan")
    df["sos_volume"] = float("nan")

    for i in range(4, len(df)):
        prior = df.iloc[max(0, i - 4):i]
        prior_high = prior["High"].max()
        prior_up_weeks = prior[prior["Close"] > prior["Open"]]
        avg_up_vol = prior_up_weeks["Volume"].mean() if len(prior_up_weeks) else df["Volume"].iloc[i]

        if df["High"].iloc[i] > prior_high and df["Volume"].iloc[i] > avg_up_vol \
                and df["Close"].iloc[i] > df["Open"].iloc[i]:
            df.at[i, "is_sos"] = True
            df.at[i, "sos_leg_origin"] = float(prior["Low"].min())
            df.at[i, "sos_volume"] = float(df["Volume"].iloc[i])
    return df


def _detect_lps(df: pd.DataFrame,
                lookahead_weeks: int = UPTREND_LPS_LOOKAHEAD_WEEKS,
                vol_mult: float = UPTREND_PULLBACK_VOL_MULT) -> pd.DataFrame:
    """
    For each SOS week, finds the first subsequent pullback week that
    holds above the SOS leg's origin AND prints volume below
    vol_mult * the SOS week's volume (supply drying up).
    """
    df = df.copy()
    df["lps_confirmed"] = False
    df["lps_week_end"] = None
    df["lps_pos"] = pd.NA

    for pos in df.index[df["is_sos"]]:
        leg_origin = df["sos_leg_origin"].iloc[pos]
        sos_vol = df["sos_volume"].iloc[pos]
        if pd.isna(leg_origin) or pd.isna(sos_vol):
            continue

        window_end = min(len(df), pos + 1 + lookahead_weeks)
        for j in range(pos + 1, window_end):
            is_pullback = df["Close"].iloc[j] < df["Close"].iloc[pos]
            holds_support = df["Low"].iloc[j] > leg_origin
            quiet_volume = df["Volume"].iloc[j] < sos_vol * vol_mult

            if is_pullback and holds_support and quiet_volume:
                df.at[pos, "lps_confirmed"] = True
                df.at[pos, "lps_week_end"] = df["week_end"].iloc[j]
                df.at[pos, "lps_pos"] = j
                break
    return df


def current_trailing_stop(weekly_df: pd.DataFrame):
    """
    Call this on each new completed week while in an open long from
    this strategy. Returns the swing-low-based trailing stop level to
    move the stop to (buffered by STOP_BUFFER_PCT), using the MOST
    RECENT confirmed swing-low pivot. As the uptrend continues and new
    (higher) swing lows form, this ratchets up with it. Mirror of
    downtrend_short.current_trailing_stop(). Returns None if there's no
    confirmed swing low yet.
    """
    swung = find_swings(weekly_df)
    swing_lows = swung[swung["swing_low"]]
    if swing_lows.empty:
        return None
    last_swing = swing_lows.iloc[-1]
    return {
        "trailing_stop": round(float(last_swing["Low"]) * (1 - STOP_BUFFER_PCT / 100), 2),
        "based_on_week": str(last_swing["week_end"]),
    }


def evaluate(df: pd.DataFrame):
    """
    df must have columns: Open, High, Low, Close, Volume (most recent
    row last) -- same daily df every other strategy in this repo
    receives. Internally resamples to WEEKLY and needs at least ~60
    daily rows.

    Returns a dict if the LPS pullback lands on the last completed week
    (PENDING_ENTRY) or the one before it (ENTERED), otherwise None.
    """
    if df is None or len(df) < 60:
        return None

    regime_info = classify_regime(df)
    if regime_info["regime"] != "UPTREND":
        return None

    weekly_df = regime_info["weekly_df"]
    avg_vol_8w = weekly_df["Volume"].tail(8).mean()
    if pd.isna(avg_vol_8w) or avg_vol_8w < UPTREND_MIN_AVG_VOLUME:
        return None

    scored = _detect_sos(weekly_df)
    scored = _detect_lps(scored)
    n = len(scored)

    for target_pos, status in [(n - 1, "PENDING_ENTRY"), (n - 2, "ENTERED")]:
        if target_pos < 0:
            continue
        target_week_end = weekly_df["week_end"].iloc[target_pos]

        lps_rows = scored[(scored["lps_confirmed"]) & (scored["lps_week_end"] == target_week_end)]
        if lps_rows.empty:
            continue

        sos_pos = lps_rows.index[0]
        lps_low = float(scored["Low"].iloc[target_pos])
        stop_loss = lps_low * (1 - STOP_BUFFER_PCT / 100)

        base = {
            "pattern": "Wyckoff Uptrend Long (Sign of Strength + Last Point of Support)",
            "direction": "LONG",
            "status": status,
            "lps_week": str(target_week_end),
            "sos_week": str(scored["week_end"].iloc[sos_pos]),
            "sos_leg_origin": round(float(scored["sos_leg_origin"].iloc[sos_pos]), 2),
            "stop_loss": round(stop_loss, 2),
            "exit_rule": "Trail stop using weekly swing structure -- call current_trailing_stop() each new week. No fixed target.",
            "regime_structure": regime_info["structure"]["structure"],
            "volume_trend_read": regime_info["volume_trend"]["read"],
            "avg_volume_8w": int(avg_vol_8w),
            "timeframe": "weekly",
        }

        if status == "PENDING_ENTRY":
            if stop_loss >= weekly_df["Close"].iloc[target_pos]:
                continue
            base["entry_price"] = None
            base["entry_note"] = "Enter at NEXT week's open. Not known yet."
            base["risk_per_share"] = None
        else:  # ENTERED
            entry_price = float(weekly_df["Open"].iloc[target_pos + 1])
            risk_per_share = entry_price - stop_loss
            if risk_per_share <= 0:
                continue
            base["entry_price"] = round(entry_price, 2)
            base["entry_week"] = str(weekly_df["week_end"].iloc[target_pos + 1])
            base["risk_per_share"] = round(risk_per_share, 2)

        return base

    return None
