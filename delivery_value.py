"""
Builds and maintains 120-day Delivery Value history for actively tracked
stocks, using NSE's official daily bhavcopy archive.

Delivery Value = DELIV_QTY x CLOSE_PRICE, stored in crores.

Fixes applied after reviewing real data:
1. NSE's archive sometimes serves stale/duplicate data for non-trading
   days instead of a clean 404 (identical deliv_qty + close_price
   repeated across dates). These are detected and skipped so they don't
   pollute the history or dilute the trading-day count.
2. The 120-day baseline uses the MEDIAN instead of the mean, since a
   plain average gets dragged upward by the very spikes we're trying to
   detect (Delivery Value can range 10x-20x within the same stock).
3. "High DV" is no longer a simple day-count -- it requires CLUSTERING:
   at least 3 elevated days within any 5-trading-day window in the last
   30 days. A single huge spike (often just one block deal) no longer
   triggers the tag on its own; sustained accumulation does.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""

import csv
import io
import json
import os
import statistics
from datetime import date, timedelta

import gspread
import requests
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
HISTORY_SHEET = "DV_History"
SUMMARY_SHEET = "DV_Summary"

TARGET_DAYS = 120
HIGHLIGHT_MULTIPLIER = 1.5
LOOKBACK_WINDOW = 30
CLUSTER_WINDOW_SIZE = 5
CLUSTER_MIN_ELEVATED_DAYS = 3
MAX_CALENDAR_LOOKBACK = 350

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
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
                close_price = float(row["CLOSE_PRICE"])
                delivery_value_cr = round((deliv_qty * close_price) / 1_00_00_000, 4)
                results[symbol] = {
                    "deliv_qty": deliv_qty,
                    "close_price": close_price,
                    "delivery_value": delivery_value_cr,
                }
            except (ValueError, KeyError):
                continue
    return results


def load_existing_history(history_ws):
    records = history_ws.get_all_records()
    history = {}
    for r in records:
        symbol = r["symbol"]
        history.setdefault(symbol, {})[r["date"]] = {
            "deliv_qty": float(r["deliv_qty"]),
            "close_price": float(r["close_price"]),
            "delivery_value": float(r["delivery_value"]),
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


def compute_summary(symbol, symbol_history):
    dates_sorted = sorted(symbol_history.keys())
    values = [symbol_history[d]["delivery_value"] for d in dates_sorted]

    if len(values) < TARGET_DAYS:
        return None

    median_dv_120 = statistics.median(values)

    last_30_dates = dates_sorted[-LOOKBACK_WINDOW:]
    last_30_values = [symbol_history[d]["delivery_value"] for d in last_30_dates]

    days_above_baseline = sum(1 for v in last_30_values if v > median_dv_120)
    highlighted_days = [
        d for d in last_30_dates
        if symbol_history[d]["delivery_value"] >= HIGHLIGHT_MULTIPLIER * median_dv_120
    ]

    # Clustering check: any 5-trading-day window in the last 30 with
    # at least 3 days that are genuinely elevated (1.5x median) -- using
    # plain "above median" here would trigger for almost every stock,
    # since ~50% of days are above their own median by definition
    elevated_flags = [v >= HIGHLIGHT_MULTIPLIER * median_dv_120 for v in last_30_values]
    has_cluster = False
    for i in range(len(elevated_flags) - CLUSTER_WINDOW_SIZE + 1):
        window = elevated_flags[i:i + CLUSTER_WINDOW_SIZE]
        if sum(window) >= CLUSTER_MIN_ELEVATED_DAYS:
            has_cluster = True
            break

    return {
        "median_dv_120": round(median_dv_120, 2),
        "high_dv_tag": has_cluster,
        "days_above_baseline_last30": days_above_baseline,
        "highlighted_days_count": len(highlighted_days),
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Tracking Delivery Value for {len(active_symbols)} active symbols.")

    history_ws = get_or_create_sheet(
        spreadsheet, HISTORY_SHEET, ["symbol", "date", "deliv_qty", "close_price", "delivery_value"]
    )
    summary_ws = get_or_create_sheet(
        spreadsheet, SUMMARY_SHEET,
        ["symbol", "median_dv_120", "high_dv_tag", "days_above_baseline_last30", "highlighted_days_count", "last_updated"]
    )

    history = load_existing_history(history_ws)
    history = backfill_history(active_symbols, history)

    history_rows = [["symbol", "date", "deliv_qty", "close_price", "delivery_value"]]
    for symbol, days in history.items():
        for d, v in days.items():
            history_rows.append([symbol, d, v["deliv_qty"], v["close_price"], v["delivery_value"]])
    history_ws.update(history_rows, "A1")
    print(f"Wrote {len(history_rows) - 1} history rows (after deduplication).")

    today_str = str(date.today())
    summary_rows = [["symbol", "median_dv_120", "high_dv_tag", "days_above_baseline_last30", "highlighted_days_count", "last_updated"]]
    for symbol in active_symbols:
        result = compute_summary(symbol, history.get(symbol, {}))
        if result:
            summary_rows.append([
                symbol, result["median_dv_120"], result["high_dv_tag"],
                result["days_above_baseline_last30"], result["highlighted_days_count"], today_str,
            ])
        else:
            summary_rows.append([symbol, "", "insufficient history", "", "", today_str])

    summary_ws.update(summary_rows, "A1")
    print(f"Wrote summary for {len(summary_rows) - 1} symbols.")


if __name__ == "__main__":
    main()
