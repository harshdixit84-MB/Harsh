"""
Builds data/history.json — a full historical + analytical record of every
signal this system has ever produced, joined against:
  - Sheet1            (every stock ever added to the watchlist: source,
                        added_date, buy_target, status, archived_reason)
  - Trades             (what you actually bought/sold, by hand)
  - DV_History          (daily close_price per symbol -- used to reconstruct
                        the price on added_date, since Sheet1 only stores
                        the LIVE/current price, not the price at signal time)

WHY THIS EXISTS
----------------
Sheet1, Trades, and DV_History each answer a different question, but none
of them alone tells you whether the SYSTEM is working:
  - Sheet1 tells you what was flagged.
  - Trades tells you what you actually did.
  - DV_History tells you what the price actually did.

This script joins all three into one row per signal so you can answer the
two things that actually matter:
  1. Would you have done BETTER buying immediately when a stock hit the
     list ("list_price"), or waiting for your own manually-set target
     ("buy_target_manual") the way you actually traded it?
  2. Where is the execution process leaking value -- signals that never
     got traded, entries with heavy slippage, waits that dragged on for
     weeks, stocks archived before your target was ever reached, etc.
     (see LOOPHOLE FLAGS below).

OUTPUT SCHEMA (data/history.json)
----------------------------------
{
  "generated_at": "...",
  "summary": { ... aggregate stats, see build_summary() ... },
  "entries": [
    {
      "symbol", "source", "added_date", "status", "archived_reason",
      "list_price", "list_price_date_used",
      "buy_target_manual",
      "target_hit_date", "days_to_target",          # first day price touched buy_target after being flagged
      "traded": true/false,
      "actual_buy_date", "actual_buy_price", "gap_days_waited",
      "exit_date", "exit_price", "trade_status": "closed"/"open"/"never_entered",
      "immediate_entry_return_pct",   # if you'd bought at list_price
      "actual_entry_return_pct",      # what you actually realised/are holding
      "entry_diff_pct",               # actual - immediate. Negative = waiting cost you.
      "entry_slippage_pct",           # actual_buy_price vs list_price, ignoring timing
      "loophole_flags": [ ... ]
    }, ...
  ]
}

LOOPHOLE FLAGS
--------------
  never_entered          - flagged by the scanner, never appears in Trades at all
  archived_before_entry  - archived from the watchlist before you ever bought it
  large_entry_slippage    - actual buy price >5% away from the list price (bad fills / chasing)
  long_wait               - >30 days between being flagged and actually buying
  waiting_cost_you        - actual_entry_return_pct is meaningfully worse than immediate_entry_return_pct
  target_never_reached    - buy_target_manual was set but price never touched it
  duplicate_signal        - this symbol was flagged more than once (noisy screener / re-adds)
  no_price_history        - DV_History didn't have enough days to reconstruct list_price

Environment variable required: GOOGLE_SERVICE_ACCOUNT_KEY (same as the other scripts in this repo)
Run this after sync_dashboard.py / delivery_value.py in the daily workflow so DV_History is fresh.
"""

import json
import os
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
OUTPUT_PATH = "data/history.json"

LARGE_SLIPPAGE_PCT = 5.0
LONG_WAIT_DAYS = 30
WAITING_COST_THRESHOLD_PCT = 3.0     # actual return this many pts worse than immediate = "waiting cost you"
PRICE_SEARCH_WINDOW_DAYS = 10        # how far past added_date to look for a matching DV_History row


def get_client():
    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def load_sheets(spreadsheet):
    sheet1 = spreadsheet.sheet1.get_all_records()
    trades = spreadsheet.worksheet("Trades").get_all_records()
    dv_history = spreadsheet.worksheet("DV_History").get_all_records()
    return sheet1, trades, dv_history


def build_dv_lookup(dv_history):
    """symbol -> {date_str: close_price}"""
    lookup = defaultdict(dict)
    for row in dv_history:
        sym, d, cp = row.get("symbol"), row.get("date"), row.get("close_price")
        if sym and d and cp not in (None, ""):
            lookup[sym][str(d)] = float(cp)
    return lookup


def price_on_or_after(dv_lookup, symbol, date_str, window=PRICE_SEARCH_WINDOW_DAYS):
    if not date_str or symbol not in dv_lookup:
        return None, None
    hist = dv_lookup[symbol]
    d0 = datetime.strptime(str(date_str)[:10], "%Y-%m-%d")
    for i in range(window + 1):
        d = (d0 + timedelta(days=i)).strftime("%Y-%m-%d")
        if d in hist:
            return hist[d], d
    return None, None


