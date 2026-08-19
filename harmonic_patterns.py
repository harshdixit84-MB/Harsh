"""
Detects Gartley, Bat, and Butterfly harmonic patterns on WEEKLY charts for
actively tracked stocks, using standard published Fibonacci ratio
definitions. This is an original implementation based on well-documented,
industry-standard harmonic pattern theory -- not a port of any specific
proprietary indicator (the source library behind the reference script
wasn't available).

For each stock, reports only the MOST RECENT pattern found using the last
4-5 swing pivots: which pattern, whether point D has been confirmed or is
still projected, the D price (actual or projected), and a confidence
score based on how closely the actual price ratios match the pattern's
ideal ratios.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""

import json
import os
from datetime import date

import gspread
import yfinance as yf
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
PATTERNS_SHEET = "Harmonic_Patterns"

SWING_FRACTAL_BARS = 2      # bars on each side to confirm a swing pivot
MIN_CONFIDENCE = 60         # don't report patterns scoring below this
WEEKS_OF_HISTORY = "3y"     # yfinance period for weekly data

# Standard published ratio ranges (min, ideal, max) per pattern/leg
PATTERN_DEFINITIONS = {
    "Gartley": {
        "AB_XA": (0.55, 0.618, 0.68),
        "BC_AB": (0.382, 0.618, 0.886),
        "AD_XA": (0.75, 0.786, 0.82),
        "CD_BC_min": 1.13, "CD_BC_max": 1.618,
    },
    "Bat": {
        "AB_XA": (0.35, 0.45, 0.55),
        "BC_AB": (0.382, 0.618, 0.886),
        "AD_XA": (0.85, 0.886, 0.92),
        "CD_BC_min": 1.618, "CD_BC_max": 2.618,
    },
    "Butterfly": {
        "AB_XA": (0.70, 0.786, 0.85),
        "BC_AB": (0.382, 0.618, 0.886),
        "AD_XA": (1.20, 1.27, 1.70),
        "CD_BC_min": 1.618, "CD_BC_max": 2.618,
    },
}


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
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def find_swing_pivots(df, k=SWING_FRACTAL_BARS):
    """A bar is a swing high/low if it's the highest/lowest point within
    k bars on each side -- a simple, standard fractal pivot method."""
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
    """Keeps only alternating high/low pivots, collapsing consecutive
    same-type pivots down to whichever is more extreme."""
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


def ratio_score(actual, ideal_range):
    lo, ideal, hi = ideal_range
    if actual < lo or actual > hi:
        return None
    max_dev = max(ideal - lo, hi - ideal)
    dev = abs(actual - ideal)
    return max(0, 1 - (dev / max_dev)) if max_dev > 0 else 1.0


def check_pattern(x, a, b, c, pattern_name, bull, d=None):
    defn = PATTERN_DEFINITIONS[pattern_name]

    xa = abs(a[1] - x[1])
    ab = abs(b[1] - a[1])
    bc = abs(c[1] - b[1])

    if xa == 0 or ab == 0:
        return None

    score_ab = ratio_score(ab / xa, defn["AB_XA"])
    score_bc = ratio_score(bc / ab, defn["BC_AB"])

    if score_ab is None or score_bc is None:
        return None

    if d is not None:
        cd = abs(d[1] - c[1])
        ad = abs(d[1] - a[1])
        if bc == 0:
            return None
        cd_bc = cd / bc
        ad_xa = ad / xa

        if not (defn["CD_BC_min"] <= cd_bc <= defn["CD_BC_max"]):
            return None
        score_ad = ratio_score(ad_xa, defn["AD_XA"])
        if score_ad is None:
            return None

        confidence = round(((score_ab + score_bc + score_ad) / 3) * 100)
        return {
            "pattern": pattern_name,
            "bull": bull,
            "status": "D formed - Complete",
            "d_price": round(d[1], 2),
            "confidence": confidence,
        }
    else:
        ideal_ad_xa = defn["AD_XA"][1]
        projected_d = a[1] - ideal_ad_xa * xa if bull else a[1] + ideal_ad_xa * xa

        confidence = round(((score_ab + score_bc) / 2) * 100)
        return {
            "pattern": pattern_name,
            "bull": bull,
            "status": "C formed - D projected",
            "d_price": round(projected_d, 2),
            "confidence": confidence,
        }


def detect_latest_pattern(symbol):
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period=WEEKS_OF_HISTORY, interval="1wk")
        if hist.empty or len(hist) < 30:
            return None

        pivots = alternate_pivots(find_swing_pivots(hist))
        if len(pivots) < 4:
            return None

        window = pivots[-5:] if len(pivots) >= 5 else pivots[-4:]
        if len(window) == 5:
            x, a, b, c, d = window
        else:
            x, a, b, c = window
            d = None

        bull = x[2] == "low"  # X is a low -> bullish pattern (D also a low)

        best_result = None
        for pattern_name in ["Gartley", "Bat", "Butterfly"]:
            result = check_pattern(x, a, b, c, pattern_name, bull, d)
            if result and result["confidence"] >= MIN_CONFIDENCE:
                if best_result is None or result["confidence"] > best_result["confidence"]:
                    best_result = result

        return best_result

    except Exception as e:
        print(f"Pattern detection failed for {symbol}: {e}")
        return None


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for harmonic patterns (weekly).")

    patterns_ws = get_or_create_sheet(
        spreadsheet, PATTERNS_SHEET,
        ["symbol", "pattern_name", "status", "d_price", "confidence", "last_updated"]
    )

    today_str = str(date.today())
    rows = [["symbol", "pattern_name", "status", "d_price", "confidence", "last_updated"]]

    for symbol in active_symbols:
        result = detect_latest_pattern(symbol)
        if result:
            direction = "Bullish" if result["bull"] else "Bearish"
            pattern_label = f"{direction} {result['pattern']}"
            rows.append([symbol, pattern_label, result["status"], result["d_price"], result["confidence"], today_str])
            print(f"{symbol}: {pattern_label} - {result['status']} - confidence {result['confidence']}%")
        else:
            rows.append([symbol, "", "No pattern found", "", "", today_str])

    patterns_ws.update(rows, "A1")
    print(f"Wrote pattern results for {len(rows) - 1} symbols.")


if __name__ == "__main__":
    main()
