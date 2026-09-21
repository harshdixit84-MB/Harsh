#!/usr/bin/env python3
"""
outlook_batch.py -- the once-a-day Buy/Hold/Sell outlook run.

Scheduled by .github/workflows/outlook-daily.yml at 4:00 PM IST, Monday to
Friday (after the 3:30 PM close, so today's daily candle is final).

What it does
------------
1. Reads every non-archived symbol from the "Monthly Breakout Scan" sheet
   (the same list the dashboard shows).
2. Asks the nse-stock-chatbot API for each symbol's outlook, one at a time,
   with fresh=1 so yesterday's server-side cache is never reused. The
   outlook logic itself lives in that repo (core/outlook.py): Nifty trend ->
   sector trend -> stock trend/patterns -> BUY / HOLD / SELL.
3. Writes data/outlook.json, which the dashboard (index.html) reads. The
   workflow commits that file back to the repo.

The dashboard no longer calculates outlooks by itself in the background:
this run is the only scheduled calculation. (Clicking a ticker that is not
in the file yet, e.g. one added after 4 PM, still calculates that one
ticker on demand.)

If a ticker fails, its previous result is carried over and flagged, instead
of the badge disappearing. If nothing at all could be calculated, the
existing file is left untouched and the run exits with an error.

Environment
-----------
GOOGLE_SERVICE_ACCOUNT_KEY  service-account JSON (same secret the other jobs use)
OUTLOOK_API_URL             optional override of the chatbot API base URL
"""
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SHEET_NAME = "Monthly Breakout Scan"
API_URL = os.environ.get("OUTLOOK_API_URL", "https://nse-stock-chatbot.vercel.app/api")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "outlook.json")

REQUEST_TIMEOUT = 70      # Vercel functions stop at 60 s; give the response a little longer to arrive
PAUSE_SECONDS = 1.0       # gap between tickers: keeps the shared Angel account well under its rate limit
MAX_ATTEMPTS = 3
RETRY_WAIT_SECONDS = 20   # also gives a throttled Angel session time to recover
NON_RETRYABLE = ("valid NSE equity symbol",)

IST = timezone(timedelta(hours=5, minutes=30))


def get_active_symbols():
    "Every non-archived symbol on the dashboard's sheet (dashboard.js treats status == 'archived' as hidden)."
    import gspread
    from google.oauth2.service_account import Credentials

    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    client = gspread.authorize(Credentials.from_service_account_info(key_dict, scopes=scopes))
    records = client.open(SHEET_NAME).sheet1.get_all_records()
    return sorted({
        str(r["symbol"]).strip()
        for r in records
        if r.get("symbol") and str(r.get("status", "")).strip().lower() != "archived"
    })


def fetch_one(symbol):
    "-> (outlook_dict, None) on success, (None, error_text) after the retries are used up."
    last_error = "unknown error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.get(
                API_URL,
                params={"symbol": symbol.upper(), "mode": "outlook", "fresh": "1"},
                timeout=REQUEST_TIMEOUT,
            )
            data = resp.json()
        except Exception as e:  # timeout, connection reset, or a non-JSON 504 page from Vercel
            last_error = f"{type(e).__name__}: {e}"
        else:
            if isinstance(data, dict) and "error" not in data and data.get("action"):
                return data, None
            last_error = str(data.get("error", "unexpected response")) if isinstance(data, dict) else "unexpected response"
            if any(marker in last_error for marker in NON_RETRYABLE):
                break
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_WAIT_SECONDS)
    return None, last_error


def load_previous():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f).get("results", {})
    except Exception:
        return {}


def merge(previous_results, fresh, symbols):
    "Today's results, with a flagged carry-over of the last good result for any ticker that failed."
    results, carried = {}, []
    note = "Today's 4 PM run could not refresh this ticker; showing the last successful result."
    for symbol in symbols:
        if symbol in fresh:
            results[symbol] = fresh[symbol]
        elif symbol in previous_results:
            old = dict(previous_results[symbol])
            old["carried_over"] = True
            notes = list(old.get("data_notes") or [])
            if note not in notes:
                notes.append(note)
            old["data_notes"] = notes
            results[symbol] = old
            carried.append(symbol)
    return results, carried


def write_atomic(payload):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_PATH)


def main():
    started = time.time()
    symbols = get_active_symbols()
    print(f"{len(symbols)} active symbols to process")

    fresh, failed = {}, {}
    for i, symbol in enumerate(symbols, 1):
        data, error = fetch_one(symbol)
        if data:
            fresh[symbol] = data
            print(f"[{i}/{len(symbols)}] {symbol}: {data['action']['label']}")
        else:
            failed[symbol] = error
            print(f"[{i}/{len(symbols)}] {symbol}: FAILED ({error})")
        time.sleep(PAUSE_SECONDS)

    if symbols and not fresh:
        print("::error::No outlook could be calculated for any symbol; leaving data/outlook.json untouched.")
        sys.exit(1)

    results, carried = merge(load_previous(), fresh, symbols)
    now = datetime.now(timezone.utc)
    write_atomic({
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "symbols": len(symbols),
        "refreshed": len(fresh),
        "carried_over": carried,
        "failed": failed,
        "results": results,
    })

    counts = {}
    for r in results.values():
        label = (r.get("action") or {}).get("label", "?")
        counts[label] = counts.get(label, 0) + 1
    print(f"Done in {time.time() - started:.0f}s: {len(fresh)} refreshed, {len(carried)} carried over, "
          f"{len(failed) - len(carried)} without a result. Totals: {counts}")
    if failed:
        print(f"::warning::{len(failed)} ticker(s) failed today: {', '.join(sorted(failed))}")


if __name__ == "__main__":
    main()