def find_target_hit(dv_lookup, symbol, added_date, buy_target, source):
    """First date, after added_date, that price actually reached buy_target.
    Direction depends on the strategy: Breakout/Consolidation assume you're
    buying strength on a dip TOWARD the target (price <= target counts as
    'reached'); Near52WLow assumes the same (you're waiting for a better,
    lower entry). In both cases in this sheet buy_target is a LIMIT price
    you're waiting to buy at or better, so 'hit' = price <= buy_target.
    """
    if not added_date or symbol not in dv_lookup or buy_target in (None, "", 0):
        return None, None
    hist = dv_lookup[symbol]
    dates = sorted(d for d in hist if d >= str(added_date)[:10])
    for d in dates:
        if hist[d] <= float(buy_target):
            return d, (datetime.strptime(d, "%Y-%m-%d") - datetime.strptime(str(added_date)[:10], "%Y-%m-%d")).days
    return None, None


def build_entries(sheet1, trades, dv_lookup, today_str):
    symbol_counts = Counter(r["symbol"] for r in sheet1 if r.get("symbol"))
    trades_by_symbol = defaultdict(list)
    for t in trades:
        if t.get("symbol"):
            trades_by_symbol[t["symbol"]].append(t)

    entries = []
    for row in sheet1:
        symbol = row.get("symbol")
        if not symbol:
            continue
        added_date = row.get("added_date")
        source = row.get("source") or ""
        buy_target = row.get("buy_target")
        status = row.get("status")

        list_price, list_price_date_used = price_on_or_after(dv_lookup, symbol, added_date)
        target_hit_date, days_to_target = find_target_hit(dv_lookup, symbol, added_date, buy_target, source)

        # watchlist-level performance (list_price -> most recent known price), independent of
        # whether it was ever actually traded -- this is what drives the by-source aggregates.
        list_to_latest_return_pct = None
        hist = dv_lookup.get(symbol, {})
        if list_price and hist:
            latest_d = max(hist.keys())
            latest_p = hist[latest_d]
            list_to_latest_return_pct = round((latest_p - list_price) / list_price * 100, 2)

        matching_trades = trades_by_symbol.get(symbol, [])
        # use the trade whose buy_date is closest to (and >=) added_date
        trade = None
        if matching_trades:
            trade = sorted(matching_trades, key=lambda t: t.get("buy_date") or "9999")[0]

        flags = []
        if symbol_counts[symbol] > 1:
            flags.append("duplicate_signal")
        if list_price is None:
            flags.append("no_price_history")
        if buy_target and not target_hit_date:
            flags.append("target_never_reached")

        if not trade:
            flags.append("never_entered")
            if status == "archived":
                flags.append("archived_before_entry")
            entries.append({
                "symbol": symbol, "source": source, "added_date": added_date,
                "status": status, "archived_reason": row.get("archived_reason"),
                "list_price": list_price, "list_price_date_used": list_price_date_used,
                "list_to_latest_return_pct": list_to_latest_return_pct,
                "buy_target_manual": buy_target,
                "target_hit_date": target_hit_date, "days_to_target": days_to_target,
                "traded": False, "actual_buy_date": None, "actual_buy_price": None,
                "gap_days_waited": None, "exit_date": None, "exit_price": None,
                "trade_status": "never_entered",
                "immediate_entry_return_pct": None, "actual_entry_return_pct": None,
                "entry_diff_pct": None, "entry_slippage_pct": None,
                "loophole_flags": flags,
            })
            continue

        actual_buy_date = trade.get("buy_date")
        actual_buy_price = trade.get("buy_price")
        sell_price = trade.get("sell_price") or None
        sell_date = trade.get("sell_date") or None
        trade_status = "closed" if sell_price else "open"

        exit_price = sell_price
        if exit_price is None:
            # open position -- fall back to the most recent DV_History price
            hist = dv_lookup.get(symbol, {})
            if hist:
                latest_d = max(hist.keys())
                exit_price = hist[latest_d]

        gap_days_waited = None
        if added_date and actual_buy_date:
            gap_days_waited = (datetime.strptime(str(actual_buy_date)[:10], "%Y-%m-%d")
                                - datetime.strptime(str(added_date)[:10], "%Y-%m-%d")).days

        immediate_ret = None
        if list_price and exit_price:
            immediate_ret = round((exit_price - list_price) / list_price * 100, 2)

        actual_ret = None
        if actual_buy_price and exit_price:
            actual_ret = round((exit_price - actual_buy_price) / actual_buy_price * 100, 2)

        entry_diff = round(actual_ret - immediate_ret, 2) if (actual_ret is not None and immediate_ret is not None) else None

        entry_slippage = None
        if list_price and actual_buy_price:
            entry_slippage = round((actual_buy_price - list_price) / list_price * 100, 2)

        if entry_slippage is not None and abs(entry_slippage) > LARGE_SLIPPAGE_PCT:
            flags.append("large_entry_slippage")
        if gap_days_waited is not None and gap_days_waited > LONG_WAIT_DAYS:
            flags.append("long_wait")
        if entry_diff is not None and entry_diff < -WAITING_COST_THRESHOLD_PCT:
            flags.append("waiting_cost_you")
        if status == "archived" and trade_status == "open":
            flags.append("archived_before_entry")

        entries.append({
            "symbol": symbol, "source": source, "added_date": added_date,
            "status": status, "archived_reason": row.get("archived_reason"),
            "list_price": list_price, "list_price_date_used": list_price_date_used,
            "list_to_latest_return_pct": list_to_latest_return_pct,
            "buy_target_manual": buy_target,
            "target_hit_date": target_hit_date, "days_to_target": days_to_target,
            "traded": True, "actual_buy_date": actual_buy_date, "actual_buy_price": actual_buy_price,
            "gap_days_waited": gap_days_waited, "exit_date": sell_date, "exit_price": exit_price,
            "trade_status": trade_status,
            "immediate_entry_return_pct": immediate_ret, "actual_entry_return_pct": actual_ret,
            "entry_diff_pct": entry_diff, "entry_slippage_pct": entry_slippage,
            "loophole_flags": flags,
        })

    return entries


