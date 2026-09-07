"""
Detects two EMA-based setups on DAILY charts for actively tracked stocks:

  1. "20/50 EMA Crossover + Volume Breakout" -- a TREND CHANGE signal: the
     20 EMA crosses above the 50 EMA on the same day volume breaks out to
     >= CROSSOVER_VOLUME_MULT x its 20-day average. This is distinct from a
     bare crossover (which only checked baseline liquidity, not an actual
     volume spike) -- the volume breakout is now a required condition, not
     just a liquidity floor.

  2. "EMA Pullback" -- a CONTINUATION signal: price is in an established
     uptrend (Close > EMA20 > EMA50), has pulled back 5-15% from its recent
     swing high, sits within tolerance of EMA20 or EMA50, and prints a
     bullish reversal candle (hammer or bullish engulfing) on above-average
     volume.

Both are ports of the logic already running in the nse-stock-chatbot repo
(core/ema_crossover.py and core/strategy.py respectively) -- same ratios,
same thresholds -- just adapted here to scan every actively tracked symbol
on a schedule and write results to a sheet, instead of running on-demand
for a single queried stock.

IMPORTANT: both signals are based on DAILY closes/EMAs, so this is
scheduled to run ONCE, AFTER market close -- an intraday run would read a
still-forming candle and could flag (or miss) a crossover/pullback that
isn't actually confirmed yet. See .github/workflows/ema-signals.yml.

Each check only asks "does the MOST RECENT daily bar qualify" -- so unlike
the divergence/retest checks elsewhere in this project, there's no
separate "days_ago" freshness field needed: True inherently means "as of
today's close."

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
EMA_SHEET = "EMA_Signals"

# ---- Shared ----
RISK_REWARD_MULT = 2.0
STOP_BUFFER_PCT = 0.5
HISTORY_PERIOD = "1y"

# ---- 20/50 EMA Crossover + Volume Breakout ----
EMA_FAST = 20
EMA_SLOW = 50
CROSSOVER_LOOKBACK_DAYS = 20      # bars used to find a recent swing low for the stop
CROSSOVER_MIN_AVG_VOLUME = 500_000
CROSSOVER_VOLUME_MULT = 1.5       # the "volume breakout" requirement -- crossover-day volume vs its 20-day average

# ---- EMA Pullback ----
SWING_LOOKBACK_DAYS = 20          # bars used to find the recent swing high
PULLBACK_MIN_PCT = 5.0
PULLBACK_MAX_PCT = 15.0
EMA_TOLERANCE_PCT = 2.0           # how close price must be to EMA20/EMA50
VOLUME_CONFIRM_MULT = 1.0         # reversal-day volume vs 20-day avg volume
PULLBACK_MIN_AVG_VOLUME = 500_000

# ---- EMA Retest (crossover happened at some point in the past, uptrend still
#      intact, price now touching the 20 or 50 EMA -- no candle-pattern or
#      volume-spike requirement, unlike EMA Pullback which needs both) ----
RETEST_CROSSOVER_LOOKBACK_DAYS = 30   # how far back to look for the crossover itself
RETEST_EMA_TOLERANCE_PCT = 2.0        # how close price must be to count as "touching" the line
RETEST_MIN_AVG_VOLUME = 500_000


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
    "Every tracked symbol regardless of status -- same fix already applied in harmonic_patterns.py/wm_patterns.py/rsi_divergence.py."
    records = spreadsheet.sheet1.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def _is_hammer(row):
    body_high = max(row["Close"], row["Open"])
    body_low = min(row["Close"], row["Open"])
    body_size = body_high - body_low
    upper_wick = row["High"] - body_high
    lower_wick = body_low - row["Low"]
    if body_size <= 0:
        return False
    return lower_wick >= body_size * 2 and upper_wick <= body_size * 0.5


def _is_bullish_engulfing(prev_row, row):
    prior_bearish = prev_row["Close"] < prev_row["Open"]
    is_bullish = row["Close"] > row["Open"]
    engulfs = row["Close"] >= prev_row["Open"] and row["Open"] <= prev_row["Close"]
    return prior_bearish and is_bullish and engulfs


def check_ema_crossover_volume(df):
    "20 EMA crosses above 50 EMA on this bar, confirmed by price, AND that same bar's volume breaks out to >= CROSSOVER_VOLUME_MULT x its 20-day average -- not just a baseline liquidity floor."
    if df is None or len(df) < EMA_SLOW + CROSSOVER_LOOKBACK_DAYS + 1:
        return None

    df = df.copy()
    df["EMA20"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["AvgVol20"] = df["Volume"].rolling(20).mean()
    df["SwingLow"] = df["Low"].rolling(CROSSOVER_LOOKBACK_DAYS).min()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = last["Close"]
    ema20, ema50 = last["EMA20"], last["EMA50"]
    prev_ema20, prev_ema50 = prev["EMA20"], prev["EMA50"]

    just_crossed_up = prev_ema20 <= prev_ema50 and ema20 > ema50
    if not just_crossed_up:
        return None

    if not (close > ema20 and close > ema50):
        return None

    avg_vol20 = last["AvgVol20"]
    if pd.isna(avg_vol20) or avg_vol20 < CROSSOVER_MIN_AVG_VOLUME:
        return None

    # The actual "volume breakout" condition -- crossover day must show a real volume spike.
    if last["Volume"] < avg_vol20 * CROSSOVER_VOLUME_MULT:
        return None

    swing_low = last["SwingLow"]
    stop_loss = min(swing_low, ema50) * (1 - STOP_BUFFER_PCT / 100)
    risk_per_share = close - stop_loss
    if risk_per_share <= 0:
        return None

    target = close + risk_per_share * RISK_REWARD_MULT

    return {
        "entry_price": round(float(close), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target": round(float(target), 2),
        "reward_risk_ratio": round(float((target - close) / risk_per_share), 2),
        "ema20": round(float(ema20), 2),
        "ema50": round(float(ema50), 2),
        "volume_ratio": round(float(last["Volume"] / avg_vol20), 2),
    }


def check_ema_pullback(df):
    "Uptrend + 5-15% pullback + price near EMA20/50 + bullish reversal candle on above-average volume. Exact port of nse-stock-chatbot's core/strategy.py."
    if df is None or len(df) < EMA_SLOW + SWING_LOOKBACK_DAYS:
        return None

    df = df.copy()
    df["EMA20"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["AvgVol20"] = df["Volume"].rolling(20).mean()
    df["SwingHigh"] = df["High"].rolling(SWING_LOOKBACK_DAYS).max()
    df["PullbackLow5"] = df["Low"].rolling(5).min()

    last = df.iloc[-1]
    prev = df.iloc[-2]

    close = last["Close"]
    ema20 = last["EMA20"]
    ema50 = last["EMA50"]

    uptrend = close > ema20 and ema20 > ema50 and close > ema50
    if not uptrend:
        return None

    swing_high = last["SwingHigh"]
    if swing_high <= 0:
        return None
    pullback_pct = (swing_high - close) / swing_high * 100
    if not (PULLBACK_MIN_PCT <= pullback_pct <= PULLBACK_MAX_PCT):
        return None

    dist_ema20_pct = abs(close - ema20) / ema20 * 100
    dist_ema50_pct = abs(close - ema50) / ema50 * 100
    near_ema = dist_ema20_pct <= EMA_TOLERANCE_PCT or dist_ema50_pct <= EMA_TOLERANCE_PCT
    if not near_ema:
        return None

    avg_vol20 = last["AvgVol20"]
    if pd.isna(avg_vol20) or avg_vol20 < PULLBACK_MIN_AVG_VOLUME:
        return None

    is_hammer = _is_hammer(last)
    is_engulfing = _is_bullish_engulfing(prev, last)
    if not (is_hammer or is_engulfing):
        return None

    if last["Volume"] < avg_vol20 * VOLUME_CONFIRM_MULT:
        return None

    pullback_low = last["PullbackLow5"]
    stop_loss = max(pullback_low, ema50) * (1 - STOP_BUFFER_PCT / 100)
    risk_per_share = close - stop_loss
    if risk_per_share <= 0:
        return None

    target_2r = close + risk_per_share * RISK_REWARD_MULT
    target = max(target_2r, swing_high)

    return {
        "entry_price": round(float(close), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target": round(float(target), 2),
        "reward_risk_ratio": round(float((target - close) / risk_per_share), 2),
        "pullback_pct": round(float(pullback_pct), 2),
        "pattern": "Hammer" if is_hammer else "Bullish Engulfing",
        "ema20": round(float(ema20), 2),
        "ema50": round(float(ema50), 2),
    }


def check_ema_retest(df):
    """20 EMA crossed above 50 EMA at SOME POINT in the recent past (not
    necessarily today), the uptrend is still intact (hasn't crossed back
    down since), and price has now pulled back to where it's actually
    touching the 20 or 50 EMA line. No candle-pattern or volume-spike
    requirement -- this is a simpler "is price retesting EMA support in an
    intact uptrend" check, distinct from EMA Pullback (needs a % pullback +
    reversal candle) and EMA Crossover+Volume Breakout (needs the crossover
    itself to be happening today)."""
    if df is None or len(df) < EMA_SLOW + RETEST_CROSSOVER_LOOKBACK_DAYS + 1:
        return None

    df = df.copy()
    df["EMA20"] = df["Close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["EMA50"] = df["Close"].ewm(span=EMA_SLOW, adjust=False).mean()
    df["AvgVol20"] = df["Volume"].rolling(20).mean()
    df["SwingLow"] = df["Low"].rolling(CROSSOVER_LOOKBACK_DAYS).min()

    last = df.iloc[-1]
    close = last["Close"]
    ema20, ema50 = last["EMA20"], last["EMA50"]

    # Uptrend must still be intact right now.
    if not (ema20 > ema50):
        return None

    avg_vol20 = last["AvgVol20"]
    if pd.isna(avg_vol20) or avg_vol20 < RETEST_MIN_AVG_VOLUME:
        return None

    # Find the most recent bullish crossover within the lookback window, and
    # confirm the trend hasn't flipped back down since then.
    ema20_arr = df["EMA20"].values
    ema50_arr = df["EMA50"].values
    n = len(df)
    days_since_cross = None
    for i in range(n - 1, max(n - 1 - RETEST_CROSSOVER_LOOKBACK_DAYS, 1) - 1, -1):
        crossed_up = ema20_arr[i - 1] <= ema50_arr[i - 1] and ema20_arr[i] > ema50_arr[i]
        if crossed_up:
            days_since_cross = (n - 1) - i
            break
        if ema20_arr[i] <= ema50_arr[i]:
            break  # trend flipped back down before we found a qualifying crossover -- stop looking

    if days_since_cross is None:
        return None

    dist_ema20_pct = abs(close - ema20) / ema20 * 100
    dist_ema50_pct = abs(close - ema50) / ema50 * 100
    touching_20 = dist_ema20_pct <= RETEST_EMA_TOLERANCE_PCT
    touching_50 = dist_ema50_pct <= RETEST_EMA_TOLERANCE_PCT
    if not (touching_20 or touching_50):
        return None

    touched = "20" if touching_20 else "50"
    swing_low = last["SwingLow"]
    stop_loss = min(swing_low, ema50) * (1 - STOP_BUFFER_PCT / 100)
    risk_per_share = close - stop_loss
    if risk_per_share <= 0:
        return None

    target = close + risk_per_share * RISK_REWARD_MULT

    return {
        "entry_price": round(float(close), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target": round(float(target), 2),
        "reward_risk_ratio": round(float((target - close) / risk_per_share), 2),
        "days_since_cross": int(days_since_cross),
        "touched_ema": touched,
        "ema20": round(float(ema20), 2),
        "ema50": round(float(ema50), 2),
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for EMA Crossover+Volume Breakout, EMA Pullback, and EMA Retest (daily).")

    header = [
        "symbol",
        "ema_cross_signal", "ema_cross_entry", "ema_cross_stop", "ema_cross_target", "ema_cross_rr",
        "ema_pullback_signal", "ema_pullback_pattern", "ema_pullback_entry", "ema_pullback_stop",
        "ema_pullback_target", "ema_pullback_rr", "ema_pullback_pct",
        "ema_retest_signal", "ema_retest_touched_ema", "ema_retest_days_since_cross", "ema_retest_entry",
        "ema_retest_stop", "ema_retest_target", "ema_retest_rr",
        "last_updated",
    ]
    ws = get_or_create_sheet(spreadsheet, EMA_SHEET, header)

    today = date.today()
    today_str = str(today)
    rows = [header]
    stale_count = 0

    for symbol in active_symbols:
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period=HISTORY_PERIOD, interval="1d")
        except Exception as e:
            print(f"{symbol}: history fetch failed ({e})")
            hist = None

        cross, pullback, retest = None, None, None
        if hist is not None and not hist.empty:
            # Hard freshness gate -- a signal only counts if it's based on TODAY's
            # confirmed bar. If Yahoo's latest bar is still yesterday's (e.g. this
            # ran before data caught up, or got triggered manually on an off day),
            # skip both checks entirely rather than reporting a stale signal as current.
            last_bar_date = hist.index[-1].date()
            if last_bar_date == today:
                cross = check_ema_crossover_volume(hist)
                pullback = check_ema_pullback(hist)
                retest = check_ema_retest(hist)
            else:
                stale_count += 1

        rows.append([
            symbol,
            bool(cross), cross["entry_price"] if cross else "", cross["stop_loss"] if cross else "",
            cross["target"] if cross else "", cross["reward_risk_ratio"] if cross else "",
            bool(pullback), pullback["pattern"] if pullback else "", pullback["entry_price"] if pullback else "",
            pullback["stop_loss"] if pullback else "", pullback["target"] if pullback else "",
            pullback["reward_risk_ratio"] if pullback else "", pullback["pullback_pct"] if pullback else "",
            bool(retest), retest["touched_ema"] if retest else "", retest["days_since_cross"] if retest else "",
            retest["entry_price"] if retest else "", retest["stop_loss"] if retest else "",
            retest["target"] if retest else "", retest["reward_risk_ratio"] if retest else "",
            today_str,
        ])

        if cross:
            print(f"{symbol}: EMA Crossover+Volume Breakout -- entry {cross['entry_price']}, target {cross['target']}")
        if pullback:
            print(f"{symbol}: EMA Pullback ({pullback['pattern']}) -- entry {pullback['entry_price']}, target {pullback['target']}")
        if retest:
            print(f"{symbol}: EMA Retest (touching {retest['touched_ema']} EMA, crossed {retest['days_since_cross']}d ago) -- entry {retest['entry_price']}, target {retest['target']}")

    ws.update(rows, "A1")
    print(f"Wrote EMA signal results for {len(rows) - 1} symbols ({stale_count} skipped for stale/non-today data).")


if __name__ == "__main__":
    main()
