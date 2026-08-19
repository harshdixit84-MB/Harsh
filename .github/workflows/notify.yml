"""
Checks every tracked stock (active AND archived) against 5 signal filters --
Near Target, Near Stoploss, Bullish Divergence, Bearish Divergence, and
Reversal Confluence -- and sends ONE collective Telegram message per filter,
listing every ticker that newly qualifies this run. No message is sent for
a filter if nothing new qualifies.

Divergence signals are restricted to patterns that FORMED TODAY: RSI_Divergence
already tracks daily_days_ago/weekly_days_ago per stock, so a divergence that
formed 3 days ago (even if still within rsi_divergence.py's own 7/14-day
display window) will NOT trigger a notification here -- only days_ago == 0
counts. Reversal Confluence's own RSI sub-signals use the same same-day rule;
its harmonic-pattern and high-DV sub-signals have no formation date available
in the sheet, so those stay presence-based (can't be restricted to "today").
Near Target / Near Stoploss are continuous price-relative conditions, not
one-off pattern events -- they don't have a "formed today" concept, so they
rely only on the state-transition dedup below (which already means "first
run this condition became true").

Dedup: a "Notify_State" tab tracks whether each (symbol, filter) pair was
matching on the previous run. Only a false->true transition counts as "new"
and gets included in that filter's message.

Environment variables required:
  GOOGLE_SERVICE_ACCOUNT_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import datetime
import json
import os

import gspread
import requests
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
STATE_SHEET = "Notify_State"
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
    "Mirrors the join logic in api/dashboard.js, in Python, so notify.py sees exactly what the dashboard sees."
    main_rows = spreadsheet.sheet1.get_all_records()
    dv_by_symbol = read_tab_by_symbol(spreadsheet, "DV_Summary")
    harmonic_by_symbol = read_tab_by_symbol(spreadsheet, "Harmonic_Patterns")
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

        dv = dv_by_symbol.get(symbol, {})
        harmonic = harmonic_by_symbol.get(symbol, {})
        rsi_div = rsi_by_symbol.get(symbol, {})

        high_dv = str(dv.get("high_dv_tag", "")).strip().lower() == "true"
        harmonic_pattern = harmonic.get("pattern_name", "") or ""
        rsi_daily_div = rsi_div.get("daily_divergence", "") or ""
        rsi_weekly_div = rsi_div.get("weekly_divergence", "") or ""
        rsi_daily_days_ago = _to_int_or_none(rsi_div.get("daily_days_ago"))
        rsi_weekly_days_ago = _to_int_or_none(rsi_div.get("weekly_days_ago"))

        daily_formed_today = rsi_daily_days_ago == 0
        weekly_formed_today = rsi_weekly_days_ago == 0

        # Reversal's RSI sub-signals also require same-day formation; harmonic
        # pattern and high-DV have no formation date in the sheet, so they
        # stay presence-based.
        reversal_score = sum([
            "bullish" in harmonic_pattern.lower(),
            rsi_daily_div.lower() == "bullish" and daily_formed_today,
            rsi_weekly_div.lower() == "bullish" and weekly_formed_today,
            high_dv,
        ])

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
            "reversal_score": reversal_score,
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
            s["reversal_score"] >= 3,
            f" ({s['reversal_score']}/4)",
        ),
    }


FILTER_DISPLAY_NAMES = {
    "near_target": "Near Target",
    "near_sl": "Near Stoploss",
    "bullish_divergence": "Bullish Divergence (formed today)",
    "bearish_divergence": "Bearish Divergence (formed today)",
    "reversal": "★ Reversal Confluence",
}


def load_state(spreadsheet):
    try:
        ws = spreadsheet.worksheet(STATE_SHEET)
    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=STATE_SHEET, rows=1000, cols=3)
        ws.update([["key", "state", "last_updated"]], "A1")
        return ws, {}

    rows = ws.get_all_records()
    state = {r["key"]: str(r.get("state", "")).strip().lower() == "true" for r in rows if r.get("key")}
    return ws, state


def save_state(ws, state, today_str):
    header = ["key", "state", "last_updated"]
    rows = [[key, str(is_on), today_str] for key, is_on in sorted(state.items())]
    ws.update([header] + rows, "A1")


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
    today_str = str(datetime.date.today())

    client, spreadsheet = get_client_and_spreadsheet()
    stocks = build_merged_stocks(spreadsheet)
    print(f"Checking {len(stocks)} tracked stocks across {len(FILTER_DISPLAY_NAMES)} signal types.")

    state_ws, state = load_state(spreadsheet)
    new_state = {}
    newly_matching_by_filter = {key: [] for key in FILTER_DISPLAY_NAMES}

    for stock in stocks:
        signals = compute_signals(stock)
        for filter_key, (is_matching, detail_suffix) in signals.items():
            state_key = f"{stock['symbol']}|{filter_key}"
            was_matching = state.get(state_key, False)
            new_state[state_key] = is_matching

            if is_matching and not was_matching:
                newly_matching_by_filter[filter_key].append((stock, detail_suffix))

    sent_count = 0
    for filter_key, entries in newly_matching_by_filter.items():
        if not entries:
            continue
        send_telegram_message(format_group_message(filter_key, entries))
        sent_count += 1
        symbols = ", ".join(s["symbol"] for s, _ in entries)
        print(f"Notified {FILTER_DISPLAY_NAMES[filter_key]}: {symbols}")

    save_state(state_ws, new_state, today_str)
    print(f"Done. Sent {sent_count} group notification(s) this run.")


if __name__ == "__main__":
    main()
