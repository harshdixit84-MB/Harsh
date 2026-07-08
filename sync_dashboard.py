"""
Scheduled job: fetches NEW matches from a Chartlink screener (to discover
stocks worth tracking), then refreshes LIVE prices for every currently
active tracked stock via Yahoo Finance -- so price stays current even
after a stock drops out of today's screener results. Merges everything
into a Google Sheet, preserving manually-set buy targets and auto-archiving
stocks that have moved 20%+ away from their target.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY (the full JSON
key content, as a string).
"""

import asyncio
import json
import os
from datetime import date

import gspread
import yfinance as yf
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

SCREENER_URL = "https://chartink.com/screener/monthly-breakouts-898"
MIN_VOLUME = 5000000  # 50,00,000
ARCHIVE_THRESHOLD_PCT = 20
SHEET_NAME = "Monthly Breakout Scan"

HEADERS = ["symbol", "name", "price", "percent_change", "volume",
           "buy_target", "status", "added_date", "archived_date", "archived_reason"]


async def fetch_raw_results():
    captured = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        async def handle_response(response):
            if "/screener/process" in response.url:
                try:
                    captured["data"] = await response.json()
                except Exception as e:
                    captured["error"] = str(e)

        page.on("response", handle_response)
        await page.goto(SCREENER_URL, wait_until="networkidle")
        await page.wait_for_timeout(3000)
        await browser.close()

    if "data" not in captured:
        raise RuntimeError(f"No scan results captured. Details: {captured}")

    return captured["data"]["data"]


def clean_and_filter(raw_rows):
    cleaned = []
    for row in raw_rows:
        if row.get("bsecode") is None:
            continue

        name = row.get("name") or ""
        if "ETF" in name.upper():
            continue

        volume = row.get("scan-column-default-volume") or 0
        if volume < MIN_VOLUME:
            continue

        cleaned.append({
            "symbol": row.get("nsecode"),
            "name": row.get("name"),
            "price": row.get("scan-column-default-close"),
            "percent_change": row.get("scan-column-default-percent-change"),
            "volume": volume,
        })

    return cleaned


async def get_filtered_screener_stocks():
    raw_rows = await fetch_raw_results()
    return clean_and_filter(raw_rows)


def _fast_info_value(info, *keys):
    for key in keys:
        try:
            value = info[key]
            if value is not None:
                return value
        except Exception:
            pass
        value = getattr(info, key, None)
        if value is not None:
            return value
    return None


def get_live_prices(symbols):
    prices = {}
    if not symbols:
        return prices

    tickers_str = " ".join(f"{s}.NS" for s in symbols)
    try:
        tickers = yf.Tickers(tickers_str)
    except Exception as e:
        print(f"Could not initialize yfinance tickers: {e}")
        return prices

    for symbol in symbols:
        try:
            info = tickers.tickers[f"{symbol}.NS"].fast_info
            last_price = _fast_info_value(info, "lastPrice", "last_price")
            prev_close = _fast_info_value(info, "previousClose", "previous_close")

            if last_price is None:
                print(f"No live price returned for {symbol}, will fall back if possible.")
                continue

            pct_change = None
            if prev_close:
                pct_change = round(((last_price - prev_close) / prev_close) * 100, 2)

            prices[symbol] = {"price": round(float(last_price), 2), "percent_change": pct_change}
        except Exception as e:
            print(f"Could not fetch live price for {symbol}: {e}")
            continue

    return prices


def get_sheet():
    key_json = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]
    key_dict = json.loads(key_json)

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open(SHEET_NAME).sheet1


def sync_stocks_to_sheet(sheet, fetched_stocks):
    existing_records = sheet.get_all_records()
    existing_by_symbol = {r["symbol"]: r for r in existing_records}
    today = str(date.today())

    fetched_by_symbol = {s["symbol"]: s for s in fetched_stocks}

    for symbol, live in fetched_by_symbol.items():
        if symbol not in existing_by_symbol:
            existing_by_symbol[symbol] = {
                "symbol": symbol,
                "name": live["name"],
                "price": live["price"],
                "percent_change": live["percent_change"],
                "volume": live["volume"],
                "buy_target": "",
                "status": "active",
                "added_date": today,
                "archived_date": "",
                "archived_reason": "",
            }

    active_symbols = [s for s, r in existing_by_symbol.items() if r.get("status") != "archived"]
    live_prices = get_live_prices(active_symbols)

    updated_rows = []
    for symbol, record in existing_by_symbol.items():
        if record.get("status") == "archived":
            updated_rows.append(record)
            continue

        if symbol in live_prices:
            record["price"] = live_prices[symbol]["price"]
            if live_prices[symbol]["percent_change"] is not None:
                record["percent_change"] = live_prices[symbol]["percent_change"]
        elif symbol in fetched_by_symbol:
            live = fetched_by_symbol[symbol]
            record["price"] = live["price"]
            record["percent_change"] = live["percent_change"]

        if symbol in fetched_by_symbol:
            record["volume"] = fetched_by_symbol[symbol]["volume"]

        buy_target = record.get("buy_target")
        if buy_target not in (None, "", 0):
            try:
                distance_pct = abs((float(record["price"]) - float(buy_target)) / float(buy_target)) * 100
                if distance_pct >= ARCHIVE_THRESHOLD_PCT:
                    record["status"] = "archived"
                    record["archived_date"] = today
                    record["archived_reason"] = f"moved {round(distance_pct)}% from target"
            except (ValueError, TypeError) as e:
                print(f"Skipping archive check for {symbol}: bad buy_target/price ({e})")

        updated_rows.append(record)

    rows_for_sheet = [[r.get(h, "") for h in HEADERS] for r in updated_rows]
    sheet.update([HEADERS] + rows_for_sheet, "A1")
    print(f"Synced {len(updated_rows)} total rows ({len(live_prices)} live prices fetched).")


async def main():
    stocks = await get_filtered_screener_stocks()
    sheet = get_sheet()
    sync_stocks_to_sheet(sheet, stocks)


if __name__ == "__main__":
    asyncio.run(main())
