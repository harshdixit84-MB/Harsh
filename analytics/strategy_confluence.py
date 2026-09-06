"""
For every ticker in data/history.json, checks which of the 4 strategies
from nse-stock-chatbot (Volume Breakout, EMA Crossover, RSI Divergence,
EMA Pullback) were ACTUALLY triggered on the exact day it was added to
this watchlist.

This answers: "was this Breakout/Consolidation/Near52WLow pick also
independently confirmed by one of the chatbot's own strategies on the
same day?" -- i.e. signal confluence.

HOW IT WORKS
------------
The 4 strategy modules are vendored as-is from nse-stock-chatbot
(chatbot_strategies/) -- same code, not a reimplementation, so the
result matches exactly what /analyze or the Telegram bot would have said
if you'd run it on that symbol on that day.

Each strategy's evaluate(df) function only looks at whether the LAST row
of the dataframe qualifies (see backtest.py's day-by-day walk in that
repo -- this uses the same technique). So for a ticker added on date D,
we fetch OHLCV up to and including D, and call each evaluate() on that
slice. Whatever fires is "the strategy that triggered on the day this
ticker appeared."

WHY NOT USE DV_History (already in this sheet)?
--------------------------------------------------
DV_History has High/Low/Volume but no Open (needed for EMA Pullback's
candle-pattern check), and only covers a rolling ~120-day window --
not long enough lookback for RSI Divergence (needs ~84 prior trading
days) or the EMA50-based strategies for tickers added early in that
window. yfinance gives full depth with no gaps, so it's used instead.

OUTPUT
------
Rewrites data/history.json in place, adding to every entry:
  "chatbot_strategies_triggered": ["EMA Pullback", "RSI Divergence", ...],
  "chatbot_strategy_count": 2,
  "chatbot_strategy_plans": { "EMA Pullback": {...entry/stop/target...}, ... }

and adds to summary:
  "chatbot_confluence": {
      "trigger_counts": {"EMA Pullback": N, "EMA Crossover": N, ...},
      "signals_with_0_confluence": N,   # none of the 4 fired that day
      "signals_with_1plus_confluence": N,
      "avg_return_by_confluence_count": {"0": x%, "1": y%, "2+": z%},
      "by_harsh_source_x_chatbot_strategy": { "Breakout": {"EMA Pullback": N, ...}, ... }
  }

Requires: pip install yfinance pandas
Run this AFTER build_history.py (it reads and rewrites data/history.json).
Network-heavy: fetches ~1.5 years of daily data per unique symbol, so it's
slow (expect several minutes for ~250 symbols) -- run it in GitHub Actions,
not interactively.
"""

import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "chatbot_strategies"))
import strategy            # EMA Pullback
import ema_crossover       # EMA Crossover
import breakout             # Volume Breakout
import rsi_divergence      # RSI Divergence

HISTORY_PATH = "data/history.json"
NSE_SUFFIX = ".NS"
LOOKBACK_CALENDAR_DAYS = 550   # comfortably covers RSI Divergence's ~84-bar minimum + buffer

STRATEGIES = {
    "EMA Pullback": strategy,
    "EMA Crossover": ema_crossover,
    "Volume Breakout": breakout,
    "RSI Divergence": rsi_divergence,
}

_price_cache = {}


