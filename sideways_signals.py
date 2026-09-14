"""
Detects the Sideways Range setup (analytics/sideways_range.py --
Wyckoff Spring+SOS for longs, Upthrust+SOW for shorts, price action +
volume only) on WEEKLY bars for every actively tracked symbol.

Runs WEEKLY, same reasoning as downtrend_signals.py: the underlying
strategy never decides off a single day, and the entry rule is "next
week's open".

Each symbol's result can be:
  - blank          -- no confirmed/near-confirmed sideways regime, no
                       range narrow enough, or no Spring/Upthrust setup
                       on the most recent 1-2 weekly bars
  - PENDING_ENTRY   -- confirmation IS the most recently completed
                       week. Entry isn't known yet -- watch for NEXT
                       week's open.
  - ENTERED         -- only shows up if re-run after the entry week has
                       also completed.
  direction is LONG (off support) or SHORT (off resistance).

See .github/workflows/sideways-signals.yml for the schedule.

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
import sideways_range  # noqa: E402  (path insert must happen first)

SHEET_NAME = "Monthly Breakout Scan"
SIDEWAYS_SHEET = "Sideways_Signals"
HISTORY_PERIOD = "2y"  # same as downtrend_signals.py -- needs real weekly history for range detection


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
    "Every tracked symbol regardless of status -- same convention as ema_signals.py / downtrend_signals.py."
    records = spreadsheet.sheet1.get_all_records()
    return sorted({r["symbol"] for r in records if r.get("symbol")})


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)

    active_symbols = get_active_symbols(spreadsheet)
    print(f"Scanning {len(active_symbols)} active symbols for Sideways Range (weekly).")

    header = [
        "symbol",
        "sideways_status", "sideways_direction", "sideways_pattern",
        "sideways_entry_price", "sideways_entry_note",
        "sideways_stop_loss", "sideways_target",
        "sideways_pattern_week", "sideways_confirm_week",
        "sideways_range_resistance", "sideways_range_support", "sideways_range_width_pct",
        "sideways_volume_trend",
        "last_updated",
    ]
    ws = get_or_create_sheet(spreadsheet, SIDEWAYS_SHEET, header)

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
            signal = sideways_range.evaluate(hist)

        rows.append([
            symbol,
            signal["status"] if signal else "",
            signal["direction"] if signal else "",
            signal["pattern"] if signal else "",
            (signal.get("entry_price") or "") if signal else "",
            (signal.get("entry_note") or "") if signal else "",
            signal["stop_loss"] if signal else "",
            signal["target"] if signal else "",
            signal["pattern_week"] if signal else "",
            signal["confirm_week"] if signal else "",
            signal["range_resistance"] if signal else "",
            signal["range_support"] if signal else "",
            signal["range_width_pct"] if signal else "",
            signal["volume_trend_read"] if signal else "",
            today_str,
        ])

        if signal:
            signal_count += 1
            note = signal.get("entry_note", f"entered at {signal.get('entry_price')}")
            print(f"{symbol}: Sideways Range [{signal['direction']} {signal['status']}] "
                  f"-- stop {signal['stop_loss']}, target {signal['target']} -- {note}")

    ws.update(rows, "A1")
    print(f"Wrote Sideways Range results for {len(rows) - 1} symbols "
          f"({signal_count} with a live setup, {fetch_failed} fetch failures).")


if __name__ == "__main__":
    main()
