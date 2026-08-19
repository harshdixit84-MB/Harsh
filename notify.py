"""
Checks every tracked stock (active AND archived) against 5 signal filters --
Near Target, Near Stoploss, Bullish Divergence, Bearish Divergence, and
Reversal Confluence -- and sends ONE collective Telegram message per filter,
listing EVERY ticker currently matching that filter. Runs every scheduled
hour during the day and resends the full current list each time (not just
new entries) -- so whenever you check your phone, the latest message for
each filter shows the complete, current picture for that day. A filter with
zero matching stocks is simply skipped (no empty message sent).

Divergence signals are restricted to patterns that FORMED TODAY: RSI_Divergence
already tracks daily_days_ago/weekly_days_ago per stock, so a divergence that
formed 3 days ago (even if still within rsi_divergence.py's own 7/14-day
display window) will NOT show up here -- only days_ago == 0 counts.

Reversal Confluence here is a STRICTER, same-day-only definition than the
dashboard's 3-of-4 version: it requires BOTH daily AND weekly RSI bullish
divergence to have formed on the same day. The dashboard's other two
sub-signals (harmonic pattern, high delivery value) have no formation date
anywhere in the sheet, so they can't be checked for "today" and were dropped
here rather than left as a same-day/no-date inconsistency within one filter.

Environment variables required:
  GOOGLE_SERVICE_ACCOUNT_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import json
import os

import gspread
import requests
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
WATCH_THRESHOLD = 2  # same 2% band used for Near Target / Near SL on the dashboard


def get_client_and_spreadsheet():
    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    client = gspread.authorize(creds)
    return client, client.open(SHEET_NAME)


def read_tab_by_symbol(spreadsheet, tab_name):
    "Reads a side tab into a dict keyed by symbol. Returns {} if the tab doesn't exist yet."
    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.exceptions.WorksheetNotFound:
        return {}
    rows = ws.get_all_records()
    return {r["symbol"]: r for r in rows if r.get("symbol")}


def _to_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_merged_stocks(spreadsheet):
    "Mirrors the relevant parts of the join logic in api/dashboard.js, in Python. Only reads RSI_Divergence -- DV_Summary/Harmonic_Patterns aren't needed since Reversal here is RSI-only (see docstring)."
    main_rows = spreadsheet.sheet1.get_all_records()
    rsi_by_symbol = read_tab_by_symbol(spreadsheet, "RSI_Divergence")

    stocks = []
    for r in main_rows:
        symbol = r.get("symbol")
        if not symbol:
            continue

        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            continue  # no usable price, skip this stock entirely

        buy_target = r.get("buy_target")
        distance_pct = None
        if buy_target not in (None, "", 0):
            try:
                distance_pct = round((price - float(buy_target)) / float(buy_target) * 100, 2)
            except (TypeError, ValueError):
                pass

        stop_loss = r.get("stop_loss")
        distance_to_sl_pct = None
        if stop_loss not in (None, "", 0):
            try:
                distance_to_sl_pct = round((price - float(stop_loss)) / float(stop_loss) * 100, 2)
            except (TypeError, ValueError):
                pass

        rsi_div = rsi_by_symbol.get(symbol, {})
        rsi_daily_div = rsi_div.get("daily_divergence", "") or ""
        rsi_weekly_div = rsi_div.get("weekly_divergence", "") or ""
        rsi_daily_days_ago = _to_int_or_none(rsi_div.get("daily_days_ago"))
        rsi_weekly_days_ago = _to_int_or_none(rsi_div.get("weekly_days_ago"))

        daily_formed_today = rsi_daily_days_ago == 0
        weekly_formed_today = rsi_weekly_days_ago == 0

        stocks.append({
            "symbol": symbol,
            "source": r.get("source", ""),
            "price": price,
            "buy_target": buy_target,
            "distance_pct": distance_pct,
            "distance_to_sl_pct": distance_to_sl_pct,
            "rsi_daily_divergence": rsi_daily_div,
            "rsi_weekly_divergence": rsi_weekly_div,
            "daily_formed_today": daily_formed_today,
            "weekly_formed_today": weekly_formed_today,
        })

    return stocks


def compute_signals(s):
    "Returns {filter_key: (is_matching, detail_suffix)} -- one entry per tracked filter."
    daily = s["rsi_daily_divergence"].lower()
    weekly = s["rsi_weekly_divergence"].lower()

    daily_bull_today = daily == "bullish" and s["daily_formed_today"]
    weekly_bull_today = weekly == "bullish" and s["weekly_formed_today"]
    daily_bear_today = daily == "bearish" and s["daily_formed_today"]
    weekly_bear_today = weekly == "bearish" and s["weekly_formed_today"]

    bullish_tf = "+".join(filter(None, ["D" if daily_bull_today else "", "W" if weekly_bull_today else ""]))
    bearish_tf = "+".join(filter(None, ["D" if daily_bear_today else "", "W" if weekly_bear_today else ""]))

    return {
        "near_target": (
            s["distance_pct"] is not None and 0 < s["distance_pct"] <= WATCH_THRESHOLD,
            "",
        ),
        "near_sl": (
            s["distance_to_sl_pct"] is not None and 0 <= s["distance_to_sl_pct"] <= WATCH_THRESHOLD,
            "",
        ),
        "bullish_divergence": (
            daily_bull_today or weekly_bull_today,
            f" ({bullish_tf})" if bullish_tf else "",
        ),
        "bearish_divergence": (
            daily_bear_today or weekly_bear_today,
            f" ({bearish_tf})" if bearish_tf else "",
        ),
        "reversal": (
            daily_bull_today and weekly_bull_today,
            " (D+W same-day)",
        ),
    }


FILTER_DISPLAY_NAMES = {
    "near_target": "Near Target",
    "near_sl": "Near Stoploss",
    "bullish_divergence": "Bullish Divergence (formed today)",
    "bearish_divergence": "Bearish Divergence (formed today)",
    "reversal": "★ Reversal Confluence (Daily+Weekly, same-day)",
}


def send_telegram_message(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def format_ticker_line(stock, detail_suffix):
    target_str = f"₹{stock['buy_target']}" if stock["buy_target"] not in (None, "", 0) else "target not set"
    return f"<b>{stock['symbol']}</b>{detail_suffix} — ₹{stock['price']} — Target: {target_str} — {stock['source'] or '-'}"


def format_group_message(filter_key, entries):
    header = f"🔔 <b>{FILTER_DISPLAY_NAMES[filter_key]}</b> ({len(entries)} stock{'s' if len(entries) != 1 else ''})\n"
    lines = [format_ticker_line(stock, detail_suffix) for stock, detail_suffix in entries]
    return header + "\n".join(lines)


def main():
    client, spreadsheet = get_client_and_spreadsheet()
    stocks = build_merged_stocks(spreadsheet)
    print(f"Checking {len(stocks)} tracked stocks across {len(FILTER_DISPLAY_NAMES)} signal types.")

    matching_by_filter = {key: [] for key in FILTER_DISPLAY_NAMES}

    for stock in stocks:
        signals = compute_signals(stock)
        for filter_key, (is_matching, detail_suffix) in signals.items():
            if is_matching:
                matching_by_filter[filter_key].append((stock, detail_suffix))

    sent_count = 0
    for filter_key, entries in matching_by_filter.items():
        if not entries:
            print(f"{FILTER_DISPLAY_NAMES[filter_key]}: 0 stocks, skipping message.")
            continue
        send_telegram_message(format_group_message(filter_key, entries))
        sent_count += 1
        symbols = ", ".join(s["symbol"] for s, _ in entries)
        print(f"Sent {FILTER_DISPLAY_NAMES[filter_key]} ({len(entries)}): {symbols}")

    print(f"Done. Sent {sent_count} message(s) this run.")


if __name__ == "__main__":
    main()
