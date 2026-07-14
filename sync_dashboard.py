"""
Scheduled job: fetches NEW matches from a Chartlink screener (to discover
stocks worth tracking), refreshes LIVE prices for every currently active
tracked stock via Yahoo Finance, computes a technical signal score
(trend structure, trend strength, momentum, MACD, volume confirmation,
and a consolidation/squeeze flag), then merges everything into a Google
Sheet -- preserving manually-set buy targets and auto-archiving stocks
that have moved 20%+ away from their target.

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY (the full JSON
key content, as a string).
"""

import asyncio
import json
import math
import os
from datetime import date

import gspread
import numpy as np
import pandas as pd
import ta
import yfinance as yf
from google.oauth2.service_account import Credentials
from playwright.async_api import async_playwright

SCREENER_URLS = {
    "Breakout": "https://chartink.com/screener/monthly-breakouts-898",
    "Consolidation": "https://chartink.com/screener/consolidation-20124597",
}
MIN_VOLUME = 5000000  # 50,00,000
ARCHIVE_THRESHOLD_PCT = 20
SHEET_NAME = "Monthly Breakout Scan"

HEADERS = ["symbol", "name", "source", "price", "percent_change", "volume",
           "buy_target", "status", "added_date", "archived_date", "archived_reason",
           "rsi", "adx", "signal_score", "signal_label", "consolidating"]


async def fetch_raw_results(screener_url):
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
        await page.goto(screener_url, wait_until="networkidle")
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


async def get_all_screener_stocks():
    """Fetches from every configured screener, tagging each stock with
    which screener(s) matched it today. A stock matching multiple
    screeners in the same run gets multiple source tags."""
    combined = {}

    for source_name, url in SCREENER_URLS.items():
        raw_rows = await fetch_raw_results(url)
        cleaned = clean_and_filter(raw_rows)

        for stock in cleaned:
            symbol = stock["symbol"]
            if symbol not in combined:
                combined[symbol] = {**stock, "sources_today": set()}
            combined[symbol]["sources_today"].add(source_name)

    return list(combined.values())


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


def compute_signal(symbol):
    try:
        hist = yf.Ticker(f"{symbol}.NS").history(period="1y")
        if hist.empty or len(hist) < 60:
            return None

        close = hist["Close"]
        volume = hist["Volume"]

        ema20 = close.ewm(span=20).mean()
        ema50 = close.ewm(span=50).mean()

        rsi = ta.momentum.RSIIndicator(close, window=14).rsi()
        adx_ind = ta.trend.ADXIndicator(hist["High"], hist["Low"], close, window=14).adx()
        macd_hist = ta.trend.MACD(close).macd_diff()

        bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
        bb_width = (bb.bollinger_hband() - bb.bollinger_lband()) / close

        latest_close = close.iloc[-1]
        latest_ema20 = ema20.iloc[-1]
        latest_ema50 = ema50.iloc[-1]
        latest_rsi = rsi.iloc[-1]
        latest_adx = adx_ind.iloc[-1]
        latest_macd_hist = macd_hist.iloc[-1]
        latest_bb_width = bb_width.iloc[-1]

        check_values = [latest_close, latest_ema20, latest_ema50, latest_rsi,
                         latest_adx, latest_macd_hist, latest_bb_width]
        if any(pd.isna(v) for v in check_values):
            print(f"Signal calculation for {symbol} produced NaN, skipping.")
            return None

        lookback = bb_width.iloc[-100:] if len(bb_width) >= 100 else bb_width
        bb_width_percentile = (lookback < latest_bb_width).mean() * 100

        avg_volume_20 = volume.iloc[-20:].mean()
        volume_ratio = (volume.iloc[-1] / avg_volume_20) if avg_volume_20 else 0

        score = 0
        if latest_close > latest_ema20 > latest_ema50:
            score += 1
        if latest_adx >= 20:
            score += 1
        if 50 <= latest_rsi <= 70:
            score += 1
        if latest_macd_hist > 0:
            score += 1
        if volume_ratio > 1.2:
            score += 1

        label = "Strong" if score >= 4 else "Moderate" if score >= 2 else "Weak"
        consolidating = bool(bb_width_percentile <= 20)

        return {
            "rsi": round(float(latest_rsi), 1),
            "adx": round(float(latest_adx), 1),
            "signal_score": score,
            "signal_label": label,
            "consolidating": consolidating,
        }
    except Exception as e:
        print(f"Signal calculation failed for {symbol}: {e}")
        return None


def get_signals(symbols):
    signals = {}
    for symbol in symbols:
        result = compute_signal(symbol)
        if result:
            signals[symbol] = result
    return signals


def _json_safe(value):
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return ""
        return value
    if isinstance(value, (bool, int, str)):
        return value
    if value is None:
        return ""
    return str(value)


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
        sources_today = live.get("sources_today", set())
        sources_str_today = ",".join(sorted(sources_today))

        if symbol not in existing_by_symbol:
            existing_by_symbol[symbol] = {
                "symbol": symbol,
                "name": live["name"],
                "source": sources_str_today,
                "price": live["price"],
                "percent_change": live["percent_change"],
                "volume": live["volume"],
                "buy_target": "",
                "status": "active",
                "added_date": today,
                "archived_date": "",
                "archived_reason": "",
            }
        else:
            # Union with whatever source tags this stock already earned --
            # a screener no longer matching today never removes a tag
            existing_sources = set(filter(None, existing_by_symbol[symbol].get("source", "").split(",")))
            merged_sources = existing_sources | sources_today
            existing_by_symbol[symbol]["source"] = ",".join(sorted(merged_sources))

    active_symbols = [s for s, r in existing_by_symbol.items() if r.get("status") != "archived"]
    live_prices = get_live_prices(active_symbols)
    signals = get_signals(active_symbols)

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

        if symbol in signals:
            sig = signals[symbol]
            record["rsi"] = sig["rsi"]
            record["adx"] = sig["adx"]
            record["signal_score"] = sig["signal_score"]
            record["signal_label"] = sig["signal_label"]
            record["consolidating"] = sig["consolidating"]

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

    rows_for_sheet = [[_json_safe(r.get(h, "")) for h in HEADERS] for r in updated_rows]
    sheet.update([HEADERS] + rows_for_sheet, "A1")
    print(f"Synced {len(updated_rows)} rows ({len(live_prices)} live prices, {len(signals)} signals computed).")


async def main():
    stocks = await get_all_screener_stocks()
    sheet = get_sheet()
    sync_stocks_to_sheet(sheet, stocks)


if __name__ == "__main__":
    asyncio.run(main())