def fetch_ohlcv(symbol, up_to_date):
    """OHLCV for this symbol, cached ONCE per symbol using the LATEST
    up_to_date requested across all of that symbol's entries (a symbol can
    appear more than once in history.json with different added_dates, e.g.
    flagged again months later). Caching by the max date needed, then
    slicing per-entry with df.loc[:added_date], guarantees every entry
    gets evaluated against its own correct date, not whichever date
    happened to be fetched first.
    """
    cache_key = symbol
    cached = _price_cache.get(cache_key)
    if cached is not None and cached["up_to"] >= up_to_date:
        return cached["df"]

    end = (datetime.strptime(up_to_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    start = (datetime.strptime(up_to_date, "%Y-%m-%d") - timedelta(days=LOOKBACK_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(symbol + NSE_SUFFIX).history(start=start, end=end)
        time.sleep(0.15)  # be polite to Yahoo's rate limits across ~250+ symbols
    except Exception:
        df = pd.DataFrame()
    if not df.empty:
        df.index = df.index.strftime("%Y-%m-%d")
    _price_cache[cache_key] = {"df": df, "up_to": up_to_date}
    return df


def normalize_date(d):
    return str(d)[:10] if d else None


def check_confluence(symbol, added_date):
    added_date = normalize_date(added_date)
    if not added_date:
        return [], {}
    df = fetch_ohlcv(symbol, added_date)
    if df.empty:
        return [], {}

    # df.loc[:added_date] works even if added_date itself isn't a trading
    # day (weekend/holiday) -- it correctly includes everything up to and
    # including the most recent prior trading day. But if added_date falls
    # entirely before the fetched data starts, or the resulting slice's
    # last row isn't actually on/near added_date, bail out rather than
    # silently evaluating the wrong day.
    sliced = df.loc[:added_date]
    if sliced.empty:
        return [], {}
    last_date_in_slice = sliced.index[-1]
    # guard: the slice's last date should be within a few calendar days of
    # added_date (covers weekends/holidays) -- if it's off by more than
    # that, the requested date is outside this symbol's fetched range.
    gap_days = (datetime.strptime(added_date, "%Y-%m-%d") - datetime.strptime(last_date_in_slice, "%Y-%m-%d")).days
    if gap_days > 5 or gap_days < 0:
        return [], {}

    sub_df = sliced.reset_index(drop=True)

    triggered, plans = [], {}
    for name, module in STRATEGIES.items():
        try:
            signal = module.evaluate(sub_df)
        except Exception:
            signal = None
        if signal is not None:
            triggered.append(name)
            plans[name] = signal
    return triggered, plans


def build_confluence_summary(entries):
    trigger_counts = Counter()
    return_by_count = defaultdict(list)
    by_source_x_strategy = defaultdict(lambda: Counter())
    zero, one_plus = 0, 0

    for e in entries:
        strategies = e.get("chatbot_strategies_triggered", [])
        n = len(strategies)
        for s in strategies:
            trigger_counts[s] += 1
        bucket = "0" if n == 0 else ("1" if n == 1 else "2+")
        ret = e.get("list_to_latest_return_pct")
        if ret is not None:
            return_by_count[bucket].append(ret)
        if n == 0:
            zero += 1
        else:
            one_plus += 1
        for src in (e.get("source") or "").split(","):
            src = src.strip()
            if not src:
                continue
            for s in strategies:
                by_source_x_strategy[src][s] += 1

    avg_return_by_count = {
        k: round(sum(v) / len(v), 2) if v else None for k, v in return_by_count.items()
    }

    return {
        "trigger_counts": dict(trigger_counts),
        "signals_with_0_confluence": zero,
        "signals_with_1plus_confluence": one_plus,
        "avg_return_by_confluence_count": avg_return_by_count,
        "by_harsh_source_x_chatbot_strategy": {k: dict(v) for k, v in by_source_x_strategy.items()},
    }


def main():
    with open(HISTORY_PATH) as f:
        data = json.load(f)

    entries = data.get("entries", [])
    total = len(entries)

    # Process each symbol's LATEST added_date first, so the first fetch for
    # that symbol already covers the full range needed -- every other
    # occurrence of the same symbol then just slices the same cached data
    # instead of triggering a redundant re-fetch.
    order = sorted(range(total), key=lambda i: (entries[i]["symbol"], normalize_date(entries[i].get("added_date")) or ""), reverse=True)

    for n, i in enumerate(order):
        e = entries[i]
        symbol, added_date = e["symbol"], e.get("added_date")
        triggered, plans = check_confluence(symbol, added_date)
        e["chatbot_strategies_triggered"] = triggered
        e["chatbot_strategy_count"] = len(triggered)
        e["chatbot_strategy_plans"] = plans
        if (n + 1) % 25 == 0:
            print(f"  {n+1}/{total} processed...")

    data["summary"]["chatbot_confluence"] = build_confluence_summary(entries)
    data["generated_at"] = datetime.now().isoformat(timespec="seconds")

    with open(HISTORY_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)

    print(f"Done. Wrote confluence data for {total} entries -> {HISTORY_PATH}")
    print(json.dumps(data["summary"]["chatbot_confluence"], indent=2, default=str))


if __name__ == "__main__":
    main()
