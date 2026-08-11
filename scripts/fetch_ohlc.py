"""
fetch_ohlc.py

Pulls daily OHLC candles for every symbol tracked in the dashboard sheet
(from Sheet1) using Yahoo Finance (free, no API key needed), and writes
them into a new "OHLC_Daily" tab in the same Google Sheet.

Run this on a schedule (see .github/workflows/ohlc.yml) -- each run
overwrites the tab with a fresh rolling window of candles, so the sheet
never grows unbounded.

Requires:
    pip install yfinance google-api-python-client google-auth

Env vars (same ones your other scripts already use):
    GOOGLE_SERVICE_ACCOUNT_KEY  -- JSON string of the service account key
    SHEET_ID                    -- the spreadsheet ID

IMPORTANT: your existing service account key was likely created with the
read-only Sheets scope (see api/dashboard.js). To let this script WRITE
to the sheet, the service account being used here needs the
"https://www.googleapis.com/auth/spreadsheets" scope (not the
".readonly" variant). If you're reusing the same key, just make sure the
scope below is granted -- no need to regenerate the key itself, scope is
requested per-call.
"""

import os
import json
import time
from datetime import datetime, timedelta

import yfinance as yf
from google.oauth2 import service_account
from googleapiclient.discovery import build

SHEET_ID = os.environ["SHEET_ID"]
CREDENTIALS = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
TAB_NAME = "OHLC_Daily"
LOOKBACK_PERIOD = "6y"  # how much history to keep in the rolling window


def get_sheets_service():
    creds = service_account.Credentials.from_service_account_info(
        CREDENTIALS,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds)


def get_all_symbols(service):
    """Pull the unique symbol list straight from Sheet1, same source dashboard.js uses."""
    resp = service.spreadsheets().values().get(
        spreadsheetId=SHEET_ID, range="Sheet1!A1:A1000"
    ).execute()
    rows = resp.get("values", [])
    if not rows:
        return []
    header = rows[0]
    if "symbol" not in header:
        return []
    symbol_idx = header.index("symbol")
    symbols = {row[symbol_idx] for row in rows[1:] if len(row) > symbol_idx and row[symbol_idx]}
    return sorted(symbols)


def fetch_candles(symbol):
    """Fetch daily candles for one NSE symbol via Yahoo Finance."""
    ticker = f"{symbol}.NS"
    try:
        hist = yf.Ticker(ticker).history(period=LOOKBACK_PERIOD, interval="1d")
    except Exception as e:
        print(f"  [skip] {symbol}: {e}")
        return []

    if hist is None or hist.empty:
        print(f"  [empty] {symbol}: no data returned")
        return []

    rows = []
    skipped = 0
    for date, row in hist.iterrows():
        o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
        v = row["Volume"]

        # Yahoo occasionally returns NaN for thinly-traded days -- NaN isn't
        # valid JSON, so a single bad row would break the entire batch write.
        # Skip any row with missing OHLC data rather than sending it upstream.
        if any(map(lambda x: x is None or x != x, [o, h, l, c])):  # x != x is a NaN check
            skipped += 1
            continue

        rows.append([
            symbol,
            date.strftime("%Y-%m-%d"),
            round(float(o), 2),
            round(float(h), 2),
            round(float(l), 2),
            round(float(c), 2),
            int(v) if v == v else 0,  # guard volume NaN too, default to 0
        ])

    if skipped:
        print(f"  [warn] {symbol}: skipped {skipped} row(s) with NaN OHLC values")

    return rows


def ensure_tab_exists(service):
    meta = service.spreadsheets().get(spreadsheetId=SHEET_ID).execute()
    existing_tabs = [s["properties"]["title"] for s in meta["sheets"]]
    if TAB_NAME not in existing_tabs:
        service.spreadsheets().batchUpdate(
            spreadsheetId=SHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": TAB_NAME}}}]},
        ).execute()
        print(f"Created new tab: {TAB_NAME}")


def write_candles(service, all_rows):
    ensure_tab_exists(service)

    # Clear the existing tab first so we don't accumulate duplicate/stale rows.
    service.spreadsheets().values().clear(
        spreadsheetId=SHEET_ID, range=f"{TAB_NAME}!A1:Z10000000"
    ).execute()

    header = ["symbol", "date", "open", "high", "low", "close", "volume"]

    # Writing everything in one request is what caused the 500 error --
    # 300k+ rows in a single payload is too large for the Sheets API to
    # handle reliably. Write the header once, then the data in batches.
    CHUNK_SIZE = 20000  # rows per request -- comfortably under API limits

    write_chunk_with_retry(service, f"{TAB_NAME}!A1", [header])

    next_row = 2  # header occupies row 1
    for i in range(0, len(all_rows), CHUNK_SIZE):
        chunk = all_rows[i : i + CHUNK_SIZE]
        write_chunk_with_retry(service, f"{TAB_NAME}!A{next_row}", chunk)
        next_row += len(chunk)
        print(f"  wrote rows {i + 1}-{i + len(chunk)} of {len(all_rows)}")


def write_chunk_with_retry(service, range_, values, max_retries=3):
    """Write one chunk, retrying on transient 5xx errors from the Sheets API."""
    from googleapiclient.errors import HttpError

    for attempt in range(1, max_retries + 1):
        try:
            service.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=range_,
                valueInputOption="RAW",
                body={"values": values},
            ).execute()
            return
        except HttpError as e:
            if e.resp.status >= 500 and attempt < max_retries:
                wait = 5 * attempt
                print(f"  [retry] Sheets API {e.resp.status} error, retrying in {wait}s (attempt {attempt}/{max_retries})")
                time.sleep(wait)
                continue
            raise


def main():
    service = get_sheets_service()
    symbols = get_all_symbols(service)
    print(f"Fetching OHLC for {len(symbols)} symbols...")

    all_rows = []
    for i, symbol in enumerate(symbols, 1):
        rows = fetch_candles(symbol)
        all_rows.extend(rows)
        print(f"[{i}/{len(symbols)}] {symbol}: {len(rows)} candles")
        time.sleep(0.3)  # be gentle with Yahoo Finance, avoid rate-limit blocks

    print(f"Writing {len(all_rows)} total rows to '{TAB_NAME}' tab...")
    write_candles(service, all_rows)
    print("Done.")


if __name__ == "__main__":
    main()
