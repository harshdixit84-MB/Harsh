"""
Detects the latest RSI divergence (daily, weekly, AND hourly) for actively
tracked stocks. This is a direct port of TradingView's own official "RSI
Divergence Indicator" (the built-in Pine script, not a community script) --
same RSI period (14, on Close), same pivot lookback (5 bars left, 5 bars
right), same pivot source (the RSI line itself, not price), and the same
5-60 bar range gate between compared pivots -- run identically across all
three timeframes. Only Regular divergence is implemented (Hidden divergence
exists in the original but defaults to off there too):

  Regular bearish: RSI makes a lower pivot high, price makes a higher pivot high.
  Regular bullish: RSI makes a higher pivot low, price makes a lower pivot low.

For each stock, reports the MOST RECENT divergence on each timeframe, the
direction (Bullish/Bearish), and how stale it is -- daily/weekly report
calendar days ago (matching the original design), while hourly reports BARS
ago instead, since several hourly bars can form within a single calendar
day and a date-based staleness check wouldn't distinguish "1 hour ago" from
"6 hours ago."

Note: a pivot can only be confirmed once PIVOT_RIGHT bars have passed after
it, same lag TradingView's indicator has in real time -- so very recent bars
won't yet show a pivot even if one is about to form.

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
DIVERGENCE_SHEET = "RSI_Divergence"

RSI_PERIOD = 14
PIVOT_LEFT = 5        # bars to the left required to confirm an RSI pivot
PIVOT_RIGHT = 5        # bars to the right required to confirm an RSI pivot
RANGE_LOWER = 5        # min bars between the two compared pivots
RANGE_UPPER = 60        # max bars between the two compared pivots
DAILY_HISTORY = "1y"         # yfinance period for daily data
WEEKLY_HISTORY = "3y"        # yfinance period for weekly data
HOURLY_HISTORY = "60d"       # yfinance period for 60m data (well within Yahoo's ~730d intraday cap)
DAILY_MAX_AGE_DAYS = 7        # ignore daily divergences older than this
WEEKLY_MAX_AGE_DAYS = 14      # ignore weekly divergences older than this
HOURLY_MAX_BARS_AGE = 3       # ignore hourly divergences confirmed more than 3 hourly bars ago


def get_client():
    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_sheet(spreadsheet, title, header_row):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=200, cols=len(header_row))
        ws.update([header_row], "A1")
    return ws


def get_active_symbols(spreadsheet):
    "Despite the name (kept for compatibility), this now returns EVERY tracked symbol, active or archived -- divergence detection should still run on archived stocks so a reversal signal can be caught even before sync_dashboard.py reactivates them."
    ws = spreadsheet.sheet1
    records = ws.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def compute_rsi(close, period=RSI_PERIOD):
    "Standard Wilder's RSI."
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def find_rsi_pivots(rsi, left=PIVOT_LEFT, right=PIVOT_RIGHT):
    "Finds pivot highs/lows on the RSI line itself: a bar is a pivot if it's the max/min within `left` bars before and `right` bars after it."
    vals = rsi.values
    n = len(vals)
    highs = []
    lows = []

    for i in range(left, n - right):
        if pd.isna(vals[i]):
            continue
        window = vals[i - left:i + right + 1]
        if any(pd.isna(w) for w in window):
            continue
        if vals[i] == max(window):
            highs.append(i)
        if vals[i] == min(window):
            lows.append(i)

    return highs, lows


def _latest_divergence_candidate(df, rsi):
    "Shared logic: finds the latest confirmed regular divergence (type + bar index) from the last two RSI pivot highs and last two RSI pivot lows, gated to pivots RANGE_LOWER-RANGE_UPPER bars apart. Returns None if nothing qualifies."
    highs, lows = find_rsi_pivots(rsi)
    candidates = []

    if len(highs) >= 2:
        i1, i2 = highs[-2], highs[-1]
        if RANGE_LOWER <= (i2 - i1) <= RANGE_UPPER:
            rsi1, rsi2 = rsi.iloc[i1], rsi.iloc[i2]
            price1, price2 = df["High"].iloc[i1], df["High"].iloc[i2]
            if price2 > price1 and rsi2 < rsi1:
                candidates.append({"type": "Bearish", "index": i2})

    if len(lows) >= 2:
        i1, i2 = lows[-2], lows[-1]
        if RANGE_LOWER <= (i2 - i1) <= RANGE_UPPER:
            rsi1, rsi2 = rsi.iloc[i1], rsi.iloc[i2]
            price1, price2 = df["Low"].iloc[i1], df["Low"].iloc[i2]
            if price2 < price1 and rsi2 > rsi1:
                candidates.append({"type": "Bullish", "index": i2})

    if not candidates:
        return None

    # if both a bullish and bearish candidate exist, keep whichever formed more recently
    candidates.sort(key=lambda c: c["index"])
    return candidates[-1]


def detect_latest_divergence(df, rsi):
    "Daily/weekly variant -- reports calendar days ago (unchanged from before)."
    latest = _latest_divergence_candidate(df, rsi)
    if latest is None:
        return None
    formed_date = df.index[latest["index"]].date()
    days_ago = (date.today() - formed_date).days
    return latest["type"], days_ago


def detect_latest_divergence_bars(df, rsi):
    "Hourly variant -- reports BARS ago instead of calendar days, since several hourly bars can form within one calendar day and a date-based check can't tell '1 hour ago' from '6 hours ago'."
    latest = _latest_divergence_candidate(df, rsi)
    if latest is None:
        return None
    bars_ago = (len(df) - 1) - latest["index"]
    return latest["type"], bars_ago


def check_timeframe(symbol, period, interval, max_age_days):
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period=period, interval=interval)
        if hist.empty or len(hist) < RSI_PERIOD + RANGE_UPPER + PIVOT_LEFT + PIVOT_RIGHT:
            return None, None

        rsi = compute_rsi(hist["Close"])
        result = detect_latest_divergence(hist, rsi)
        if result is None:
            return None, None

        div_type, days_ago = result
        if days_ago > max_age_days:
            return None, None  # too stale to be worth showing

        return div_type, days_ago

    except Exception as e:
        print(f"RSI divergence ({interval}) failed for {symbol}: {e}")
        return None, None


def check_timeframe_intraday(symbol, period, interval, max_bars_age):
    "Same idea as check_timeframe, but for hourly data: uses the bars-ago variant instead of calendar-day staleness."
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period=period, interval=interval)
        if hist.empty or len(hist) < RSI_PERIOD + RANGE_UPPER + PIVOT_LEFT + PIVOT_RIGHT:
            return None, None

        rsi = compute_rsi(hist["Close"])
        result = detect_latest_divergence_bars(hist, rsi)
        if result is None:
            return None, None

        div_type, bars_ago = result
        if bars_ago > max_bars_age:
            return None, None  # too stale to be worth showing

        return div_type, bars_ago

    except Exception as e:
        print(f"RSI divergence ({interval}) failed for {symbol}: {e}")
        return None, None


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for RSI divergence (daily + weekly + hourly).")

    header = ["symbol", "daily_divergence", "daily_days_ago", "weekly_divergence", "weekly_days_ago",
              "hourly_divergence", "hourly_bars_ago", "last_updated"]
    div_ws = get_or_create_sheet(spreadsheet, DIVERGENCE_SHEET, header)

    today_str = str(date.today())
    rows = [header]

    for symbol in active_symbols:
        daily_type, daily_days = check_timeframe(symbol, DAILY_HISTORY, "1d", DAILY_MAX_AGE_DAYS)
        weekly_type, weekly_days = check_timeframe(symbol, WEEKLY_HISTORY, "1wk", WEEKLY_MAX_AGE_DAYS)
        hourly_type, hourly_bars = check_timeframe_intraday(symbol, HOURLY_HISTORY, "60m", HOURLY_MAX_BARS_AGE)

        rows.append([
            symbol,
            daily_type or "",
            daily_days if daily_days is not None else "",
            weekly_type or "",
            weekly_days if weekly_days is not None else "",
            hourly_type or "",
            hourly_bars if hourly_bars is not None else "",
            today_str,
        ])

        if daily_type or weekly_type or hourly_type:
            print(f"{symbol}: daily={daily_type or '-'} ({daily_days}d)  weekly={weekly_type or '-'} ({weekly_days}d)  hourly={hourly_type or '-'} ({hourly_bars} bars)")

    div_ws.update(rows, "A1")
    print(f"Wrote RSI divergence results for {len(rows) - 1} symbols.")


if __name__ == "__main__":
    main()
