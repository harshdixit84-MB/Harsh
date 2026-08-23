"""
Builds and maintains Delivery % history for actively tracked stocks,
using NSE's official daily bhavcopy archive.

Delivery % = DELIV_QTY / TTL_TRD_QNTY * 100

New logic (replaces the old Delivery-Value / median-baseline approach):
1. Track Delivery % per day (not raw Delivery Value in crores).
2. Maintain two rolling averages of Delivery %:
     - ADP_5  (5-day)  -- "what's happening right now"
     - ADP_20 (20-day) -- "what's normal for this stock"
3. Flag a day when Delivery % > 1.3x ADP_20:
     - price change that day positive -> "Potential Buying"
     - price change that day negative -> "Potential Selling"
4. Track ADP_5 vs ADP_20 the same way you'd track a moving-average
   crossover on price:
     - ADP_5 crossing above ADP_20 -> "Bullish Cross"
     - ADP_5 crossing below ADP_20 -> "Bearish Cross"
   and separately track whether each average is Rising/Falling/Flat
   day-over-day (used directly by the dashboard).

Fixes carried over from the previous version:
- NSE's archive sometimes serves stale/duplicate data for non-trading
  days instead of a clean 404 (identical deliv_qty + close_price
  repeated across dates). These are detected and skipped so they don't
  pollute the history or dilute the trading-day count.
- Old history rows written by the previous (Delivery-Value) schema
  don't have TTL_TRD_QNTY, so they can't produce a Delivery %. Those
  rows are dropped on load and transparently re-fetched from the NSE
  archive under the new schema.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""

import csv
import io
import json
import os
from datetime import date, datetime, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
HISTORY_SHEET = "DV_History"
SUMMARY_SHEET = "DV_Summary"

TARGET_DAYS = 120
SHORT_WINDOW = 5
LONG_WINDOW = 20
THRESHOLD_MULTIPLIER = 1.3
MAX_CALENDAR_LOOKBACK = 350
VERDICT_LOOKBACK_DAYS = 30

HISTORY_HEADER = [
    "symbol", "date", "deliv_qty", "ttl_trd_qnty", "close_price",
    "high_price", "low_price", "delivery_pct", "adp_5", "adp_20",
    "change_in_price", "verdict",
]
SUMMARY_HEADER = [
    "symbol", "adp_5", "adp_20", "adp5_trend", "adp20_trend",
    "crossover", "buying_selling_verdict", "last_updated",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
}


def parse_date_safe(d):
    """Handles dates that may have been auto-reformatted by Google Sheets
    (e.g. DD-MM-YYYY instead of the YYYY-MM-DD this script writes)."""
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(d, fmt).date()
        except ValueError:
            continue
    return None


def get_client():
    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_sheet(spreadsheet, title, header_row):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(header_row))
        ws.update([header_row], "A1")
    return ws


def get_active_symbols(spreadsheet):
    ws = spreadsheet.sheet1
    records = ws.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("status") == "active"})


def fetch_bhavcopy(day):
    date_str = day.strftime("%d%m%Y")
    url = f"https://archives.nseindia.com/products/content/sec_bhavdata_full_{date_str}.csv"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        print(f"  [diagnostic] {date_str}: HTTP {resp.status_code}, body preview: {resp.text[:150]!r}")
        return None
    return resp.text


def parse_bhavcopy_for_symbols(csv_text, symbols_wanted):
    results = {}
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        row = {k.strip(): v.strip() for k, v in row.items()}
        symbol = row.get("SYMBOL")
        series = row.get("SERIES")
        if symbol in symbols_wanted and series == "EQ":
            try:
                deliv_qty = float(row["DELIV_QTY"])
                ttl_trd_qnty = float(row["TTL_TRD_QNTY"])
                close_price = float(row["CLOSE_PRICE"])
                high_price = float(row["HIGH_PRICE"])
                low_price = float(row["LOW_PRICE"])
                results[symbol] = {
                    "deliv_qty": deliv_qty,
                    "ttl_trd_qnty": ttl_trd_qnty,
                    "close_price": close_price,
                    "high_price": high_price,
                    "low_price": low_price,
                }
            except (ValueError, KeyError):
                continue
    return results


def load_existing_history(history_ws):
    """Loads previously-saved history. Rows written under the old
    (Delivery-Value) schema won't have ttl_trd_qnty -- those are
    dropped here so backfill_history re-fetches them fresh from the
    NSE archive under the new schema."""
    records = history_ws.get_all_records()
    history = {}
    for r in records:
        if not r.get("ttl_trd_qnty"):
            continue  # old-schema row, force a re-fetch
        symbol = r["symbol"]
        close_price = float(r["close_price"])
        history.setdefault(symbol, {})[r["date"]] = {
            "deliv_qty": float(r["deliv_qty"]),
            "ttl_trd_qnty": float(r["ttl_trd_qnty"]),
            "close_price": close_price,
            "high_price": float(r["high_price"]) if r.get("high_price") not in (None, "") else close_price,
            "low_price": float(r["low_price"]) if r.get("low_price") not in (None, "") else close_price,
        }
    return history


def dedupe_symbol_history(symbol_history):
    """Removes stale-duplicate entries where consecutive dates (in time)
    have identical deliv_qty + close_price -- keeps only the earliest
    date in each duplicate run, since that's the real trading day."""
    dates_sorted = sorted(symbol_history.keys())
    cleaned = {}
    last_kept_values = None

    for d in dates_sorted:
        values = symbol_history[d]
        signature = (values["deliv_qty"], values["close_price"])
        if signature == last_kept_values:
            continue  # stale duplicate, skip
        cleaned[d] = values
        last_kept_values = signature

    return cleaned


