"""
F&O 3-Day Move Screener -- CE/PE candidate finder
====================================================

Scans the current NSE F&O stock universe and shortlists names that, on pure
PRICE ACTION + VOLUME (no RSI/MACD/ADX -- see analytics/config.py), look
set up to move at least FNO_MOVE_THRESHOLD (5%) within FNO_MAX_HOLD_DAYS (3)
trading days. Meant for buying option lots (CE on bullish setups, PE on
bearish setups) and holding up to 3 days -- not an intraday tool.

WHERE DATA COMES FROM
----------------------
- F&O universe: Angel One's public scrip master JSON (same URL already used
  by api/fno-list.js -- no login required for this one).
- Daily OHLCV: yfinance, same as analytics/strategy_confluence.py, so no
  Angel session/TOTP is needed just to run the screen. (Angel login is only
  needed later, at order-placement time -- see api/option-chain.js for the
  existing angelLogin() pattern to reuse for that.)

SETUP LOGIC (price action + volume only)
------------------------------------------
1. Range contraction -- last FNO_CONTRACTION_DAYS daily ranges (high-low as
   % of close) are tighter than the prior FNO_BASE_DAYS average. A stock
   going quiet often precedes a sharp move.
2. Volume surge -- latest volume >= FNO_VOL_MULTIPLIER x its own
   FNO_VOL_AVG_DAYS average volume.
3. Breakout direction -- close at/above the FNO_BREAKOUT_LOOKBACK_DAYS-day
   high -> CE candidate; at/below the low -> PE candidate.
4. Relative strength vs NIFTY over FNO_RS_LOOKBACK_DAYS -- must agree with
   the breakout direction (filters out stocks just riding the index).
5. Liquidity floor -- FNO_MIN_AVG_VOLUME, same floor used by every other
   strategy in this repo.

A stock needs (1) AND (2) AND (3) AND liquidity to be shortlisted; (4) is a
pass/fail direction filter.

OUTPUT
------
Writes data/fno_3day_candidates.json:
{
  "generated_at": "...",
  "config": {...},
  "candidates": [
    {"symbol": "...", "setup": "CE"|"PE", "close": ..., "relative_strength_pct": ...,
     "volume_vs_avg": ..., "stop_ref": ..., "note": "..."},
    ...
  ],
  "backtest": {
     "SYMBOL": {"CE_signals": N, "CE_hit_rate_pct": x, "PE_signals": N, "PE_hit_rate_pct": y},
     ...
  }
}

The "backtest" block replays this exact setup over each shortlisted
symbol's own history and reports how often it actually delivered >=5%
within 3 days historically -- check this before trusting a fresh signal.

Run: pip install yfinance pandas requests
     python analytics/fno_3day_move_screener.py
Network-heavy (one yfinance fetch per F&O stock) -- run in GitHub Actions
or interactively with patience, same caveat as strategy_confluence.py.
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta

import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(__file__))
import config as cfg

SCRIP_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
NSE_SUFFIX = ".NS"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "fno_3day_candidates.json")
HISTORY_CALENDAR_DAYS = 400  # comfortably covers FNO_BASE_DAYS + FNO_CONTRACTION_DAYS + backtest lookback


# ----------------------------- Universe ----------------------------- #

def get_fno_symbols():
    """F&O stock underlyings, from Angel's public scrip master (no login needed).
    Same filter as api/fno-list.js: NFO options on individual stocks/index."""
    resp = requests.get(SCRIP_MASTER_URL, timeout=30)
    resp.raise_for_status()
    all_instruments = resp.json()
    names = set()
    for inst in all_instruments:
        if inst.get("exch_seg") == "NFO" and inst.get("instrumenttype") == "OPTSTK":
            names.add(inst["name"])
    return sorted(names)


# ----------------------------- OHLCV (yfinance, same pattern as strategy_confluence.py) ----------------------------- #

_price_cache = {}


def fetch_ohlcv(symbol):
    if symbol in _price_cache:
        return _price_cache[symbol]
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    try:
        df = yf.Ticker(symbol + NSE_SUFFIX).history(start=start, end=end)
        time.sleep(0.15)  # be polite to Yahoo's rate limits across ~200+ symbols
    except Exception:
        df = pd.DataFrame()
    if not df.empty:
        df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                                 "Close": "close", "Volume": "volume"})
        df.index = pd.to_datetime(df.index)
    _price_cache[symbol] = df
    return df


def fetch_nifty():
    return fetch_ohlcv_index("^NSEI")


def fetch_ohlcv_index(ticker):
    end = datetime.today().strftime("%Y-%m-%d")
    start = (datetime.today() - timedelta(days=HISTORY_CALENDAR_DAYS)).strftime("%Y-%m-%d")
    df = yf.Ticker(ticker).history(start=start, end=end)
    df = df.rename(columns={"Open": "open", "High": "high", "Low": "low",
                             "Close": "close", "Volume": "volume"})
    df.index = pd.to_datetime(df.index)
    return df


# ----------------------------- Setup detection ----------------------------- #

def has_range_contraction(df):
    rng_pct = (df["high"] - df["low"]) / df["close"]
    recent = rng_pct.iloc[-cfg.FNO_CONTRACTION_DAYS:].mean()
    base = rng_pct.iloc[-(cfg.FNO_CONTRACTION_DAYS + cfg.FNO_BASE_DAYS):-cfg.FNO_CONTRACTION_DAYS].mean()
    if pd.isna(recent) or pd.isna(base) or base == 0:
        return False
    return recent < 0.7 * base


def has_volume_surge(df):
    vol_avg = df["volume"].iloc[-(cfg.FNO_VOL_AVG_DAYS + 1):-1].mean()
    latest_vol = df["volume"].iloc[-1]
    if pd.isna(vol_avg) or vol_avg == 0:
        return False
    return latest_vol >= cfg.FNO_VOL_MULTIPLIER * vol_avg


def breakout_direction(df):
    lookback = df.iloc[-(cfg.FNO_BREAKOUT_LOOKBACK_DAYS + 1):-1]
    if lookback.empty:
        return "NONE"
    recent_high, recent_low = lookback["high"].max(), lookback["low"].min()
    close = df["close"].iloc[-1]
    if close >= recent_high:
        return "CE"
    if close <= recent_low:
        return "PE"
    return "NONE"


def relative_strength(stock_df, index_df):
    s, ix = stock_df["close"], index_df["close"]
    if len(s) <= cfg.FNO_RS_LOOKBACK_DAYS or len(ix) <= cfg.FNO_RS_LOOKBACK_DAYS:
        return 0.0
    stock_chg = (s.iloc[-1] / s.iloc[-1 - cfg.FNO_RS_LOOKBACK_DAYS]) - 1
    index_chg = (ix.iloc[-1] / ix.iloc[-1 - cfg.FNO_RS_LOOKBACK_DAYS]) - 1
    return stock_chg - index_chg


def screen_setup(df, index_df, is_benchmark=False):
    """
    Applies the range-contraction + volume-surge + breakout (+ relative
    strength) setup to one instrument.

    is_benchmark=True is for screening NIFTY itself:
      - the relative-strength-vs-NIFTY filter is skipped (comparing NIFTY
        to itself is meaningless -- it would always be 0 and fail both
        directions).
      - the volume-surge check is skipped gracefully if the index's volume
        data is unusable (Yahoo Finance often reports 0/NaN volume for
        index tickers like ^NSEI, since an index isn't itself traded the
        way a stock is). Contraction + breakout still both apply.
    """
    min_len = cfg.FNO_BASE_DAYS + cfg.FNO_CONTRACTION_DAYS + 5
    if len(df) < min_len:
        return None

    avg_vol = df["volume"].iloc[-(cfg.FNO_VOL_AVG_DAYS + 1):-1].mean()
    volume_usable = not (pd.isna(avg_vol) or avg_vol == 0)

    if not is_benchmark:
        if not volume_usable or avg_vol < cfg.FNO_MIN_AVG_VOLUME:
            return None
        if not has_volume_surge(df):
            return None
    elif volume_usable:
        # NIFTY volume data happened to be usable this run -- still require
        # the surge if so, no reason to skip a check that actually works.
        if not has_volume_surge(df):
            return None

    if not has_range_contraction(df):
        return None

    setup = breakout_direction(df)
    if setup == "NONE":
        return None

    if is_benchmark:
        rs = None
    else:
        rs = relative_strength(df, index_df)
        if setup == "CE" and rs <= 0:
            return None
        if setup == "PE" and rs >= 0:
            return None

    lookback = df.iloc[-(cfg.FNO_BREAKOUT_LOOKBACK_DAYS + 1):-1]
    stop_ref = lookback["low"].min() if setup == "CE" else lookback["high"].max()
    vol_vs_avg = round(float(df["volume"].iloc[-1] / avg_vol), 2) if volume_usable else None

    if is_benchmark:
        note = (f"NIFTY {setup} bias: range contraction + "
                f"{cfg.FNO_BREAKOUT_LOOKBACK_DAYS}-day breakout"
                + (f", {vol_vs_avg}x volume" if vol_vs_avg is not None else " (index volume data unavailable/unreliable, not checked)")
                + " -- index-wide bias, not a single stock's move.")
    else:
        note = (f"{setup} setup: range contraction + {vol_vs_avg}x volume "
                f"+ {cfg.FNO_BREAKOUT_LOOKBACK_DAYS}-day breakout, RS {round(rs * 100, 1)}% vs NIFTY")

    return {
        "setup": setup,
        "close": round(float(df["close"].iloc[-1]), 2),
        "relative_strength_pct": round(rs * 100, 2) if rs is not None else None,
        "volume_vs_avg": vol_vs_avg,
        "stop_ref": round(float(stop_ref), 2),
        "is_benchmark": is_benchmark,
        "note": note,
    }


# ----------------------------- Historical hit-rate check ----------------------------- #

def backtest_hit_rate(df, index_df, is_benchmark=False):
    """Walk the instrument's own history day by day; every day the setup
    would have fired, check whether price reached the FNO_MOVE_THRESHOLD
    move within the next FNO_MAX_HOLD_DAYS trading days (best favorable
    close in that window, since you could book profit before the window
    ends)."""
    hits = {"CE": 0, "PE": 0}
    total = {"CE": 0, "PE": 0}
    min_window = cfg.FNO_BASE_DAYS + cfg.FNO_CONTRACTION_DAYS + 5

    for i in range(min_window, len(df) - cfg.FNO_MAX_HOLD_DAYS):
        window = df.iloc[: i + 1]
        idx_window = index_df.iloc[: i + 1] if len(index_df) > i else index_df
        result = screen_setup(window, idx_window, is_benchmark=is_benchmark)
        if not result:
            continue
        setup = result["setup"]
        entry_close = df["close"].iloc[i]
        forward = df.iloc[i + 1: i + 1 + cfg.FNO_MAX_HOLD_DAYS]
        if forward.empty:
            continue
        total[setup] += 1
        if setup == "CE":
            best_move = (forward["high"].max() / entry_close) - 1
            if best_move >= cfg.FNO_MOVE_THRESHOLD:
                hits["CE"] += 1
        else:
            best_move = 1 - (forward["low"].min() / entry_close)
            if best_move >= cfg.FNO_MOVE_THRESHOLD:
                hits["PE"] += 1

    def pct(h, t):
        return round(100 * h / t, 1) if t else None

    return {
        "CE_signals": total["CE"], "CE_hit_rate_pct": pct(hits["CE"], total["CE"]),
        "PE_signals": total["PE"], "PE_hit_rate_pct": pct(hits["PE"], total["PE"]),
    }


# ----------------------------- Main ----------------------------- #

def main():
    print("Fetching F&O universe...")
    symbols = get_fno_symbols()
    print(f"{len(symbols)} F&O stocks found.")

    index_df = fetch_nifty()

    candidates = []
    backtests = {}

    # Screen NIFTY itself first -- it's the macro driver behind most of
    # these stock moves anyway, so its own CE/PE bias is worth having
    # alongside the stock candidates, not just implied by them.
    nifty_result = screen_setup(index_df, index_df, is_benchmark=True)
    if nifty_result:
        nifty_result["symbol"] = "NIFTY"
        candidates.append(nifty_result)
        backtests["NIFTY"] = backtest_hit_rate(index_df, index_df, is_benchmark=True)
        print(f"NIFTY: {nifty_result['setup']} bias -- {nifty_result['note']}")
    else:
        print("NIFTY: no setup today.")

    for i, symbol in enumerate(symbols):
        df = fetch_ohlcv(symbol)
        if df.empty:
            continue
        result = screen_setup(df, index_df, is_benchmark=False)
        if result:
            result["symbol"] = symbol
            candidates.append(result)
            backtests[symbol] = backtest_hit_rate(df, index_df, is_benchmark=False)
        if (i + 1) % 25 == 0:
            print(f"...{i + 1}/{len(symbols)} scanned, {len(candidates)} candidates so far")

    # NIFTY (is_benchmark, RS=None) sorts first since it's the macro read;
    # stocks after it, ranked by |relative strength| same as before.
    candidates.sort(key=lambda c: (
        0 if c.get("is_benchmark") else 1,
        -abs(c["relative_strength_pct"]) if c["relative_strength_pct"] is not None else 0,
    ))

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "config": {
            "move_threshold": cfg.FNO_MOVE_THRESHOLD,
            "max_hold_days": cfg.FNO_MAX_HOLD_DAYS,
            "contraction_days": cfg.FNO_CONTRACTION_DAYS,
            "base_days": cfg.FNO_BASE_DAYS,
            "vol_multiplier": cfg.FNO_VOL_MULTIPLIER,
            "breakout_lookback_days": cfg.FNO_BREAKOUT_LOOKBACK_DAYS,
            "rs_lookback_days": cfg.FNO_RS_LOOKBACK_DAYS,
            "min_avg_volume": cfg.FNO_MIN_AVG_VOLUME,
        },
        "candidates": candidates,
        "backtest": backtests,
    }

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n{len(candidates)} candidates written to {OUTPUT_PATH}")
    for c in candidates:
        bt = backtests.get(c["symbol"], {})
        hr = bt.get(f"{c['setup']}_hit_rate_pct")
        rs_str = f"{c['relative_strength_pct']}%" if c["relative_strength_pct"] is not None else "n/a (index)"
        vol_str = f"{c['volume_vs_avg']}x" if c["volume_vs_avg"] is not None else "n/a"
        print(f"  {c['symbol']:15s} {c['setup']}  close={c['close']:<10} "
              f"vol={vol_str}  RS={rs_str}  hist_hit_rate={hr}%")


if __name__ == "__main__":
    main()