def build_summary(entries):
    traded = [e for e in entries if e["traded"]]
    never = [e for e in entries if not e["traded"]]
    both = [e for e in traded if e["immediate_entry_return_pct"] is not None and e["actual_entry_return_pct"] is not None]

    by_source = defaultdict(lambda: {"count": 0, "up": 0, "returns": []})
    for e in entries:
        for src in (e["source"] or "").split(","):
            src = src.strip()
            if not src:
                continue
            by_source[src]["count"] += 1
            if e["list_to_latest_return_pct"] is not None:
                by_source[src]["returns"].append(e["list_to_latest_return_pct"])
                if e["list_to_latest_return_pct"] > 0:
                    by_source[src]["up"] += 1

    source_summary = {}
    for src, d in by_source.items():
        n = len(d["returns"])
        source_summary[src] = {
            "signals_flagged": d["count"],
            "with_price_data": n,
            "pct_up_since_flagged": round(d["up"] / n * 100, 1) if n else None,
            "avg_return_pct": round(sum(d["returns"]) / n, 2) if n else None,
        }

    flag_counts = Counter(f for e in entries for f in e["loophole_flags"])

    avg_immediate = round(sum(e["immediate_entry_return_pct"] for e in both) / len(both), 2) if both else None
    avg_actual = round(sum(e["actual_entry_return_pct"] for e in both) / len(both), 2) if both else None
    waiting_better = sum(1 for e in both if e["entry_diff_pct"] and e["entry_diff_pct"] > 0)
    immediate_better = sum(1 for e in both if e["entry_diff_pct"] and e["entry_diff_pct"] < 0)
    avg_gap = None
    gaps = [e["gap_days_waited"] for e in traded if e["gap_days_waited"] is not None]
    if gaps:
        avg_gap = round(sum(gaps) / len(gaps), 1)

    return {
        "total_signals": len(entries),
        "total_traded": len(traded),
        "total_never_traded": len(never),
        "execution_rate_pct": round(len(traded) / len(entries) * 100, 1) if entries else None,
        "entry_timing": {
            "n_compared": len(both),
            "avg_immediate_entry_return_pct": avg_immediate,
            "avg_actual_entry_return_pct": avg_actual,
            "trades_where_waiting_was_better": waiting_better,
            "trades_where_immediate_was_better": immediate_better,
            "avg_days_waited": avg_gap,
            "verdict": (
                "IMMEDIATE entry (buy when it hits the list) has outperformed waiting for your target"
                if avg_immediate is not None and avg_actual is not None and avg_immediate > avg_actual
                else "Waiting for your manual target has outperformed immediate entry"
                if avg_immediate is not None and avg_actual is not None
                else "Not enough data yet"
            ),
        },
        "by_source": source_summary,
        "loophole_flag_counts": dict(flag_counts),
    }


def main():
    client = get_client()
    spreadsheet = client.open(SHEET_NAME)
    sheet1, trades, dv_history = load_sheets(spreadsheet)
    dv_lookup = build_dv_lookup(dv_history)

    today_str = date.today().isoformat()
    entries = build_entries(sheet1, trades, dv_lookup, today_str)
    summary = build_summary(entries)

    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "entries": entries,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2, default=str)

    print(f"Wrote {len(entries)} entries to {OUTPUT_PATH}")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
