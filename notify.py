"""
Checks every tracked stock (active AND archived) against 6 signal filters --
Near Target, Near Stoploss, Need Target Set, 20/50 EMA Crossover + Volume
Breakout, EMA Pullback, and EMA Retest (first touch since latest crossover)
-- and sends ONE collective Telegram message per filter, listing EVERY
ticker currently matching that filter. Runs every scheduled hour during the
day and resends the full current list each time (not just new entries) --
so whenever you check your phone, the latest message for each filter shows
the complete, current picture for that day. A filter with zero matching
stocks is simply skipped (no empty message sent).

Environment variables required:
  GOOGLE_SERVICE_ACCOUNT_KEY
  TELEGRAM_BOT_TOKEN
  TELEGRAM_CHAT_ID
"""
import json
import os
import time

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
    "Reads a side tab into a dict keyed by symbol. Returns {} if the tab doesn't exist, OR if reading it fails for any reason (e.g. duplicate/malformed headers) -- one broken side tab should degrade that signal, not crash the entire run and send zero messages."
    try:
        ws = spreadsheet.worksheet(tab_name)
        rows = ws.get_all_records()
        return {r["symbol"]: r for r in rows if r.get("symbol")}
    except gspread.exceptions.WorksheetNotFound:
        return {}
    except Exception as e:
        print(f"WARNING: could not read '{tab_name}' tab ({type(e).__name__}: {e}) -- continuing without it.")
        return {}


def _to_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool(value):
    return value is True or value == "TRUE" or value == "True" or value == "true"


def build_merged_stocks(spreadsheet):
    "Mirrors the relevant parts of the join logic in api/dashboard.js, in Python."
    main_rows = spreadsheet.sheet1.get_all_records()
    print(f"Sheet1: read {len(main_rows)} row(s).")
    dv_by_symbol = read_tab_by_symbol(spreadsheet, "DV_Summary")
    ema_by_symbol = read_tab_by_symbol(spreadsheet, "EMA_Signals")
    print(f"Side tabs: DV_Summary={len(dv_by_symbol)}, EMA_Signals={len(ema_by_symbol)} symbol(s).")

    stocks = []
    skipped_no_symbol = 0
    skipped_bad_price = 0
    for r in main_rows:
        symbol = r.get("symbol")
        if not symbol:
            skipped_no_symbol += 1
            continue

        try:
            price = float(r.get("price"))
        except (TypeError, ValueError):
            skipped_bad_price += 1
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

        quality_score = _to_int_or_none(r.get("quality_score"))
        quality_flags = r.get("quality_flags", "") or ""
        market_regime = r.get("market_regime", "") or ""

        ema = ema_by_symbol.get(symbol, {})
        ema_cross_signal = _to_bool(ema.get("ema_cross_signal"))
        ema_pullback_signal = _to_bool(ema.get("ema_pullback_signal"))
        ema_retest_signal = _to_bool(ema.get("ema_retest_signal"))

        stocks.append({
            "symbol": symbol,
            "source": r.get("source", ""),
            "price": price,
            "buy_target": buy_target,
            "distance_pct": distance_pct,
            "stop_loss": stop_loss,
            "distance_to_sl_pct": distance_to_sl_pct,
            "quality_score": quality_score,
            "quality_flags": quality_flags,
            "market_regime": market_regime,
            "dv_decision": dv.get("decision", "") or "",
            "dv_cross_state": dv.get("cross_state", "") or "",
            "dv_crossover_age": dv.get("crossover_age", ""),
            "dv_recent_bias": dv.get("recent_bias", "") or "",
            "ema_cross_signal": ema_cross_signal,
            "ema_cross_target": ema.get("ema_cross_target", ""),
            "ema_pullback_signal": ema_pullback_signal,
            "ema_pullback_pattern": ema.get("ema_pullback_pattern", "") or "",
            "ema_pullback_target": ema.get("ema_pullback_target", ""),
            "ema_retest_signal": ema_retest_signal,
            "ema_retest_touched": ema.get("ema_retest_touched_ema", "") or "",
            "ema_retest_days_since_cross": ema.get("ema_retest_days_since_cross", ""),
            "ema_retest_target": ema.get("ema_retest_target", ""),
        })

    print(f"Built {len(stocks)} usable stock record(s) "
          f"(skipped: {skipped_no_symbol} with no symbol, {skipped_bad_price} with unusable price).")
    return stocks


def compute_signals(s):
    "Returns {filter_key: (is_matching, detail_suffix)} -- one entry per tracked filter."
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
        "need_target": (
            s["buy_target"] in (None, "", 0),
            "",
        ),
        "ema_crossover_volume": (
            s["ema_cross_signal"],
            f" (target ₹{s['ema_cross_target']})" if s["ema_cross_signal"] else "",
        ),
        "ema_pullback": (
            s["ema_pullback_signal"],
            f" ({s['ema_pullback_pattern']} → target ₹{s['ema_pullback_target']})" if s["ema_pullback_signal"] else "",
        ),
        "ema_retest": (
            s["ema_retest_signal"],
            f" (touching {s['ema_retest_touched']} EMA, crossed {s['ema_retest_days_since_cross']}d ago → target ₹{s['ema_retest_target']})" if s["ema_retest_signal"] else "",
        ),
    }


FILTER_DISPLAY_NAMES = {
    "near_target": "Near Target",
    "near_sl": "Near Stoploss",
    "need_target": "📝 Need Target Set",
    "ema_crossover_volume": "📈 20/50 EMA Crossover + Volume Breakout",
    "ema_pullback": "↩️ EMA Pullback",
    "ema_retest": "〰️ EMA Retest (Past Crossover)",
}


