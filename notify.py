"""
Checks every tracked stock (active AND archived -- some signals, like the
>20%/<20% filters, only apply to extreme movers) against the same 12 signal
conditions used as quick-filter chips on the dashboard, and sends a Telegram
message the moment a stock NEWLY starts matching one.

Dedup: a "Notify_State" tab tracks whether each (symbol, signal) pair was
matching on the previous run. A message only goes out on a false->true
transition -- so a stock sitting in "Near Target" for 3 days doesn't ping
you every single run, only once when it first qualifies. If it later drops
out and re-qualifies, that's treated as a fresh signal and pings again.

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


def build_merged_stocks(spreadsheet):
    "Mirrors the join logic in api/dashboard.js, in Python, so notify.py sees exactly what the dashboard sees."
    main_rows = spreadsheet.sheet1.get_all_records()
    dv_by_symbol = read_tab_by_symbol(spreadsheet, "DV_Summary")
    harmonic_by_symbol = read_tab_by_symbol(spreadsheet, "Harmonic_Patterns")
    wm_by_symbol = read_tab_by_symbol(spreadsheet, "WM_Patterns")
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
        wm = wm_by_symbol.get(symbol, {})
        rsi_div = rsi_by_symbol.get(symbol, {})

        consolidating = str(r.get("consolidating", "")).strip().lower() == "true"
        high_dv = str(dv.get("high_dv_tag", "")).strip().lower() == "true"
        buying_selling_verdict = dv.get("buying_selling_verdict", "") or ""
        harmonic_pattern = harmonic.get("pattern_name", "") or ""
        wm_pattern = wm.get("pattern", "") or ""
        wm_status = wm.get("status", "") or ""
        rsi_daily_div = rsi_div.get("daily_divergence", "") or ""
        rsi_weekly_div = rsi_div.get("weekly_divergence", "") or ""

        reversal_score = sum([
            "bullish" in harmonic_pattern.lower(),
            rsi_daily_div.lower() == "bullish",
            rsi_weekly_div.lower() == "bullish",
            high_dv,
        ])

        stocks.append({
            "symbol": symbol,
            "name": r.get("name", ""),
            "source": r.get("source", ""),
            "price": price,
            "buy_target": buy_target,
            "distance_pct": distance_pct,
            "distance_to_sl_pct": distance_to_sl_pct,
            "consolidating": consolidating,
            "high_dv": high_dv,
            "buying_selling_verdict": buying_selling_verdict,
            "wm_pattern": wm_pattern,
            "wm_status": wm_status,
            "rsi_daily_divergence": rsi_daily_div,
            "rsi_weekly_divergence": rsi_weekly_div,
            "reversal_score": reversal_score,
        })

    return stocks


def compute_signals(s):
    "Returns {signal_key: (is_matching, display_name)} -- one entry per dashboard quick-filter chip."
    wm_upper = (s["wm_pattern"] or "").strip().upper()

    return {
        "near_target": (
            s["distance_pct"] is not None and 0 < s["distance_pct"] <= WATCH_THRESHOLD,
            "Near Target",
        ),
        "near_sl": (
            s["distance_to_sl_pct"] is not None and 0 <= s["distance_to_sl_pct"] <= WATCH_THRESHOLD,
            "Near Stoploss",
        ),
        "w_breakout": (
            wm_upper.startswith("W"),
            '"W" Breakout',
        ),
        "m_breakout": (
            wm_upper.startswith("M"),
            '"M" Breakout',
        ),
        "squeeze": (
            s["consolidating"],
            "Squeeze",
        ),
        "rsi_d_bull": (
            s["rsi_daily_divergence"].lower() == "bullish",
            "RSI Daily Bullish Divergence",
        ),
        "rsi_d_bear": (
            s["rsi_daily_divergence"].lower() == "bearish",
            "RSI Daily Bearish Divergence",
        ),
        "rsi_w_bull": (
            s["rsi_weekly_divergence"].lower() == "bullish",
            "RSI Weekly Bullish Divergence",
        ),
        "rsi_w_bear": (
            s["rsi_weekly_divergence"].lower() == "bearish",
            "RSI Weekly Bearish Divergence",
        ),
        "above20": (
            s["distance_pct"] is not None and s["distance_pct"] > 20,
            "Moved >20% Above Target",
        ),
        "below20": (
            s["distance_pct"] is not None and s["distance_pct"] < -20,
            "Dropped >20% Below Target",
        ),
        "reversal": (
            s["reversal_score"] >= 3,
            "★ Reversal Confluence",
        ),
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


def format_message(stock, signal_name):
    target_str = f"₹{stock['buy_target']}" if stock["buy_target"] not in (None, "", 0) else "not set"
    return (
        f"🔔 <b>{stock['symbol']}</b>\n"
        f"Signal: {signal_name}\n"
        f"Source: {stock['source'] or '-'}\n"
        f"Price: ₹{stock['price']}\n"
        f"Target: {target_str}"
    )


def main():
    import datetime
    today_str = str(datetime.date.today())

    client, spreadsheet = get_client_and_spreadsheet()
    stocks = build_merged_stocks(spreadsheet)
    print(f"Checking {len(stocks)} tracked stocks across 12 signal types.")

    state_ws, state = load_state(spreadsheet)
    new_state = {}
    sent_count = 0

    for stock in stocks:
        signals = compute_signals(stock)
        for signal_key, (is_matching, display_name) in signals.items():
            state_key = f"{stock['symbol']}|{signal_key}"
            was_matching = state.get(state_key, False)
            new_state[state_key] = is_matching

            if is_matching and not was_matching:
                send_telegram_message(format_message(stock, display_name))
                sent_count += 1
                print(f"Notified: {stock['symbol']} -> {display_name}")

    save_state(state_ws, new_state, today_str)
    print(f"Done. Sent {sent_count} new notification(s) this run.")


if __name__ == "__main__":
    main()
