"""
XABCD harmonic pattern scan.

Identifies the classic X-A-B-C-D harmonic structure (Gartley, Bat,
Butterfly, Crab, or a generic AB=CD) from CONFIRMED swing points only --
using a percentage zigzag, so point D is never the still-forming extreme
of today's price, only a point price has already reversed away from by
ZIGZAG_PCT. That's what "confirm point D" means here: D is only reported
once the market has already turned away from it, not projected in
advance.

IMPORTANT -- bullish/bearish naming, checked deliberately because the
Pine Script version had these two swapped:
  - BULLISH pattern: X, B, D are swing LOWS; A, C are swing HIGHS.
    D is the lowest point -- the pattern says "buy here, expect a move
    UP toward C/A". This is bullish because the TRADE is a buy.
  - BEARISH pattern: X, B, D are swing HIGHS; A, C are swing LOWS.
    D is the highest point -- the pattern says "sell here, expect a move
    DOWN toward C/A". This is bearish because the TRADE is a sell.
  Direction is derived directly from the pivot TYPE at D (low -> Bullish,
  high -> Bearish), not from any separately-tracked flag -- so the two
  can't drift out of sync with each other the way they did before.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""
import json
import os
from datetime import date

import gspread
import yfinance as yf
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
HARMONIC_SHEET = "Harmonic_Patterns"
HISTORY_PERIOD = "2y"

ZIGZAG_PCT = 5.0          # minimum % reversal to confirm a swing pivot
STOP_BUFFER_PCT = 1.0     # stop placed this far beyond X, on the invalidation side
FIB_TOLERANCE = 0.06      # +/- tolerance around each Fibonacci ratio check

# (name, AB/XA range, BC/AB range, CD/BC range, AD/XA range)
PATTERN_DEFINITIONS = [
    ("Gartley",   (0.618 - FIB_TOLERANCE, 0.618 + FIB_TOLERANCE),
                  (0.382, 0.886),
                  (1.13, 1.618),
                  (0.786 - FIB_TOLERANCE, 0.786 + FIB_TOLERANCE)),
    ("Bat",       (0.382, 0.50 + FIB_TOLERANCE),
                  (0.382, 0.886),
                  (1.618, 2.618),
                  (0.886 - FIB_TOLERANCE, 0.886 + FIB_TOLERANCE)),
    ("Butterfly", (0.786 - FIB_TOLERANCE, 0.786 + FIB_TOLERANCE),
                  (0.382, 0.886),
                  (1.618, 2.24),
                  (1.27, 1.618 + FIB_TOLERANCE)),
    ("Crab",      (0.382, 0.618 + FIB_TOLERANCE),
                  (0.382, 0.886),
                  (2.24, 3.618),
                  (1.618 - FIB_TOLERANCE, 1.618 + FIB_TOLERANCE)),
]


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


def _zigzag_pivots(df, pct_threshold):
    """
    Returns a list of (date, price, kind) for CONFIRMED pivots only, in
    chronological order, strictly alternating kind ('high'/'low'). The
    still-forming extreme at the end of the series is deliberately
    excluded -- that's what makes every pivot returned here "confirmed".
    The very first pivot is the series' starting anchor point (X), typed
    opposite to whichever direction price first confirmed a move in.
    """
    highs = df["High"].values
    lows = df["Low"].values
    dates = df.index
    n = len(df)

    pivots = []
    trend = None
    anchor_idx = 0
    anchor_price = float(df["Close"].iloc[0])
    extreme_idx, extreme_price = anchor_idx, anchor_price

    for i in range(1, n):
        high, low = highs[i], lows[i]

        if trend is None:
            if high >= anchor_price * (1 + pct_threshold / 100):
                trend = "up"
                pivots.append((dates[anchor_idx], anchor_price, "low"))
                extreme_idx, extreme_price = i, high
            elif low <= anchor_price * (1 - pct_threshold / 100):
                trend = "down"
                pivots.append((dates[anchor_idx], anchor_price, "high"))
                extreme_idx, extreme_price = i, low
            continue

        if trend == "up":
            if high > extreme_price:
                extreme_idx, extreme_price = i, high
            elif low <= extreme_price * (1 - pct_threshold / 100):
                pivots.append((dates[extreme_idx], extreme_price, "high"))
                trend = "down"
                extreme_idx, extreme_price = i, low
        else:
            if low < extreme_price:
                extreme_idx, extreme_price = i, low
            elif high >= extreme_price * (1 + pct_threshold / 100):
                pivots.append((dates[extreme_idx], extreme_price, "low"))
                trend = "up"
                extreme_idx, extreme_price = i, high

    return pivots


def _in_range(value, bounds):
    lo, hi = bounds
    return lo <= value <= hi


def _classify_pattern(x, a, b, c, d):
    "x, a, b, c, d are prices (floats). Returns the matched pattern name, or None."
    xa = abs(a - x)
    ab = abs(b - a)
    bc = abs(c - b)
    cd = abs(d - c)
    ad = abs(d - a)
    if xa == 0 or ab == 0 or bc == 0:
        return None

    ab_xa = ab / xa
    bc_ab = bc / ab
    cd_bc = cd / bc
    ad_xa = ad / xa

    for name, ab_range, bc_range, cd_range, ad_range in PATTERN_DEFINITIONS:
        if _in_range(ab_xa, ab_range) and _in_range(bc_ab, bc_range) and _in_range(cd_bc, cd_range) and _in_range(ad_xa, ad_range):
            return name

    # Generic AB=CD fallback: the CD leg is roughly the same size as AB
    # (a looser, much more common pattern than the 4 named ones above).
    if 0.8 <= (cd / ab) <= 1.27:
        return "AB=CD"

    return None


def find_harmonic_setup(df):
    """
    Looks at the last 5 confirmed zigzag pivots for a completed XABCD
    pattern. Returns a dict if one is found, else None.
    """
    pivots = _zigzag_pivots(df, ZIGZAG_PCT)
    if len(pivots) < 5:
        return None

    x_date, x_price, x_kind = pivots[-5]
    a_date, a_price, a_kind = pivots[-4]
    b_date, b_price, b_kind = pivots[-3]
    c_date, c_price, c_kind = pivots[-2]
    d_date, d_price, d_kind = pivots[-1]

    pattern_name = _classify_pattern(x_price, a_price, b_price, c_price, d_price)
    if pattern_name is None:
        return None

    # Direction comes directly from D's pivot kind -- see the module
    # docstring for why this is deliberately not a separate flag.
    if d_kind == "low":
        direction = "Bullish"
        stop_loss = x_price * (1 - STOP_BUFFER_PCT / 100)
        target_1 = d_price + 0.382 * (a_price - d_price)
        target_2 = d_price + 0.618 * (a_price - d_price)
        target_3 = a_price
    else:
        direction = "Bearish"
        stop_loss = x_price * (1 + STOP_BUFFER_PCT / 100)
        target_1 = d_price - 0.382 * (d_price - a_price)
        target_2 = d_price - 0.618 * (d_price - a_price)
        target_3 = a_price

    return {
        "pattern": pattern_name,
        "direction": direction,
        "x_date": str(x_date.date()), "x_price": round(float(x_price), 2),
        "a_date": str(a_date.date()), "a_price": round(float(a_price), 2),
        "b_date": str(b_date.date()), "b_price": round(float(b_price), 2),
        "c_date": str(c_date.date()), "c_price": round(float(c_price), 2),
        "d_date": str(d_date.date()), "d_price": round(float(d_price), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target_1": round(float(target_1), 2),
        "target_2": round(float(target_2), 2),
        "target_3": round(float(target_3), 2),
        "days_since_d": (date.today() - d_date.date()).days,
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for confirmed XABCD harmonic patterns.")

    header = [
        "symbol", "pattern", "direction",
        "x_date", "x_price", "a_date", "a_price", "b_date", "b_price",
        "c_date", "c_price", "d_date", "d_price",
        "stop_loss", "target_1", "target_2", "target_3",
        "days_since_d", "last_updated",
    ]
    ws = get_or_create_sheet(spreadsheet, HARMONIC_SHEET, header)

    today_str = str(date.today())
    rows = [header]
    found = 0

    for symbol in active_symbols:
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period=HISTORY_PERIOD, interval="1d")
        except Exception as e:
            print(f"{symbol}: history fetch failed ({e})")
            hist = None

        if hist is None or len(hist) < 60:
            rows.append([symbol] + [""] * (len(header) - 2) + [today_str])
            continue

        setup = find_harmonic_setup(hist)
        if setup is None:
            rows.append([symbol] + [""] * (len(header) - 2) + [today_str])
            continue

        found += 1
        rows.append([
            symbol, setup["pattern"], setup["direction"],
            setup["x_date"], setup["x_price"], setup["a_date"], setup["a_price"],
            setup["b_date"], setup["b_price"], setup["c_date"], setup["c_price"],
            setup["d_date"], setup["d_price"],
            setup["stop_loss"], setup["target_1"], setup["target_2"], setup["target_3"],
            setup["days_since_d"], today_str,
        ])
        print(f"{symbol}: {setup['direction']} {setup['pattern']} -- D confirmed {setup['days_since_d']}d ago at {setup['d_price']}, "
              f"targets {setup['target_1']}/{setup['target_2']}/{setup['target_3']}, stop {setup['stop_loss']}")

    ws.update(rows, "A1")
    print(f"Wrote harmonic pattern results for {len(rows) - 1} symbols ({found} with a confirmed pattern).")


if __name__ == "__main__":
    main()
