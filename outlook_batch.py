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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

SHEET_NAME = "Monthly Breakout Scan"
API_URL = os.environ.get("OUTLOOK_API_URL", "https://nse-stock-chatbot.vercel.app/api")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "outlook.json")

REQUEST_TIMEOUT = 65      # Vercel functions stop at 60 s; give the response a little longer to arrive
WORKERS = 2               # tickers processed at the same time (gentle on the shared Angel account)
PAUSE_SECONDS = 1.0       # gap after each ticker, per worker
MAX_ATTEMPTS = 2
RETRY_WAIT_SECONDS = 20   # also gives a throttled Angel session time to recover
NON_RETRYABLE = ("valid NSE equity symbol",)
BUDGET_SECONDS = 45 * 60  # stop starting new tickers after this, so partial results are still saved
                          # (the GitHub job itself is cut off at 60 minutes and would save nothing)
MAX_CONSECUTIVE_FAILURES = 5  # this many failures in a row means something systemic is wrong: stop early

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
        except Exception as e:  # timeout or connection error
            last_error = f"{type(e).__name__}: {e}"
        else:
            try:
                data = resp.json()
            except ValueError:  # e.g. Vercel's HTML "504 gateway timeout" page
                last_error = f"HTTP {resp.status_code}, not JSON: {resp.text[:150]!r}"
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


def log(message):
    print(message, flush=True)  # flush: GitHub buffers piped output, and a cancelled job would otherwise show nothing


def main():
    started = time.time()
    symbols = get_active_symbols()
    log(f"{len(symbols)} active symbols to process via {API_URL} "
        f"({WORKERS} at a time, stop starting new ones after {BUDGET_SECONDS // 60} min)")

    fresh, failed = {}, {}
    state = {"consecutive_failures": 0, "abort_reason": None}
    lock = threading.Lock()

    def process(item):
        index, symbol = item
        with lock:
            if not state["abort_reason"] and time.time() - started > BUDGET_SECONDS:
                state["abort_reason"] = f"the {BUDGET_SECONDS // 60}-minute time budget ran out"
            reason = state["abort_reason"]
        if reason:
            return symbol, None, f"skipped: {reason}"
        t0 = time.time()
        data, error = fetch_one(symbol)
        took = time.time() - t0
        with lock:
            if data:
                state["consecutive_failures"] = 0
                log(f"[{index}/{len(symbols)}] {symbol}: {data['action']['label']} ({took:.1f}s)")
            else:
                state["consecutive_failures"] += 1
                log(f"[{index}/{len(symbols)}] {symbol}: FAILED after {took:.1f}s ({error})")
                if state["consecutive_failures"] >= MAX_CONSECUTIVE_FAILURES and not state["abort_reason"]:
                    state["abort_reason"] = (f"{MAX_CONSECUTIVE_FAILURES} tickers in a row failed "
                                             f"(last error: {error})")
                    log(f"STOPPING EARLY: {state['abort_reason']}")
        time.sleep(PAUSE_SECONDS)
        return symbol, data, error

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for symbol, data, error in pool.map(process, enumerate(symbols, 1)):
            if data:
                fresh[symbol] = data
            else:
                failed[symbol] = error

    skipped = [sym for sym, err in failed.items() if str(err).startswith("skipped:")]
    if symbols and not fresh:
        first_error = next(iter(failed.values()), "unknown")
        print(f"::error::No outlook could be calculated for any symbol ({first_error}); "
              "leaving data/outlook.json untouched.", flush=True)
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
    log(f"Done in {time.time() - started:.0f}s: {len(fresh)} refreshed, {len(carried)} carried over from an "
        f"earlier run, {len(failed) - len(carried)} without any result. Totals: {counts}")
    if state["abort_reason"]:
        log(f"::warning::Run stopped early because {state['abort_reason']}. "
            f"{len(skipped)} ticker(s) were not attempted.")
    elif failed:
        log(f"::warning::{len(failed)} ticker(s) failed today: {', '.join(sorted(failed))}")


if __name__ == "__main__":
    main()