def backfill_history(active_symbols, history):
    # Clean up any weekend dates that slipped in before this fix existed --
    # also handles rows whose date got reformatted by Google Sheets
    for symbol in list(history.keys()):
        cleaned = {}
        for d, v in history[symbol].items():
            parsed = parse_date_safe(d)
            if parsed is None:
                print(f"Skipping malformed date '{d}' for {symbol}")
                continue
            if parsed.weekday() < 5:
                cleaned[d] = v
        history[symbol] = cleaned

    # Clean up any duplicates already sitting in previously-loaded history
    for symbol in list(history.keys()):
        history[symbol] = dedupe_symbol_history(history[symbol])

    # Track the last accepted (deliv_qty, close_price) per symbol so we can
    # detect stale duplicates as we walk backward through new fetches too
    last_accepted = {}
    for symbol, days in history.items():
        if days:
            latest_date = max(days.keys())
            v = days[latest_date]
            last_accepted[symbol] = (v["deliv_qty"], v["close_price"])

    today = date.today()
    calendar_days_checked = 0
    current_day = today

    while calendar_days_checked < MAX_CALENDAR_LOOKBACK:
        symbols_needing_data = [
            s for s in active_symbols if len(history.get(s, {})) < TARGET_DAYS
        ]
        if not symbols_needing_data:
            break

        date_str = current_day.strftime("%Y-%m-%d")

        if current_day.weekday() >= 5:  # Saturday=5, Sunday=6 -- NSE never trades these
            current_day -= timedelta(days=1)
            calendar_days_checked += 1
            continue

        already_have_this_date = all(
            date_str in history.get(s, {}) for s in symbols_needing_data
        )

        if not already_have_this_date:
            csv_text = fetch_bhavcopy(current_day)
            if csv_text:
                day_data = parse_bhavcopy_for_symbols(csv_text, set(active_symbols))
                accepted_count = 0
                for symbol, values in day_data.items():
                    signature = (values["deliv_qty"], values["close_price"])
                    if last_accepted.get(symbol) == signature:
                        continue  # stale duplicate of the last real day, skip
                    history.setdefault(symbol, {})[date_str] = values
                    last_accepted[symbol] = signature
                    accepted_count += 1
                print(f"{date_str}: fetched, {accepted_count} symbols accepted (duplicates skipped).")
            else:
                print(f"{date_str}: no file (likely non-trading day), skipping.")

        current_day -= timedelta(days=1)
        calendar_days_checked += 1

    for symbol in history:
        dates_sorted = sorted(history[symbol].keys(), reverse=True)[:TARGET_DAYS]
        history[symbol] = {d: history[symbol][d] for d in dates_sorted}

    return history


def rolling_average(values, window):
    """values: list of floats (or None) in chronological order.
    Returns a same-length list where index i is the average of the
    trailing `window` values ending at i, or None if there aren't
    enough prior values yet (or a None sits in that window)."""
    result = []
    for i in range(len(values)):
        if i + 1 < window:
            result.append(None)
            continue
        window_vals = values[i - window + 1:i + 1]
        if any(v is None for v in window_vals):
            result.append(None)
        else:
            result.append(round(sum(window_vals) / window, 4))
    return result


def determine_daily_verdict(delivery_pct, adp_20, price_change_pct):
    if delivery_pct is None or adp_20 is None or price_change_pct is None:
        return ""
    if delivery_pct > THRESHOLD_MULTIPLIER * adp_20:
        if price_change_pct > 0:
            return "Potential Buying"
        elif price_change_pct < 0:
            return "Potential Selling"
    return ""


