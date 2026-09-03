"""
Checks every tracked stock (active AND archived) against 5 signal filters --
Near Target, Near Stoploss, Bullish Divergence, Bearish Divergence, and
Reversal Confluence -- and sends ONE collective Telegram message per filter,
listing EVERY ticker currently matching that filter. Runs every scheduled
hour during the day and resends the full current list each time (not just
new entries) -- so whenever you check your phone, the latest message for
each filter shows the complete, current picture for that day. A filter with
zero matching stocks is simply skipped (no empty message sent).

Divergence signals are restricted to FRESH formations: RSI_Divergence tracks
daily_days_ago/weekly_days_ago/hourly_bars_ago per stock -- daily/weekly must
have formed_today (days_ago == 0), and hourly counts as fresh up to
HOURLY_NOTIFY_MAX_BARS_AGE bars back (~2 trading days on 60m candles), shown
in the alert as e.g. "1H·3b" so you can see how old it actually is. A
divergence that formed 3 days ago on the daily timeframe (even if still
within rsi_divergence.py's own 7/14-day display window) will NOT show up
here on daily/weekly -- only the hourly timeframe gets this wider window.

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
HOURLY_NOTIFY_MAX_BARS_AGE = 14  # ~2 trading days of 60m bars (NSE runs ~7 bars/day) -- notify on current AND up to 2 days old


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
    "Mirrors the relevant parts of the join logic in api/dashboard.js, in Python."
    main_rows = spreadsheet.sheet1.get_all_records()
    rsi_by_symbol = read_tab_by_symbol(spreadsheet, "RSI_Divergence")
    dv_by_symbol = read_tab_by_symbol(spreadsheet, "DV_Summary")

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
        rsi_hourly_div = rsi_div.get("hourly_divergence", "") or ""
        rsi_daily_days_ago = _to_int_or_none(rsi_div.get("daily_days_ago"))
        rsi_weekly_days_ago = _to_int_or_none(rsi_div.get("weekly_days_ago"))
        rsi_hourly_bars_ago = _to_int_or_none(rsi_div.get("hourly_bars_ago"))

        daily_formed_today = rsi_daily_days_ago == 0
        weekly_formed_today = rsi_weekly_days_ago == 0
        # Hourly: notify on current AND up to ~2 trading days old (not just the latest bar).
        hourly_recent = rsi_hourly_bars_ago is not None and rsi_hourly_bars_ago <= HOURLY_NOTIFY_MAX_BARS_AGE

        dv = dv_by_symbol.get(symbol, {})

        quality_score = _to_int_or_none(r.get("quality_score"))
        quality_flags = r.get("quality_flags", "") or ""

        stocks.append({
            "symbol": symbol,
            "source": r.get("source", ""),
            "price": price,
            "buy_target": buy_target,
            "distance_pct": distance_pct,
            "stop_loss": stop_loss,
            "distance_to_sl_pct": distance_to_sl_pct,
            "rsi_daily_divergence": rsi_daily_div,
            "rsi_weekly_divergence": rsi_weekly_div,
            "rsi_hourly_divergence": rsi_hourly_div,
            "rsi_hourly_bars_ago": rsi_hourly_bars_ago,
            "daily_formed_today": daily_formed_today,
            "weekly_formed_today": weekly_formed_today,
            "hourly_recent": hourly_recent,
            "dv_decision": dv.get("decision", "") or "",
            "dv_cross_state": dv.get("cross_state", "") or "",
            "dv_crossover_age": dv.get("crossover_age", ""),
            "dv_recent_bias": dv.get("recent_bias", "") or "",
            "quality_score": quality_score,
            "quality_flags": quality_flags,
        })

    return stocks


def compute_signals(s):
    "Returns {filter_key: (is_matching, detail_suffix)} -- one entry per tracked filter."
    daily = s["rsi_daily_divergence"].lower()
    weekly = s["rsi_weekly_divergence"].lower()
    hourly = s["rsi_hourly_divergence"].lower()

    daily_bull_today = daily == "bullish" and s["daily_formed_today"]
    weekly_bull_today = weekly == "bullish" and s["weekly_formed_today"]
    hourly_bull_recent = hourly == "bullish" and s["hourly_recent"]
    daily_bear_today = daily == "bearish" and s["daily_formed_today"]
    weekly_bear_today = weekly == "bearish" and s["weekly_formed_today"]
    hourly_bear_recent = hourly == "bearish" and s["hourly_recent"]

    hourly_tag = f"1H·{s['rsi_hourly_bars_ago']}b" if s["rsi_hourly_bars_ago"] is not None else "1H"
    bullish_tf = "+".join(filter(None, ["D" if daily_bull_today else "", "W" if weekly_bull_today else "", hourly_tag if hourly_bull_recent else ""]))
    bearish_tf = "+".join(filter(None, ["D" if daily_bear_today else "", "W" if weekly_bear_today else "", hourly_tag if hourly_bear_recent else ""]))

    near_sl_matching = s["distance_to_sl_pct"] is not None and 0 <= s["distance_to_sl_pct"] <= WATCH_THRESHOLD
    near_sl_suffix = ""
    if near_sl_matching and s["stop_loss"] not in (None, ""):
        near_sl_suffix = f" (SL: ₹{s['stop_loss']}, {s['distance_to_sl_pct']:+.2f}% away)"

    return {
        "near_target": (
            s["distance_pct"] is not None and 0 < s["distance_pct"] <= WATCH_THRESHOLD,
            "",
        ),
        "near_sl": (
            near_sl_matching,
            near_sl_suffix,
        ),
        "bullish_divergence": (
            daily_bull_today or weekly_bull_today or hourly_bull_recent,
            f" ({bullish_tf})" if bullish_tf else "",
        ),
        "bearish_divergence": (
            daily_bear_today or weekly_bear_today or hourly_bear_recent,
            f" ({bearish_tf})" if bearish_tf else "",
        ),
        "reversal": (
            daily_bull_today and weekly_bull_today,
            " (D+W same-day)",
        ),
        "confirmed_buy": (
            s["dv_decision"] == "Confirmed Buy",
            "",
        ),
        "high_quality": (
            s["quality_score"] is not None and s["quality_score"] >= 4,
            f" (Q {s['quality_score']}/5)" if s["quality_score"] is not None else "",
        ),
    }


FILTER_DISPLAY_NAMES = {
    "near_target": "Near Target",
    "near_sl": "Near Stoploss",
    "bullish_divergence": "Bullish Divergence (Daily/Weekly/1H)",
    "bearish_divergence": "Bearish Divergence (Daily/Weekly/1H)",
    "reversal": "★ Reversal Confluence (Daily+Weekly, same-day)",
    "confirmed_buy": "✅ Confirmed Buy (Delivery %)",
    "high_quality": "🌟 High Quality Breakout (Score ≥4/5)",
}


def send_telegram_message(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not resp.ok:
        print(f"Telegram send failed: {resp.status_code} {resp.text}")


def format_dv_context(stock):
    "Raw ADP_5-vs-ADP_20 crossover state + how many days it has held -- kept deliberately separate from the Confirmed Buy/Sell decision, which is its own multi-condition signal. Appended to every ticker line in every message."
    state = stock["dv_cross_state"]
    if not state:
        return ""
    age = stock["dv_crossover_age"]
    age_str = f"{age}d" if age not in (None, "") else "?d"
    arrow = "5D↗20D ADP" if state == "Bullish" else "5D↘20D ADP"
    return f" · {arrow} ({age_str})"


def quality_dot(quality_score):
    "A quick visual read of breakout quality, shown on EVERY alert regardless of which filter fired it -- so a Near Target hit that's actually a weak setup doesn't look the same as a genuinely strong one."
    if quality_score is None:
        return "⚪"
    if quality_score >= 4:
        return "🟢"
    if quality_score >= 2:
        return "🟡"
    return "🔴"


def format_price_block(stock):
    "Price + target + stop-loss with their distances, grouped together -- these are the actual numbers needed to decide on the trade, shown every time regardless of which filter triggered the alert."
    parts = [f"💰 ₹{stock['price']}"]

    if stock["buy_target"] not in (None, "", 0):
        dist = f" ({stock['distance_pct']:+.2f}%)" if stock["distance_pct"] is not None else ""
        parts.append(f"🎯 ₹{stock['buy_target']}{dist}")
    else:
        parts.append("🎯 not set")

    if stock["stop_loss"] not in (None, ""):
        dist = f" ({stock['distance_to_sl_pct']:+.2f}%)" if stock["distance_to_sl_pct"] is not None else ""
        parts.append(f"🛑 ₹{stock['stop_loss']}{dist}")

    return "  ·  ".join(parts)


def format_ticker_block(stock, detail_suffix, filter_key):
    tv_url = f"https://www.tradingview.com/chart/?symbol=NSE:{stock['symbol']}"
    symbol_link = f'<a href="{tv_url}"><b>{stock["symbol"]}</b></a>'
    quality_str = f"Q{stock['quality_score']}/5" if stock["quality_score"] is not None else "Q -"

    lines = [f"{quality_dot(stock['quality_score'])} {symbol_link}  <i>{quality_str}</i>"]
    lines.append(format_price_block(stock))

    context_bits = []
    if stock["source"]:
        context_bits.append(stock["source"])
    dv_context = format_dv_context(stock).strip(" ·")
    if dv_context:
        context_bits.append(dv_context)
    # SL details and the Q-score are already shown above for these two filters --
    # repeating them in the note line would just be noise.
    if detail_suffix and filter_key not in ("near_sl", "high_quality"):
        context_bits.append(detail_suffix.strip(" ()"))
    if context_bits:
        lines.append("📊 " + "  ·  ".join(context_bits))

    return "\n".join(lines)


def format_group_message(filter_key, entries):
    header = (
        f"🔔 <b>{FILTER_DISPLAY_NAMES[filter_key]}</b>  •  {len(entries)} stock{'s' if len(entries) != 1 else ''}\n"
        + "─" * 24
    )
    blocks = [format_ticker_block(stock, detail_suffix, filter_key) for stock, detail_suffix in entries]
    return header + "\n\n" + "\n\n".join(blocks)


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
