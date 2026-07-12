"""
Builds and maintains 120-day Delivery Value history for actively tracked
stocks, using NSE's official daily bhavcopy archive (a different, less
locked-down subdomain than nseindia.com's main site).

Delivery Value = DELIV_QTY x CLOSE_PRICE, both taken from the same day's
bhavcopy row -- no separate price lookup needed.

Logic:
- Walks backward day by day from today, skipping any date where no file
  exists (weekend/holiday), fetching each day's full-market file once and
  extracting data for every actively tracked symbol found in it.
- Stops once every tracked symbol has at least 120 recorded days, or a
  safety cap of ~250 calendar days is hit.
- Trims each symbol's history to the most recent 120 days.
- Computes avg_dv_120, the "High DV" tag, and the last-30-day highlight
  count, writing results to a separate DV_Summary tab.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""

import csv
import io
import json
import os
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
HIGH_DV_THRESHOLD_COUNT = 10
MAX_CALENDAR_LOOKBACK = 250

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
                results[symbol] = {
                    "deliv_qty": deliv_qty,
                    "close_price": close_price,
                    "delivery_value": round(deliv_qty * close_price, 2),
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


def backfill_history(active_symbols, history):
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
                for symbol, values in day_data.items():
                    history.setdefault(symbol, {})[date_str] = values
                print(f"{date_str}: fetched, {len(day_data)} tracked symbols found.")
            else:
                print(f"{date_str}: no file (likely non-trading day), skipping.")

        current_day -= timedelta(days=1)
        calendar_days_checked += 1

    # Trim each symbol's history down to the most recent TARGET_DAYS entries
    for symbol in history:
        dates_sorted = sorted(history[symbol].keys(), reverse=True)[:TARGET_DAYS]
        history[symbol] = {d: history[symbol][d] for d in dates_sorted}

    return history


def compute_summary(symbol, symbol_history):
    dates_sorted = sorted(symbol_history.keys())
    values = [symbol_history[d]["delivery_value"] for d in dates_sorted]

    if len(values) < TARGET_DAYS:
        return None  # not enough history yet to compute a meaningful average

    avg_dv_120 = sum(values) / len(values)

    last_30_dates = dates_sorted[-LOOKBACK_WINDOW:]
    last_30_values = [symbol_history[d]["delivery_value"] for d in last_30_dates]

    days_above_avg = sum(1 for v in last_30_values if v > avg_dv_120)
    high_dv_tag = days_above_avg > HIGH_DV_THRESHOLD_COUNT

    highlighted_days = [
        d for d in last_30_dates
        if symbol_history[d]["delivery_value"] >= HIGHLIGHT_MULTIPLIER * avg_dv_120
    ]

    return {
        "avg_dv_120": round(avg_dv_120, 2),
        "high_dv_tag": high_dv_tag,
        "days_above_avg_last30": days_above_avg,
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
        spreadsheet, SUMMARY_SHEET, ["symbol", "avg_dv_120", "high_dv_tag", "days_above_avg_last30", "last_updated"]
    )

    history = load_existing_history(history_ws)
    history = backfill_history(active_symbols, history)

    # Write history back
    history_rows = [["symbol", "date", "deliv_qty", "close_price", "delivery_value"]]
    for symbol, days in history.items():
        for d, v in days.items():
            history_rows.append([symbol, d, v["deliv_qty"], v["close_price"], v["delivery_value"]])
    history_ws.update(history_rows, "A1")
    print(f"Wrote {len(history_rows) - 1} history rows.")

    # Compute and write summary
    today_str = str(date.today())
    summary_rows = [["symbol", "avg_dv_120", "high_dv_tag", "days_above_avg_last30", "last_updated"]]
    for symbol in active_symbols:
        result = compute_summary(symbol, history.get(symbol, {}))
        if result:
            summary_rows.append([
                symbol, result["avg_dv_120"], result["high_dv_tag"],
                result["days_above_avg_last30"], today_str,
            ])
        else:
            summary_rows.append([symbol, "", "insufficient history", "", today_str])

    summary_ws.update(summary_rows, "A1")
    print(f"Wrote summary for {len(summary_rows) - 1} symbols.")


if __name__ == "__main__":
    main()