def enrich_history(symbol_history):
    """Adds delivery_pct, adp_5, adp_20, adp_diff, change_in_price and
    verdict to every day in a symbol's history."""
    dates_sorted = sorted(symbol_history.keys())

    delivery_pcts = []
    for d in dates_sorted:
        day = symbol_history[d]
        pct = round((day["deliv_qty"] / day["ttl_trd_qnty"]) * 100, 4) if day.get("ttl_trd_qnty") else None
        day["delivery_pct"] = pct
        delivery_pcts.append(pct)

    adp5_list = rolling_average(delivery_pcts, SHORT_WINDOW)
    adp20_list = rolling_average(delivery_pcts, LONG_WINDOW)

    for i, d in enumerate(dates_sorted):
        day = symbol_history[d]
        day["adp_5"] = adp5_list[i]
        day["adp_20"] = adp20_list[i]
        day["adp_diff"] = (
            round(adp5_list[i] - adp20_list[i], 4)
            if adp5_list[i] is not None and adp20_list[i] is not None
            else None
        )

        if i == 0:
            day["change_in_price"] = None
            day["verdict"] = ""
            continue

        prev_close = symbol_history[dates_sorted[i - 1]]["close_price"]
        price_change_pct = ((day["close_price"] - prev_close) / prev_close) * 100 if prev_close else None
        day["change_in_price"] = round(price_change_pct, 2) if price_change_pct is not None else None
        day["verdict"] = determine_daily_verdict(day["delivery_pct"], day["adp_20"], price_change_pct)

    return symbol_history


def trend_label(curr, prev_val):
    if curr is None or prev_val is None:
        return ""
    if curr > prev_val:
        return "Rising"
    if curr < prev_val:
        return "Falling"
    return "Flat"


def compute_summary(symbol_history):
    dates_sorted = sorted(symbol_history.keys())
    if len(dates_sorted) < LONG_WINDOW + 1:
        return None  # not enough history yet for a 20-day average plus a day-over-day comparison

    latest = symbol_history[dates_sorted[-1]]
    prev = symbol_history[dates_sorted[-2]]

    adp_5 = latest.get("adp_5")
    adp_20 = latest.get("adp_20")

    adp5_trend = trend_label(adp_5, prev.get("adp_5"))
    adp20_trend = trend_label(adp_20, prev.get("adp_20"))

    crossover = ""
    curr_diff = latest.get("adp_diff")
    prev_diff = prev.get("adp_diff")
    if curr_diff is not None and prev_diff is not None:
        if prev_diff <= 0 and curr_diff > 0:
            crossover = "Bullish Cross"
        elif prev_diff >= 0 and curr_diff < 0:
            crossover = "Bearish Cross"

    last_30_dates = dates_sorted[-VERDICT_LOOKBACK_DAYS:]
    verdicts = [symbol_history[d].get("verdict", "") for d in last_30_dates]
    buying_count = verdicts.count("Potential Buying")
    selling_count = verdicts.count("Potential Selling")
    if buying_count > selling_count:
        buying_selling_verdict = "Heavy Buying"
    elif selling_count > buying_count:
        buying_selling_verdict = "Heavy Selling"
    else:
        buying_selling_verdict = ""

    return {
        "adp_5": adp_5,
        "adp_20": adp_20,
        "adp5_trend": adp5_trend,
        "adp20_trend": adp20_trend,
        "crossover": crossover,
        "buying_selling_verdict": buying_selling_verdict,
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Tracking Delivery % for {len(active_symbols)} active symbols.")

    history_ws = get_or_create_sheet(spreadsheet, HISTORY_SHEET, HISTORY_HEADER)
    summary_ws = get_or_create_sheet(spreadsheet, SUMMARY_SHEET, SUMMARY_HEADER)

    history = load_existing_history(history_ws)
    history = backfill_history(active_symbols, history)

    for symbol in history:
        history[symbol] = enrich_history(history[symbol])

    history_rows = [HISTORY_HEADER]
    for symbol, days in history.items():
        for d, v in days.items():
            history_rows.append([
                symbol, d, v["deliv_qty"], v["ttl_trd_qnty"], v["close_price"],
                v["high_price"], v["low_price"], v.get("delivery_pct", ""),
                v.get("adp_5", ""), v.get("adp_20", ""),
                v.get("change_in_price", ""), v.get("verdict", ""),
            ])
    history_ws.clear()
    history_ws.update(history_rows, "A1")
    print(f"Wrote {len(history_rows) - 1} history rows (after deduplication).")

    today_str = str(date.today())
    summary_rows = [SUMMARY_HEADER]
    for symbol in active_symbols:
        symbol_history = history.get(symbol, {})
        result = compute_summary(symbol_history)

        if result:
            summary_rows.append([
                symbol, result["adp_5"], result["adp_20"],
                result["adp5_trend"], result["adp20_trend"],
                result["crossover"], result["buying_selling_verdict"], today_str,
            ])
        else:
            summary_rows.append([symbol, "", "", "insufficient history", "", "", "", today_str])

    summary_ws.clear()
    summary_ws.update(summary_rows, "A1")
    print(f"Wrote summary for {len(summary_rows) - 1} symbols.")


if __name__ == "__main__":
    main()
