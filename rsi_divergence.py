"""
Detects the latest RSI divergence (daily and weekly) for actively tracked
stocks. A regular bearish divergence is price making a higher swing high
while RSI makes a lower swing high; a regular bullish divergence is price
making a lower swing low while RSI makes a higher swing low. Standard,
well-documented divergence definition -- not tied to any proprietary
indicator.

For each stock, reports only the MOST RECENT divergence on each timeframe
(daily and weekly), the direction (Bullish/Bearish), and how many days ago
it formed (counted from the bar where the divergence pivot confirmed, to
today). Both timeframes are checked in a single run.

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

SWING_FRACTAL_BARS = 2       # bars on each side to confirm a swing pivot
RSI_PERIOD = 14
DAILY_HISTORY = "1y"         # yfinance period for daily data
WEEKLY_HISTORY = "3y"        # yfinance period for weekly data


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
    ws = spreadsheet.sheet1
    records = ws.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("status") == "active"})


def compute_rsi(close, period=RSI_PERIOD):
    "Standard Wilder's RSI."
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def find_swing_pivots(df, k=SWING_FRACTAL_BARS):
    "A bar is a swing high/low if it's the highest/lowest point within k bars on each side."
    pivots = []
    highs = df["High"].values
    lows = df["Low"].values
    n = len(df)

    for i in range(k, n - k):
        window_high = highs[i - k:i + k + 1]
        window_low = lows[i - k:i + k + 1]

        if highs[i] == max(window_high):
            pivots.append((i, highs[i], "high"))
        elif lows[i] == min(window_low):
            pivots.append((i, lows[i], "low"))

    return pivots


def alternate_pivots(pivots):
    "Keeps only alternating high/low pivots, collapsing consecutive same-type pivots down to whichever is more extreme."
    if not pivots:
        return []

    cleaned = [pivots[0]]
    for pt in pivots[1:]:
        last = cleaned[-1]
        if pt[2] == last[2]:
            if pt[2] == "high" and pt[1] > last[1]:
                cleaned[-1] = pt
            elif pt[2] == "low" and pt[1] < last[1]:
                cleaned[-1] = pt
        else:
            cleaned.append(pt)

    return cleaned


def detect_latest_divergence(df, rsi):
    "Compares the last two swing highs and the last two swing lows against RSI at the same bars; returns whichever divergence is most recent, or None."
    pivots = alternate_pivots(find_swing_pivots(df))
    highs = [p for p in pivots if p[2] == "high"]
    lows = [p for p in pivots if p[2] == "low"]

    candidates = []

    if len(highs) >= 2:
        i1, price1, _ = highs[-2]
        i2, price2, _ = highs[-1]
        rsi1, rsi2 = rsi.iloc[i1], rsi.iloc[i2]
        if pd.notna(rsi1) and pd.notna(rsi2) and price2 > price1 and rsi2 < rsi1:
            candidates.append({"type": "Bearish", "index": i2})

    if len(lows) >= 2:
        i1, price1, _ = lows[-2]
        i2, price2, _ = lows[-1]
        rsi1, rsi2 = rsi.iloc[i1], rsi.iloc[i2]
        if pd.notna(rsi1) and pd.notna(rsi2) and price2 < price1 and rsi2 > rsi1:
            candidates.append({"type": "Bullish", "index": i2})

    if not candidates:
        return None

    # if both a bullish and bearish candidate exist, keep whichever formed more recently
    candidates.sort(key=lambda c: c["index"])
    latest = candidates[-1]
    formed_date = df.index[latest["index"]].date()
    days_ago = (date.today() - formed_date).days
    return latest["type"], days_ago


def check_timeframe(symbol, period, interval):
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period=period, interval=interval)
        if hist.empty or len(hist) < RSI_PERIOD + 2 * SWING_FRACTAL_BARS + 2:
            return None, None

        rsi = compute_rsi(hist["Close"])
        result = detect_latest_divergence(hist, rsi)
        if result is None:
            return None, None
        return result  # (type, days_ago)

    except Exception as e:
        print(f"RSI divergence ({interval}) failed for {symbol}: {e}")
        return None, None


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for RSI divergence (daily + weekly).")

    header = ["symbol", "daily_divergence", "daily_days_ago", "weekly_divergence", "weekly_days_ago", "last_updated"]
    div_ws = get_or_create_sheet(spreadsheet, DIVERGENCE_SHEET, header)

    today_str = str(date.today())
    rows = [header]

    for symbol in active_symbols:
        daily_type, daily_days = check_timeframe(symbol, DAILY_HISTORY, "1d")
        weekly_type, weekly_days = check_timeframe(symbol, WEEKLY_HISTORY, "1wk")

        rows.append([
            symbol,
            daily_type or "",
            daily_days if daily_days is not None else "",
            weekly_type or "",
            weekly_days if weekly_days is not None else "",
            today_str,
        ])

        if daily_type or weekly_type:
            print(f"{symbol}: daily={daily_type or '-'} ({daily_days}d)  weekly={weekly_type or '-'} ({weekly_days}d)")

    div_ws.update(rows, "A1")
    print(f"Wrote RSI divergence results for {len(rows) - 1} symbols.")


if __name__ == "__main__":
    main()
