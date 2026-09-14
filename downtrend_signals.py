"""
Detects the Downtrend Short setup (analytics/downtrend_short.py --
Wyckoff Upthrust + Sign of Weakness, price action + volume only) on
WEEKLY bars for every actively tracked symbol.

Unlike ema_signals.py (which runs daily, after each day's close), this
runs WEEKLY -- once, after Friday's close -- because:
  - the underlying strategy never makes a decision off a single day's
    data (a trend needs at least a week to count)
  - the entry rule is "next week's open", which only exists to act on
    once a week anyway

Each symbol's result can be:
  - blank              -- no confirmed downtrend, or no Upthrust/SOW
                           setup on the most recent 1-2 weekly bars
  - PENDING_ENTRY       -- SOW break IS the most recently completed
                           week. Entry isn't known yet -- watch for
                           NEXT week's open and enter then.
  - ENTERED             -- only shows up if this is re-run mid-week
                           after the entry week has also completed;
                           normally you'll see PENDING_ENTRY turn into
                           an actual position you enter manually at
                           Monday's open, not this script re-confirming
                           it after the fact.

See .github/workflows/downtrend-signals.yml for the schedule.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY
"""

import json
import os
import sys
from datetime import date

import gspread
import pandas as pd
import yfinance as yf
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "analytics"))
import downtrend_short  # noqa: E402  (path insert must happen first)

SHEET_NAME = "Monthly Breakout Scan"
DOWNTREND_SHEET = "Downtrend_Signals"
HISTORY_PERIOD = "2y"  # needs real weekly history for swing detection -- 2y gives ~100 weekly bars


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
    "Every tracked symbol regardless of status -- same convention as ema_signals.py."
    records = spreadsheet.sheet1.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for Downtrend Short (weekly).")

    header = [
        "symbol",
        "downtrend_status", "downtrend_pattern",
        "downtrend_entry_price", "downtrend_entry_note",
        "downtrend_stop_loss", "downtrend_stop_reference_week",
        "downtrend_sow_break_week", "downtrend_upthrust_week", "downtrend_upthrust_resistance",
        "downtrend_regime_structure", "downtrend_distribution_weeks", "downtrend_volume_trend",
        "last_updated",
    ]
    ws = get_or_create_sheet(spreadsheet, DOWNTREND_SHEET, header)

    today_str = str(date.today())
    rows = [header]
    fetch_failed = 0
    signal_count = 0

    for symbol in active_symbols:
        try:
            hist = yf.Ticker(f"{symbol}.NS").history(period=HISTORY_PERIOD, interval="1d")
        except Exception as e:
            print(f"{symbol}: history fetch failed ({e})")
            hist = None
            fetch_failed += 1

        signal = None
        if hist is not None and not hist.empty:
            signal = downtrend_short.evaluate(hist)

        rows.append([
            symbol,
            signal["status"] if signal else "",
            signal["pattern"] if signal else "",
            (signal.get("entry_price") or "") if signal else "",
            (signal.get("entry_note") or "") if signal else "",
            signal["stop_loss"] if signal else "",
            signal["stop_reference_week"] if signal else "",
            signal["sow_break_week"] if signal else "",
            signal["upthrust_week"] if signal else "",
            signal["upthrust_resistance"] if signal else "",
            signal["regime_structure"] if signal else "",
            signal["distribution_week_count"] if signal else "",
            signal["volume_trend_read"] if signal else "",
            today_str,
        ])

        if signal:
            signal_count += 1
            note = signal.get("entry_note", f"entered at {signal.get('entry_price')}")
            print(f"{symbol}: Downtrend Short [{signal['status']}] -- stop {signal['stop_loss']} -- {note}")

    ws.update(rows, "A1")
    print(f"Wrote Downtrend Short results for {len(rows) - 1} symbols "
          f"({signal_count} with a live setup, {fetch_failed} fetch failures).")


if __name__ == "__main__":
    main()
