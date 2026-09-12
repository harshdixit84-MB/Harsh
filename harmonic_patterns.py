"""
XABCD harmonic pattern scan -- PREDICTIVE version.

The moment C confirms (via a percentage zigzag), this projects the
Potential Reversal Zone (PRZ) for D BEFORE D has formed -- using the
already-known X-A-B-C ratios to narrow down which pattern(s) (Gartley,
Bat, Butterfly, Crab) are still geometrically possible, then projecting
each one's own Fibonacci CD/BC and AD/XA ranges forward from C and A.
This is the standard way harmonic patterns are actually traded: you plan
the zone while price is still moving toward it, not after D has already
completed and reversed (which would mean finding out only after the move
is over).

IMPORTANT -- bullish/bearish naming, checked deliberately because the
Pine Script version had these two swapped:
  - BULLISH pattern: X, B are swing LOWS; A, C are swing HIGHS. D is
    projected to be a LOW -- "buy once price enters the PRZ, expect a
    move UP toward C/A". Bullish because the TRADE is a buy.
  - BEARISH pattern: X, B are swing HIGHS; A, C are swing LOWS. D is
    projected to be a HIGH -- "sell once price enters the PRZ, expect a
    move DOWN toward C/A". Bearish because the TRADE is a sell.
  Direction is derived directly from C's pivot type (C is a high -> D
  will be a low -> Bullish; C is a low -> D will be a high -> Bearish),
  never from a separately-tracked flag, so the two can't drift apart.

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


def find_predictive_setup(df):
    """
    Uses the last 4 CONFIRMED zigzag pivots -- X, A, B, C -- to PROJECT
    the Potential Reversal Zone (PRZ) for D, before D has formed at all.

    This is the actual way harmonic patterns are traded: the moment C
    confirms, you already know the AB/XA and BC/AB ratios, which narrows
    down which pattern(s) (Gartley/Bat/Butterfly/Crab) are still
    geometrically possible. Each surviving pattern's own CD/BC and AD/XA
    Fibonacci ranges then project a price zone for where D should
    complete -- while price is still moving from C, not after it has
    already reversed. Waiting for D to fully confirm (the old version of
    this script) means finding out about the move only after it's over.

    Returns a dict if at least one pattern is still geometrically valid
    given X-A-B-C, else None.
    """
    pivots = _zigzag_pivots(df, ZIGZAG_PCT)
    if len(pivots) < 4:
        return None

    x_date, x_price, x_kind = pivots[-4]
    a_date, a_price, a_kind = pivots[-3]
    b_date, b_price, b_kind = pivots[-2]
    c_date, c_price, c_kind = pivots[-1]

    xa = abs(a_price - x_price)
    ab = abs(b_price - a_price)
    bc = abs(c_price - b_price)
    if xa == 0 or ab == 0 or bc == 0:
        return None

    ab_xa = ab / xa
    bc_ab = bc / ab

    # D continues the strict alternation opposite of C: if C is a high,
    # D will be a low (Bullish -- buy at D, expect a move up). If C is a
    # low, D will be a high (Bearish -- sell at D, expect a move down).
    # Same deliberate direct-from-pivot-type derivation as before, so
    # this can't end up swapped either.
    direction = "Bullish" if c_kind == "high" else "Bearish"

    best = None  # tightest (smallest-range) PRZ among still-valid patterns
    for name, ab_range, bc_range, cd_range, ad_range in PATTERN_DEFINITIONS:
        if not (_in_range(ab_xa, ab_range) and _in_range(bc_ab, bc_range)):
            continue  # this pattern type is already ruled out by X-A-B-C alone

        cd_lo, cd_hi = cd_range
        ad_lo, ad_hi = ad_range

        if direction == "Bullish":
            # CD leg projects DOWN from C; AD leg projects DOWN from A.
            d_from_cd = [c_price - cd_lo * bc, c_price - cd_hi * bc]
            d_from_ad = [a_price - ad_lo * xa, a_price - ad_hi * xa]
        else:
            # CD leg projects UP from C; AD leg projects UP from A.
            d_from_cd = [c_price + cd_lo * bc, c_price + cd_hi * bc]
            d_from_ad = [a_price + ad_lo * xa, a_price + ad_hi * xa]

        prz_candidates = d_from_cd + d_from_ad
        prz_low, prz_high = min(prz_candidates), max(prz_candidates)

        candidate = {
            "pattern": name,
            "direction": direction,
            "prz_low": prz_low,
            "prz_high": prz_high,
            "x_date": x_date, "x_price": x_price,
            "a_date": a_date, "a_price": a_price,
            "b_date": b_date, "b_price": b_price,
            "c_date": c_date, "c_price": c_price,
        }
        if best is None or (prz_high - prz_low) < (best["prz_high"] - best["prz_low"]):
            best = candidate

    if best is None:
        return None

    prz_mid = (best["prz_low"] + best["prz_high"]) / 2
    current_price = float(df["Close"].iloc[-1])

    if best["direction"] == "Bullish":
        stop_loss = best["x_price"] * (1 - STOP_BUFFER_PCT / 100)
        target_1 = prz_mid + 0.382 * (best["a_price"] - prz_mid)
        target_2 = prz_mid + 0.618 * (best["a_price"] - prz_mid)
        target_3 = best["a_price"]
        # How far current price still has to fall to reach the PRZ (0 or
        # negative once price has already entered the zone).
        distance_to_prz_pct = (current_price - best["prz_high"]) / current_price * 100
    else:
        stop_loss = best["x_price"] * (1 + STOP_BUFFER_PCT / 100)
        target_1 = prz_mid - 0.382 * (prz_mid - best["a_price"])
        target_2 = prz_mid - 0.618 * (prz_mid - best["a_price"])
        target_3 = best["a_price"]
        distance_to_prz_pct = (best["prz_low"] - current_price) / current_price * 100

    return {
        "pattern": best["pattern"],
        "direction": best["direction"],
        "x_date": str(x_date.date()), "x_price": round(float(x_price), 2),
        "a_date": str(a_date.date()), "a_price": round(float(a_price), 2),
        "b_date": str(b_date.date()), "b_price": round(float(b_price), 2),
        "c_date": str(c_date.date()), "c_price": round(float(c_price), 2),
        "prz_low": round(float(min(best["prz_low"], best["prz_high"])), 2),
        "prz_high": round(float(max(best["prz_low"], best["prz_high"])), 2),
        "current_price": round(current_price, 2),
        "distance_to_prz_pct": round(float(distance_to_prz_pct), 2),
        "stop_loss": round(float(stop_loss), 2),
        "target_1": round(float(target_1), 2),
        "target_2": round(float(target_2), 2),
        "target_3": round(float(target_3), 2),
        "days_since_c": (date.today() - c_date.date()).days,
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for confirmed XABCD harmonic patterns.")

    header = [
        "symbol", "pattern", "direction",
        "x_date", "x_price", "a_date", "a_price", "b_date", "b_price",
        "c_date", "c_price",
        "prz_low", "prz_high", "current_price", "distance_to_prz_pct",
        "stop_loss", "target_1", "target_2", "target_3",
        "days_since_c", "last_updated",
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

        setup = find_predictive_setup(hist)
        if setup is None:
            rows.append([symbol] + [""] * (len(header) - 2) + [today_str])
            continue

        found += 1
        rows.append([
            symbol, setup["pattern"], setup["direction"],
            setup["x_date"], setup["x_price"], setup["a_date"], setup["a_price"],
            setup["b_date"], setup["b_price"], setup["c_date"], setup["c_price"],
            setup["prz_low"], setup["prz_high"], setup["current_price"], setup["distance_to_prz_pct"],
            setup["stop_loss"], setup["target_1"], setup["target_2"], setup["target_3"],
            setup["days_since_c"], today_str,
        ])
        print(f"{symbol}: {setup['direction']} {setup['pattern']} -- C confirmed {setup['days_since_c']}d ago, "
              f"PRZ {setup['prz_low']}-{setup['prz_high']} ({setup['distance_to_prz_pct']}% away), "
              f"projected targets {setup['target_1']}/{setup['target_2']}/{setup['target_3']}, stop {setup['stop_loss']}")

    ws.update(rows, "A1")
    print(f"Wrote harmonic pattern results for {len(rows) - 1} symbols ({found} with a live predicted pattern).")


if __name__ == "__main__":
    main()