def send_telegram_message(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    for attempt in range(2):  # one retry, only for a SHORT flood-control wait
        try:
            resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
        except requests.RequestException as e:
            print(f"Telegram send failed: network/request error -- {e}")
            return False

        if resp.ok:
            return True

        if resp.status_code == 429 and attempt == 0:
            try:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 0)
            except ValueError:
                retry_after = 0
            # A short cooldown (a few seconds) is worth waiting out inline. A long
            # one (which is what a compounding flood-control penalty looks like)
            # is NOT -- waiting minutes here would just make the whole workflow
            # run long, so give up on this message and let the NEXT scheduled
            # run send the (by-then-current) list instead.
            if 0 < retry_after <= 10:
                print(f"Telegram send hit a brief flood-control wait ({retry_after}s) -- retrying once.")
                time.sleep(retry_after + 1)
                continue
            print(f"Telegram send failed: {resp.status_code} {resp.text}")
            return False

        print(f"Telegram send failed: {resp.status_code} {resp.text}")
        return False

    return False


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


def compute_verdict(stock):
    "Turns the pieces you already have (market_regime, quality_score, and the\n    DV_Summary confirmation-aware `decision` field -- which itself only\n    marks something Confirmed once a cross has held CROSS_CONFIRM_DAYS+ days,\n    not on day one) into one plain-language call: BUY-READY / WATCH / AVOID.\n    This is a rule-based verdict -- deterministic and wrong sometimes, same\n    as any single signal -- meant to stop acting on a fresh, unconfirmed\n    cross or a good setup in a bad market regime."
    regime = stock["market_regime"]
    decision = stock["dv_decision"]
    quality = stock["quality_score"]
    age = stock["dv_crossover_age"]
    age_str = f"{age}d" if age not in (None, "") else "?d"

    if regime == "Bearish":
        return ("AVOID", "🚫", "Bearish market regime")
    if decision == "Confirmed Sell":
        return ("AVOID", "🚫", f"Confirmed Sell (held {age_str})")
    if quality is None:
        return ("WATCH", "⏳", "Quality score not available yet")
    if quality < 2:
        return ("AVOID", "🚫", f"Quality {quality}/5 too low")
    if decision == "Confirmed Buy":
        return ("BUY-READY", "✅", f"Confirmed Buy, held {age_str} · Quality {quality}/5")
    if decision == "Early Buy Signal":
        return ("WATCH", "⏳", f"Early signal only ({age_str}) -- not yet confirmed")
    if decision == "Early Sell Signal":
        return ("WATCH", "⏳", f"Early sell signal ({age_str}) -- watch, don't add")
    return ("WATCH", "⏳", f"No confirmed cross yet · Quality {quality}/5")


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

    verdict_label, verdict_emoji, verdict_reason = compute_verdict(stock)
    lines.append(f"{verdict_emoji} <b>{verdict_label}</b> — {verdict_reason}")

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
    # Fail fast and clearly if secrets are missing, instead of a bare KeyError
    # partway through a run (which would abort with zero messages sent and no
    # obvious reason why in the log).
    missing_env = [v for v in ("GOOGLE_SERVICE_ACCOUNT_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.environ.get(v)]
    if missing_env:
        print(f"ERROR: missing required environment variable(s): {', '.join(missing_env)}. "
              f"Check the workflow's secrets/env block -- nothing else in this script can run without these.")
        return

    client, spreadsheet = get_client_and_spreadsheet()
    stocks = build_merged_stocks(spreadsheet)
    print(f"Checking {len(stocks)} tracked stocks across {len(FILTER_DISPLAY_NAMES)} signal types.")

    matching_by_filter = {key: [] for key in FILTER_DISPLAY_NAMES}

    for stock in stocks:
        signals = compute_signals(stock)
        for filter_key, (is_matching, detail_suffix) in signals.items():
            if is_matching:
                matching_by_filter[filter_key].append((stock, detail_suffix))

    total_matches = sum(len(v) for v in matching_by_filter.values())
    print(f"Total matches across all filters: {total_matches}.")
    if total_matches == 0:
        print("No filter matched ANY stock this run. If this keeps happening, the likely causes are: "
              "sync_dashboard.py/ema_signals.py/etc. haven't run recently (stale/empty data), "
              "or a side tab's data doesn't match what this script expects. "
              "Check the counts printed above from build_merged_stocks().")

    sent_count = 0
    failed_count = 0
    sent_this_run = 0
    for filter_key, entries in matching_by_filter.items():
        if not entries:
            print(f"{FILTER_DISPLAY_NAMES[filter_key]}: 0 stocks, skipping message.")
            continue

        # Space consecutive sends out -- Telegram throttles a bot sending multiple
        # messages to the same chat back-to-back, which is exactly what firing all
        # 9 possible filter messages with zero delay was doing.
        if sent_this_run > 0:
            time.sleep(2)

        ok = send_telegram_message(format_group_message(filter_key, entries))
        sent_this_run += 1
        symbols = ", ".join(s["symbol"] for s, _ in entries)
        if ok:
            sent_count += 1
            print(f"Sent {FILTER_DISPLAY_NAMES[filter_key]} ({len(entries)}): {symbols}")
        else:
            failed_count += 1
            print(f"FAILED to send {FILTER_DISPLAY_NAMES[filter_key]} ({len(entries)}): {symbols} -- see Telegram error above.")

    print(f"Done. Sent {sent_count} message(s), {failed_count} failed, this run.")


if __name__ == "__main__":
    main()
