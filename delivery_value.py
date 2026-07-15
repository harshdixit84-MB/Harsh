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
HIGHLIGHT_MULTIPLIER = 2
LOOKBACK_WINDOW = 75
CLUSTER_WINDOW_SIZE = 5
CLUSTER_MIN_ELEVATED_DAYS = 3
VERDICT_LOOKBACK_DAYS = 30

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
                high_price = float(row["HIGH_PRICE"])
                low_price = float(row["LOW_PRICE"])
                delivery_value_cr = round((deliv_qty * close_price) / 1_00_00_000, 4)
                results[symbol] = {
                    "deliv_qty": deliv_qty,
                    "close_price": close_price,
                    "high_price": high_price,
                    "low_price": low_price,
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
        close_price = float(r["close_price"])
        history.setdefault(symbol, {})[r["date"]] = {
            "deliv_qty": float(r["deliv_qty"]),
            "close_price": close_price,
            "high_price": float(r["high_price"]) if r.get("high_price") not in (None, "") else close_price,
            "low_price": float(r["low_price"]) if r.get("low_price") not in (None, "") else close_price,
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
    # Clean up any weekend dates that slipped in before this fix existed
    for symbol in list(history.keys()):
        history[symbol] = {
            d: v for d, v in history[symbol].items()
            if date.fromisoformat(d).weekday() < 5
        }

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


def compute_range_position(high, low, close):
    if high == low:
        return 0.5  # no intraday range (e.g. circuit-locked day) -- treat as neutral
    return (close - low) / (high - low)


def determine_daily_verdict(is_dv_high, price_change_pct, range_position):
    if not is_dv_high or price_change_pct is None:
        return ""

    if -2 <= price_change_pct <= 2:
        if range_position >= 0.6:
            return "Accumulation"
        elif range_position <= 0.4:
            return "Distribution"
        return ""
    elif price_change_pct > 2:
        if range_position >= 0.8:
            return "Fresh Entry"
        return ""
    elif price_change_pct < -2:
        if range_position <= 0.2:
            return "Fresh Selling"
        return ""
    return ""


def enrich_history_with_verdicts(symbol_history):
    """Adds change_in_price and verdict to every day in a symbol's history.
    Requires the full history to compute the 120-day median baseline."""
    dates_sorted = sorted(symbol_history.keys())
    values = [symbol_history[d]["delivery_value"] for d in dates_sorted]

    if len(values) < TARGET_DAYS:
        median_dv_120 = None
    else:
        median_dv_120 = statistics.median(values)

    for i, d in enumerate(dates_sorted):
        day = symbol_history[d]

        if i == 0:
            day["change_in_price"] = None
            day["verdict"] = ""
            continue

        prev_close = symbol_history[dates_sorted[i - 1]]["close_price"]
        price_change_pct = ((day["close_price"] - prev_close) / prev_close) * 100 if prev_close else None
        range_position = compute_range_position(day["high_price"], day["low_price"], day["close_price"])

        is_dv_high = (
            median_dv_120 is not None
            and day["delivery_value"] >= HIGHLIGHT_MULTIPLIER * median_dv_120
        )

        day["change_in_price"] = round(price_change_pct, 2) if price_change_pct is not None else None
        day["verdict"] = determine_daily_verdict(is_dv_high, price_change_pct, range_position)

    return symbol_history
    dates_sorted = sorted(symbol_history.keys())
    values = [symbol_history[d]["delivery_value"] for d in dates_sorted]

    if len(values) < TARGET_DAYS:
        return None

    median_dv_120 = statistics.median(values)

    start_idx = len(dates_sorted) - LOOKBACK_WINDOW
    last_N_dates = dates_sorted[start_idx:]
    last_N_values = [symbol_history[d]["delivery_value"] for d in last_N_dates]

    days_above_baseline = sum(1 for v in last_N_values if v > median_dv_120)
    highlighted_days = [
        d for d in last_N_dates
        if symbol_history[d]["delivery_value"] >= HIGHLIGHT_MULTIPLIER * median_dv_120
    ]

    # Day-over-day price change for each day in the window, using the
    # previous trading day's close (from full history, not just the window)
    price_changes = []
    for i, d in enumerate(last_N_dates):
        idx_in_full = start_idx + i
        if idx_in_full == 0:
            price_changes.append(None)
            continue
        prev_close = symbol_history[dates_sorted[idx_in_full - 1]]["close_price"]
        curr_close = symbol_history[d]["close_price"]
        price_changes.append(((curr_close - prev_close) / prev_close) * 100 if prev_close else None)

    # Clustering check: any 5-trading-day window with at least 3 days that
    # are both genuinely elevated (2x median) AND quiet on price (<2% move)
    # -- the classic "big volume, flat price" signature of quiet
    # accumulation. Note: this pattern is directionally ambiguous on its
    # own -- it can also reflect quiet distribution, not just buying.
    elevated_flags = [
        (v >= HIGHLIGHT_MULTIPLIER * median_dv_120) and (pc is not None and abs(pc) < 2)
        for v, pc in zip(last_N_values, price_changes)
    ]
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
        spreadsheet, HISTORY_SHEET,
        ["symbol", "date", "deliv_qty", "close_price", "delivery_value", "change_in_price", "verdict"]
    )
    summary_ws = get_or_create_sheet(
        spreadsheet, SUMMARY_SHEET,
        ["symbol", "median_dv_120", "high_dv_tag", "days_above_baseline_last30",
         "highlighted_days_count", "buying_selling_verdict", "last_updated"]
    )

    history = load_existing_history(history_ws)
    history = backfill_history(active_symbols, history)

    # Enrich every symbol's history with change_in_price and verdict per day
    for symbol in history:
        history[symbol] = enrich_history_with_verdicts(history[symbol])

    history_rows = [["symbol", "date", "deliv_qty", "close_price", "delivery_value", "change_in_price", "verdict"]]
    for symbol, days in history.items():
        for d, v in days.items():
            history_rows.append([
                symbol, d, v["deliv_qty"], v["close_price"], v["delivery_value"],
                v.get("change_in_price", ""), v.get("verdict", ""),
            ])
    history_ws.update(history_rows, "A1")
    print(f"Wrote {len(history_rows) - 1} history rows (after deduplication).")

    today_str = str(date.today())
    summary_rows = [["symbol", "median_dv_120", "high_dv_tag", "days_above_baseline_last30",
                      "highlighted_days_count", "buying_selling_verdict", "last_updated"]]
    for symbol in active_symbols:
        symbol_history = history.get(symbol, {})
        result = compute_summary(symbol, symbol_history)

        if result:
            dates_sorted = sorted(symbol_history.keys())
            last_30_dates = dates_sorted[-VERDICT_LOOKBACK_DAYS:]
            verdicts = [symbol_history[d].get("verdict", "") for d in last_30_dates]
            accumulation_count = verdicts.count("Accumulation")
            distribution_count = verdicts.count("Distribution")

            if accumulation_count > distribution_count:
                buying_selling_verdict = "Heavy Buying"
            elif distribution_count > accumulation_count:
                buying_selling_verdict = "Heavy Selling"
            else:
                buying_selling_verdict = ""

            summary_rows.append([
                symbol, result["median_dv_120"], result["high_dv_tag"],
                result["days_above_baseline_last30"], result["highlighted_days_count"],
                buying_selling_verdict, today_str,
            ])
        else:
            summary_rows.append([symbol, "", "insufficient history", "", "", "", today_str])

    summary_ws.update(summary_rows, "A1")
    print(f"Wrote summary for {len(summary_rows) - 1} symbols.")


if __name__ == "__main__":
    main()
