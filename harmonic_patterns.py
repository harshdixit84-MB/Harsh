"""
Detects Gartley, Bat, Butterfly, and Crab harmonic patterns on WEEKLY charts
for actively tracked stocks. This is a direct port of the person's own
reference Pine Script indicator ("Harmonic Pattern Scanner (XABC -> Projected
D)") -- same pivot definition (5 bars left, 5 bars right), same zigzag
construction, same ideal-ratio-plus-tolerance bands per pattern, and the
same dual-method "Potential Reversal Zone" (PRZ) projection for D:

  1. CD as an extension of the AB leg, measured from point C
  2. D as a retracement/extension of the XA leg, measured from point A

The PRZ is the [min, max] envelope of both methods' outputs; the reported
D target is the midpoint of that zone. This matches the reference script's
"PRZ box" and dashed CD projection line exactly -- it is NOT a single
theoretical ratio point like a simpler implementation would use.

For each stock, reports the MOST RECENT setup using the last 4-5 swing
pivots (X, A, B, C, and D if it has since formed):

  "C formed - D projected": XABC ratios matched a pattern, no valid 5th
      pivot has appeared yet -- d_price is the PROJECTED PRZ midpoint, i.e.
      the target to watch for, not a price that has actually printed.
  "D formed - Complete": XABC matched AND a confirmed 5th pivot landed
      inside the PRZ (with the correct direction) -- d_price is the ACTUAL
      confirmed price.

c_days_ago tracks how long ago point C's pivot bar confirmed, so a
consuming script (notify.py) can alert only on FRESH "C formed" setups
instead of re-alerting on the same setup every run.

Note: like the reference script, a pivot can only be confirmed once
rightBars (5) bars have passed after it -- so the most recent 4-5 bars
won't yet show a pivot even if one is about to form.

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

SWING_FRACTAL_BARS = 5      # bars on each side to confirm a swing pivot (matches leftBars/rightBars in the reference script)
TOLERANCE = 0.06            # extra slack added on both sides of each ideal Fibonacci ratio (matches the reference script's default)
MIN_CONFIDENCE = 70         # don't report patterns scoring below this
WEEKS_OF_HISTORY = "3y"     # yfinance period for weekly data
PRZ_MATCH_TOLERANCE = 0.02  # 2% slack when checking whether a confirmed 5th pivot actually landed inside the projected PRZ


def _band(ideal_low, ideal_high):
    "ideal_low/ideal_high already bracket a range (e.g. Crab's CD/XA is a fixed 1.618, so low==high); tolerance is added on both outer edges."
    return round(ideal_low - TOLERANCE, 4), round(ideal_high + TOLERANCE, 4)


# Same ratio definitions as the reference Pine script, per leg:
#   ab:   AB / XA
#   bc:   BC / AB   (deliberately wide and identical across all 4 patterns in the
#                     reference script -- it isn't a pattern-differentiating leg there)
#   cdab: CD / AB, measured from point C  (one of the two PRZ methods)
#   cdxa: D's distance from A as a ratio of XA (the other PRZ method -- despite the
#                     name in the reference script, this is the classic AD/XA ratio)
PATTERN_DEFINITIONS = {
    "Gartley": {
        "ab": _band(0.618, 0.618),
        "bc": _band(0.382, 0.886),
        "cdab": _band(1.13, 1.618),
        "cdxa": _band(0.786, 0.786),
    },
    "Bat": {
        "ab": _band(0.382, 0.500),
        "bc": _band(0.382, 0.886),
        "cdab": _band(1.618, 2.618),
        "cdxa": _band(0.786, 0.886),
    },
    "Butterfly": {
        "ab": _band(0.786, 0.786),
        "bc": _band(0.382, 0.886),
        "cdab": _band(1.618, 2.618),
        "cdxa": _band(1.270, 1.618),
    },
    "Crab": {
        "ab": _band(0.382, 0.618),
        "bc": _band(0.382, 0.886),
        "cdab": _band(2.240, 3.618),
        "cdxa": _band(1.618, 1.618),
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


def _in_range(v, lo, hi):
    return lo <= v <= hi


def _band_score(actual, lo, hi):
    "0-1 score based on how close actual sits to the center of its band (not part of the reference script, kept only so the dashboard's confidence/MIN_CONFIDENCE filter still has something to sort on)."
    if actual < lo or actual > hi:
        return None
    mid = (lo + hi) / 2
    half_width = (hi - lo) / 2
    return max(0, 1 - abs(actual - mid) / half_width) if half_width > 0 else 1.0


def check_xabc(x, a, b, c, pattern_name, bull):
    "The check made the moment C confirms -- matches AB/XA and BC/AB against the pattern's bands. Doesn't require D to exist yet, same as the reference script's core detection block."
    defn = PATTERN_DEFINITIONS[pattern_name]

    xa = abs(a[1] - x[1])
    ab = abs(b[1] - a[1])
    bc = abs(c[1] - b[1])
    if xa == 0 or ab == 0:
        return None

    ab_ratio = ab / xa
    bc_ratio = bc / ab

    if not _in_range(ab_ratio, *defn["ab"]):
        return None
    if not _in_range(bc_ratio, *defn["bc"]):
        return None

    score_ab = _band_score(ab_ratio, *defn["ab"])
    score_bc = _band_score(bc_ratio, *defn["bc"])
    confidence = round(((score_ab + score_bc) / 2) * 100)

    return {"ab_ratio": ab_ratio, "bc_ratio": bc_ratio, "xa": xa, "ab": ab, "confidence": confidence}


def project_d_prz(x, a, c, xa, ab, pattern_name, bull):
    """Dual-method Potential Reversal Zone, identical to the reference script:
       1. CD as an extension of AB, measured FROM POINT C
       2. D as a retracement/extension of XA, measured FROM POINT A
    The PRZ is the envelope of both methods' outputs; the reported target
    is the midpoint."""
    defn = PATTERN_DEFINITIONS[pattern_name]
    dir_sign = -1.0 if bull else 1.0  # bullish: D below C/X-A line; bearish: D above

    cdab_min, cdab_max = defn["cdab"]
    cdxa_min, cdxa_max = defn["cdxa"]

    d_ab1 = c[1] + dir_sign * ab * cdab_min
    d_ab2 = c[1] + dir_sign * ab * cdab_max
    d_xa1 = a[1] + dir_sign * xa * cdxa_min
    d_xa2 = a[1] + dir_sign * xa * cdxa_max

    prz_high = max(d_ab1, d_ab2, d_xa1, d_xa2)
    prz_low = min(d_ab1, d_ab2, d_xa1, d_xa2)
    d_projected = (prz_high + prz_low) / 2

    return prz_low, prz_high, d_projected


def evaluate_pattern(x, a, b, c, d, pattern_name, bull):
    "Full XABC(D) evaluation for one pattern. d is the 5th pivot if one has confirmed since C, else None."
    xabc = check_xabc(x, a, b, c, pattern_name, bull)
    if xabc is None:
        return None

    prz_low, prz_high, d_projected = project_d_prz(x, a, c, xabc["xa"], xabc["ab"], pattern_name, bull)

    if d is not None:
        # A 5th pivot confirmed -- only count it as THIS pattern's D if it's
        # the expected direction (bullish D is a low, bearish D is a high)
        # AND it actually landed inside the projected PRZ (with a little slack).
        d_is_right_type = (d[2] == "low") == bull
        slack = abs(prz_high - prz_low) * PRZ_MATCH_TOLERANCE + abs(d_projected) * PRZ_MATCH_TOLERANCE
        in_zone = d_is_right_type and (prz_low - slack) <= d[1] <= (prz_high + slack)
        if in_zone:
            return {
                "pattern": pattern_name, "bull": bull,
                "status": "D formed - Complete",
                "d_price": round(d[1], 2),
                "confidence": xabc["confidence"],
                "pivot_for_freshness": d,
            }
        return None  # a 5th pivot exists but doesn't confirm this pattern -- don't report a stale projection

    return {
        "pattern": pattern_name, "bull": bull,
        "status": "C formed - D projected",
        "d_price": round(d_projected, 2),
        "confidence": xabc["confidence"],
        "pivot_for_freshness": c,
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
        for pattern_name in ["Gartley", "Bat", "Butterfly", "Crab"]:
            result = evaluate_pattern(x, a, b, c, d, pattern_name, bull)
            if result and result["confidence"] >= MIN_CONFIDENCE:
                if best_result is None or result["confidence"] > best_result["confidence"]:
                    best_result = result

        if best_result is None:
            return None

        # Freshness: days since the bar that made this result newsworthy just
        # confirmed -- C's bar for a still-projecting setup, D's bar once complete.
        freshness_pivot = best_result.pop("pivot_for_freshness")
        pivot_date = hist.index[freshness_pivot[0]].date()
        days_ago = (date.today() - pivot_date).days
        best_result["days_ago"] = days_ago

        return best_result

    except Exception as e:
        print(f"Pattern detection failed for {symbol}: {e}")
        return None


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for harmonic patterns (weekly).")

    header = ["symbol", "pattern_name", "status", "d_price", "confidence", "days_ago", "last_updated"]
    patterns_ws = get_or_create_sheet(spreadsheet, PATTERNS_SHEET, header)

    today_str = str(date.today())
    rows = [header]

    for symbol in active_symbols:
        result = detect_latest_pattern(symbol)
        if result:
            direction = "Bullish" if result["bull"] else "Bearish"
            pattern_label = f"{direction} {result['pattern']}"
            rows.append([symbol, pattern_label, result["status"], result["d_price"], result["confidence"], result["days_ago"], today_str])
            print(f"{symbol}: {pattern_label} - {result['status']} - confidence {result['confidence']}% - {result['days_ago']}d ago")
        else:
            rows.append([symbol, "", "No pattern found", "", "", "", today_str])

    patterns_ws.update(rows, "A1")
    print(f"Wrote pattern results for {len(rows) - 1} symbols.")


if __name__ == "__main__":
    main()
