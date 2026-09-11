"""
"Large player footprint" scan -- ported from the standalone test endpoint
(api/test-footprint-signals.js, which used Angel SmartAPI for ad-hoc manual
testing) into the same yfinance + Google Sheets pipeline every other signal
script here already uses (see ema_signals.py, delivery_value.py), so it can
run on a schedule for every tracked symbol instead of one at a time.

Six daily-bar checks, each meant to catch a different sign that size is
trading through a stock rather than retail noise:
  - volume_spike        : today's volume > 2x its 20-day average
  - absorption           : above-average volume (1.5x 10-day avg) but an
                            unusually SMALL price move -- someone is
                            absorbing supply/demand without letting price move
  - rejection_wick        : a long wick (2x the candle body) right at a
                            20-day high or low -- price was pushed to an
                            extreme and rejected within the same session
  - tight_consolidation   : the last 10 days have traded in a very narrow
                            (<4%) range -- a real base, not just "quiet"
  - sr_defense            : price is landing within 1% of a level it has
                            already touched 4+ times in the last 60 days --
                            a level the market keeps defending
  - breakout_volume       : closed above the prior 20-day high on 2x+
                            average volume -- a confirmed breakout, not a
                            thin one

volume_spike / absorption / breakout_volume actually imply size trading
through the stock; the other three are price-SHAPE patterns that co-occur
constantly during any quiet, range-bound stretch (this is exactly what the
first, unweighted version of this script showed on RELIANCE -- the same 3
generic signals fired on 17 of the last 60 days). So each symbol gets BOTH
a raw score (0-6, one point per signal, for transparency) and a weighted
score (volume-based signals count 2x, pattern signals count 1x, max 9) --
the weighted score is what should actually be used for a verdict. A
footprint_signal is only considered "real" for a day at weighted_score >= 4.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""
import json
import os
from datetime import date

import gspread
import pandas as pd
import yfinance as yf
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
FOOTPRINT_SHEET = "Footprint_Signals"
HISTORY_PERIOD = "1y"
LOOKBACK_DAYS_FOR_LATEST = 5  # how many recent sessions to report a "last real footprint" from

WEIGHTS = {
    "volume_spike": 2,
    "absorption": 2,
    "breakout_volume": 2,
    "rejection_wick": 1,
    "tight_consolidation": 1,
    "sr_defense": 1,
}
HIGH_FOOTPRINT_CUTOFF = 4

# ---- Weekly accumulation ----
WEEKLY_WINDOW = 15          # ~3 trading weeks
BASELINE_WINDOW = 60        # ~3 prior months, used as the "normal" baseline
WEEKLY_VOL_RATIO_MIN = 1.25 # 3-week avg volume vs prior baseline
WEEKLY_MIN_BIAS = 3         # net (up-volume days - down-volume days) over the window
WEEKLY_MIN_PRICE_RUN_PCT = -5   # allow a mild pullback/base, not a real decline
WEEKLY_MAX_PRICE_RUN_PCT = 12   # if price already ran further than this, it's a breakout to chase, not accumulation to catch


def get_client():
    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_sheet(spreadsheet, name, header):
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=name, rows=2000, cols=len(header) + 2)
        ws.update([header], "A1")
    return ws


def get_active_symbols(spreadsheet):
    records = spreadsheet.sheet1.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def compute_footprint(df):
    "Same math as the JS version -- returns the scored DataFrame."
    df = df.copy()
    o, h, l, c, v = df["Open"], df["High"], df["Low"], df["Close"], df["Volume"]

    avg_vol_20 = v.rolling(20).mean()
    avg_vol_10 = v.rolling(10).mean()

    df["volume_spike"] = v > avg_vol_20 * 2

    pct_move = (c - o).abs() / o
    avg_move_20 = pct_move.rolling(20).mean()
    df["absorption"] = (v > avg_vol_10 * 1.5) & (pct_move < avg_move_20 * 0.5)

    body = (c - o).abs().replace(0, 0.0001)
    upper_wick = h - pd.concat([c, o], axis=1).max(axis=1)
    lower_wick = pd.concat([c, o], axis=1).min(axis=1) - l
    long_wick = (upper_wick > 2 * body) | (lower_wick > 2 * body)
    recent_high_20 = h.rolling(20).max()
    recent_low_20 = l.rolling(20).min()
    near_high = (recent_high_20 - h).abs() / recent_high_20 <= 0.02
    near_low = (l - recent_low_20).abs() / recent_low_20 <= 0.02
    df["rejection_wick"] = long_wick & (near_high | near_low)

    r_high_10 = h.rolling(10).max()
    r_low_10 = l.rolling(10).min()
    df["tight_consolidation"] = (r_high_10 - r_low_10) / c < 0.04

    sr_touches = pd.Series(0, index=df.index)
    low_vals, high_vals = l.values, h.values
    for i in range(len(df)):
        if i < 10:
            continue
        window_start = max(0, i - 60)
        touches = 0
        for j in range(window_start, i):
            if abs(low_vals[j] - low_vals[i]) / low_vals[i] <= 0.01:
                touches += 1
            if abs(high_vals[j] - high_vals[i]) / high_vals[i] <= 0.01:
                touches += 1
        sr_touches.iloc[i] = touches
    df["sr_defense"] = sr_touches >= 4

    prior_high_20 = h.shift(1).rolling(20).max()
    df["breakout_volume"] = (c > prior_high_20) & (v > avg_vol_20 * 2)

    # ---- Weekly accumulation (the actual "big players buy over weeks, not
    # days" signal) ----
    # Everything above looks at ONE day at a time -- even weighted, a single
    # big volume day just means one day. Real accumulation shows up as a
    # SUSTAINED step-up in volume across a multi-week window, with more
    # up-volume days than down-volume days (buyers absorbing on strength AND
    # dips), while price hasn't already run away -- if it had, you'd be
    # chasing a breakout, not catching accumulation before one.
    up_vol_day = (c > o) & (v > avg_vol_20)
    down_vol_day = (c < o) & (v > avg_vol_20)

    weekly_vol_ratio = pd.Series(index=df.index, dtype=float)
    weekly_bias = pd.Series(index=df.index, dtype=float)
    weekly_price_run_pct = pd.Series(index=df.index, dtype=float)
    v_vals, c_vals = v.values, c.values
    up_vals, down_vals = up_vol_day.values, down_vol_day.values

    for i in range(len(df)):
        if i < WEEKLY_WINDOW + BASELINE_WINDOW:
            continue
        window_vol = v_vals[i - WEEKLY_WINDOW + 1: i + 1].mean()
        baseline_vol = v_vals[i - WEEKLY_WINDOW - BASELINE_WINDOW + 1: i - WEEKLY_WINDOW + 1].mean()
        weekly_vol_ratio.iloc[i] = window_vol / baseline_vol if baseline_vol else float("nan")

        up_days = up_vals[i - WEEKLY_WINDOW + 1: i + 1].sum()
        down_days = down_vals[i - WEEKLY_WINDOW + 1: i + 1].sum()
        weekly_bias.iloc[i] = up_days - down_days

        weekly_price_run_pct.iloc[i] = (c_vals[i] - c_vals[i - WEEKLY_WINDOW + 1]) / c_vals[i - WEEKLY_WINDOW + 1] * 100

    df["weekly_vol_ratio"] = weekly_vol_ratio
    df["weekly_bias"] = weekly_bias
    df["weekly_price_run_pct"] = weekly_price_run_pct
    df["weekly_accumulation"] = (
        (weekly_vol_ratio >= WEEKLY_VOL_RATIO_MIN)
        & (weekly_bias >= WEEKLY_MIN_BIAS)
        & (weekly_price_run_pct >= WEEKLY_MIN_PRICE_RUN_PCT)
        & (weekly_price_run_pct <= WEEKLY_MAX_PRICE_RUN_PCT)
    )

    signal_cols = list(WEIGHTS.keys())
    volume_cols = [k for k, w in WEIGHTS.items() if w == 2]
    pattern_cols = [k for k, w in WEIGHTS.items() if w == 1]
    df["score"] = df[signal_cols].sum(axis=1).astype(int)
    df["volume_component"] = sum(df[k].astype(int) * WEIGHTS[k] for k in volume_cols)
    df["pattern_component"] = sum(df[k].astype(int) * WEIGHTS[k] for k in pattern_cols)
    df["weighted_score"] = df["volume_component"] + df["pattern_component"]
    return df


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for footprint signals (daily + weekly accumulation).")

    header = [
        "symbol", "footprint_score", "footprint_weighted_score",
        "footprint_volume_component", "footprint_pattern_component",
        "footprint_volume_signals", "footprint_pattern_signals",
        "weekly_accumulation", "weekly_vol_ratio", "weekly_bias", "weekly_price_run_pct",
        "last_footprint_date", "last_footprint_weighted_score", "days_since_footprint",
        "last_updated",
    ]
    ws = get_or_create_sheet(spreadsheet, FOOTPRINT_SHEET, header)

    today = date.today()
    today_str = str(today)
    rows = [header]
    skipped = 0
    volume_cols = [k for k, w in WEIGHTS.items() if w == 2]
    pattern_cols = [k for k, w in WEIGHTS.items() if w == 1]

    for symbol in active_symbols:
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period=HISTORY_PERIOD, interval="1d")
        except Exception as e:
            print(f"{symbol}: history fetch failed ({e})")
            hist = None

        # Needs enough history for the weekly-accumulation baseline
        # (WEEKLY_WINDOW + BASELINE_WINDOW = 75 rows) plus warmup for the
        # rolling daily signals on top of that.
        if hist is None or len(hist) < WEEKLY_WINDOW + BASELINE_WINDOW + 20:
            skipped += 1
            rows.append([symbol, "", "", "", "", "", "", "", "", "", "", "", "", "", today_str])
            continue

        scored = compute_footprint(hist)
        last = scored.iloc[-1]
        volume_signals = [k for k in volume_cols if bool(last[k])]
        pattern_signals = [k for k in pattern_cols if bool(last[k])]

        weekly_accum = bool(last["weekly_accumulation"]) if pd.notna(last["weekly_accumulation"]) else False
        weekly_vol_ratio = round(last["weekly_vol_ratio"], 2) if pd.notna(last["weekly_vol_ratio"]) else ""
        weekly_bias = int(last["weekly_bias"]) if pd.notna(last["weekly_bias"]) else ""
        weekly_price_run = round(last["weekly_price_run_pct"], 2) if pd.notna(last["weekly_price_run_pct"]) else ""

        # Look back a few sessions for the most recent day that actually
        # cleared the "real" bar -- today alone might be quiet even if a
        # footprint showed up yesterday or two days ago.
        recent = scored.tail(LOOKBACK_DAYS_FOR_LATEST)
        real_days = recent[recent["weighted_score"] >= HIGH_FOOTPRINT_CUTOFF]
        if not real_days.empty:
            last_real = real_days.iloc[-1]
            last_real_date = last_real.name.date()
            days_since = (today - last_real_date).days
            last_real_weighted = int(last_real["weighted_score"])
        else:
            last_real_date, days_since, last_real_weighted = "", "", ""

        rows.append([
            symbol,
            int(last["score"]),
            int(last["weighted_score"]),
            int(last["volume_component"]),
            int(last["pattern_component"]),
            ", ".join(volume_signals),
            ", ".join(pattern_signals),
            weekly_accum,
            weekly_vol_ratio,
            weekly_bias,
            weekly_price_run,
            str(last_real_date) if last_real_date != "" else "",
            last_real_weighted,
            days_since,
            today_str,
        ])

        if last["weighted_score"] >= HIGH_FOOTPRINT_CUTOFF:
            print(f"{symbol}: footprint TODAY, weighted {int(last['weighted_score'])} "
                  f"(volume {int(last['volume_component'])}, pattern {int(last['pattern_component'])}) "
                  f"-- vol: {', '.join(volume_signals) or 'none'} | pattern: {', '.join(pattern_signals) or 'none'}")
        if weekly_accum:
            print(f"{symbol}: WEEKLY ACCUMULATION -- {weekly_vol_ratio}x normal volume over 3 weeks, "
                  f"bias {weekly_bias:+d} up-vs-down volume days, price {weekly_price_run:+.2f}% over that window")

    ws.update(rows, "A1")
    print(f"Wrote footprint results for {len(rows) - 1} symbols ({skipped} skipped for insufficient history).")


if __name__ == "__main__":
    main()
