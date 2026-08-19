"""
Detects "W" (double bottom) and "M" (double top) chart patterns on WEEKLY
charts for actively tracked stocks, using swing pivot detection.

Unlike the harmonic pattern detector, this uses pure geometry with no
heuristic confidence scoring -- just: are the two outer points (both lows
for W, both highs for M) roughly symmetric, and has price crossed the
neckline (breakout point) yet.

A confirmed breakout is only reported if it happened within the last
MAX_BREAKOUT_AGE_DAYS -- a breakout that triggered weeks ago and simply
never got picked up until now is stale, not a fresh signal. "Awaiting
Breakout" (not yet triggered) has no age concept and is always shown,
since it's a live, ongoing setup rather than a one-time event.

For each stock, reports the most recent W/M candidate formed from the
last 3 swing pivots: pattern type, whether the breakout is confirmed or
still awaited, the breakout level (neckline price), current price, how
far price still needs to move to confirm the breakout, and (for confirmed
breakouts) how many days ago it triggered.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""
import json
import math
import os
from datetime import date

import gspread
import yfinance as yf
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
PATTERNS_SHEET = "WM_Patterns"

SWING_FRACTAL_BARS = 2       # bars on each side to confirm a swing pivot
SYMMETRY_TOLERANCE_PCT = 3   # how close the two outer points must be (%) to count as a genuine W/M
WEEKS_OF_HISTORY = "3y"
MAX_BREAKOUT_AGE_DAYS = 7    # ignore confirmed breakouts older than this


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
    "Despite the name (kept for compatibility), this now returns EVERY tracked symbol, active or archived -- pattern detection should still run on archived stocks so a reversal can be caught even before sync_dashboard.py reactivates them."
    ws = spreadsheet.sheet1
    records = ws.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def find_swing_pivots(df, k=SWING_FRACTAL_BARS):
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


def find_breakout_age_days(hist, p3_index, neckline, pattern_name):
    "Walks forward from the 3rd pivot to find the bar where price FIRST crossed the neckline, and returns how many days ago that bar was. None if it hasn't happened yet."
    for i in range(p3_index + 1, len(hist)):
        close = hist["Close"].iloc[i]
        crossed = (pattern_name == "W" and close > neckline) or (pattern_name == "M" and close < neckline)
        if crossed:
            breakout_date = hist.index[i].date()
            return (date.today() - breakout_date).days
    return None


def detect_wm_pattern(symbol):
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period=WEEKS_OF_HISTORY, interval="1wk")
        if hist.empty or len(hist) < 20:
            return None

        pivots = alternate_pivots(find_swing_pivots(hist))
        if len(pivots) < 3:
            return None

        p1, p2, p3 = pivots[-3], pivots[-2], pivots[-1]
        current_price = float(hist["Close"].iloc[-1])

        if not math.isfinite(current_price) or current_price <= 0:
            return None

        p1_val = float(p1[1])
        p2_val = float(p2[1])
        p3_val = float(p3[1])

        if not all(math.isfinite(v) for v in (p1_val, p2_val, p3_val)):
            return None

        if p1[2] == "low" and p2[2] == "high" and p3[2] == "low":
            pattern_name = "W"
            outer_avg = (p1_val + p3_val) / 2
            if outer_avg <= 0:
                return None
            symmetry_pct = round(abs(p1_val - p3_val) / outer_avg * 100, 2)
            if not math.isfinite(symmetry_pct) or symmetry_pct > SYMMETRY_TOLERANCE_PCT:
                return None

            neckline = p2_val
            is_confirmed = current_price > neckline
            distance_pct = round((neckline - current_price) / current_price * 100, 2)

        elif p1[2] == "high" and p2[2] == "low" and p3[2] == "high":
            pattern_name = "M"
            outer_avg = (p1_val + p3_val) / 2
            if outer_avg <= 0:
                return None
            symmetry_pct = round(abs(p1_val - p3_val) / outer_avg * 100, 2)
            if not math.isfinite(symmetry_pct) or symmetry_pct > SYMMETRY_TOLERANCE_PCT:
                return None

            neckline = p2_val
            is_confirmed = current_price < neckline
            distance_pct = round((current_price - neckline) / current_price * 100, 2)

        else:
            return None

        if not math.isfinite(distance_pct):
            return None

        breakout_days_ago = None
        if is_confirmed:
            p3_index = p3[0]
            breakout_days_ago = find_breakout_age_days(hist, p3_index, neckline, pattern_name)
            if breakout_days_ago is not None and breakout_days_ago > MAX_BREAKOUT_AGE_DAYS:
                return None  # confirmed, but too stale to be worth showing

        status = "Breakout Confirmed" if is_confirmed else "Awaiting Breakout"

        return {
            "pattern": pattern_name,
            "status": status,
            "breakout_level": round(neckline, 2),
            "current_price": round(current_price, 2),
            "distance_to_breakout_pct": distance_pct,
            "symmetry_pct": symmetry_pct,
            "breakout_days_ago": breakout_days_ago,
        }

    except Exception as e:
        print(f"W/M detection failed for {symbol}: {e}")
        return None


def sanitize_row(row):
    "Replace any non-finite float (nan/inf/-inf) with an empty string so the payload is always valid JSON for the Sheets API."
    clean = []
    for v in row:
        if isinstance(v, float) and not math.isfinite(v):
            clean.append("")
        else:
            clean.append(v)
    return clean


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for W/M patterns (weekly).")

    header = ["symbol", "pattern", "status", "breakout_level", "current_price",
              "distance_to_breakout_pct", "symmetry_pct", "breakout_days_ago", "last_updated"]
    patterns_ws = get_or_create_sheet(spreadsheet, PATTERNS_SHEET, header)

    today_str = str(date.today())
    rows = [header]

    for symbol in active_symbols:
        result = detect_wm_pattern(symbol)
        if result:
            row = [
                symbol, result["pattern"], result["status"], result["breakout_level"],
                result["current_price"], result["distance_to_breakout_pct"],
                result["symmetry_pct"], result["breakout_days_ago"] if result["breakout_days_ago"] is not None else "",
                today_str,
            ]
            rows.append(sanitize_row(row))
            age_str = f" - {result['breakout_days_ago']}d ago" if result["breakout_days_ago"] is not None else ""
            print(f"{symbol}: {result['pattern']} pattern - {result['status']}{age_str} - "
                  f"neckline {result['breakout_level']} - symmetry {result['symmetry_pct']}%")
        else:
            rows.append([symbol, "", "No pattern found", "", "", "", "", "", today_str])

    patterns_ws.update(rows, "A1")
    print(f"Wrote W/M pattern results for {len(rows) - 1} symbols.")


if __name__ == "__main__":
    main()
