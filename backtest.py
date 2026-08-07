"""
Backtest
=========
Replays the EXACT SAME screening logic used live (screener.py) against
historical data, walking forward day by day, to see how candidates and
BuySignal flags would have actually performed historically - before
risking real capital on live signals.

This deliberately reuses the live screener's functions (check_stage2_trend,
find_vcp_base, check_breakout) rather than reimplementing the logic, so
the backtest can't drift from what the live system actually does.

At each simulated "today", only data up to and including that day is
used (df.iloc[:i+1]) - no future data ever leaks into a decision. This
is the single most important property of a valid backtest; without it,
the results are meaningless.

USAGE
-----
Sanity check on a couple of stocks first:
    python3 backtest.py --tickers RELIANCE.NS,TCS.NS --years 3

Once that runs cleanly, try a bigger sample (start small - this is
network + compute heavy, one yfinance fetch per ticker plus a walk-
forward loop over every trading day):
    python3 backtest.py --tickers-file data/nifty500.csv --years 3 --limit 50

Results are saved to backtest_results_<date>.csv and a summary is
printed to the console.
"""

import argparse
from datetime import datetime
import numpy as np
import pandas as pd

import screener

# How many trading days after a signal to measure forward returns at.
DEFAULT_HORIZONS = (5, 10, 20, 40)

# Minimum trading days between signals on the SAME ticker, to avoid
# counting the same base/breakout as many overlapping "signals" in a row.
COOLDOWN_DAYS = 15


def fetch_full_history(ticker: str, years: int = 3) -> pd.DataFrame:
    df = screener.fetch_data(ticker, period=f"{years}y")
    if df.empty:
        return df
    return screener.compute_indicators(df)


def walk_forward_signals(df: pd.DataFrame, min_days: int = 220, step: int = 1):
    """
    Walks through df day by day, running the SAME live screening logic
    on only the data available up to that day (df.iloc[:i+1]), and
    collects every point where a candidate/signal would have fired.
    """
    n = len(df)
    signals = []
    last_signal_idx = -9999

    for i in range(min_days, n, step):
        if i - last_signal_idx < COOLDOWN_DAYS:
            continue

        window = df.iloc[:i + 1]  # "as of" this day only - no future data

        stage2_ok, trend_info = screener.check_stage2_trend(window)
        if not stage2_ok:
            continue

        base, reason = screener.find_vcp_base(window)
        if not base:
            continue

        is_near, pct_from_pivot = screener.check_breakout(window, base)
        if not is_near:
            continue

        close = trend_info["close"]
        extension = (close - trend_info["sma50"]) / trend_info["sma50"] * 100
        extended = extension > screener.MAX_EXTENSION_ABOVE_SMA50_PCT
        has_broken_out = pct_from_pivot <= 0
        buy_signal = (
            has_broken_out and base["breakout_vol_expansion"]
            and base["rs_near_high"] and not extended
        )

        signals.append({
            "date": df.index[i], "idx": i, "close": close,
            "pivot": base["pivot_price"], "buy_signal": buy_signal,
            "has_broken_out": has_broken_out,
            "final_pullback_pct": base["final_pullback_pct"],
        })
        last_signal_idx = i

    return signals


def evaluate_signal(df: pd.DataFrame, signal: dict, horizons=DEFAULT_HORIZONS, stop_pct: float = 8.0):
    """
    For one signal, computes the forward return at each horizon, applying
    a stop-loss simulation: if price's daily LOW closes below the stop
    at any point before the horizon, the trade is marked as stopped out
    at the stop price for every horizon from that point on (a realistic
    approximation - you would have exited, not held through further moves).
    """
    entry_idx = signal["idx"]
    entry_price = signal["close"]
    n = len(df)
    result = {"date": signal["date"], "buy_signal": signal["buy_signal"]}

    stop_price = entry_price * (1 - stop_pct / 100) if stop_pct else None
    stopped_out = False

    for h in horizons:
        target_idx = entry_idx + h
        if target_idx >= n:
            result[f"ret_{h}d"] = None
            continue

        if stop_price and not stopped_out:
            path = df["Low"].iloc[entry_idx + 1: target_idx + 1]
            if (path <= stop_price).any():
                stopped_out = True

        if stopped_out:
            result[f"ret_{h}d"] = round((stop_price - entry_price) / entry_price * 100, 2)
        else:
            future_close = df["Close"].iloc[target_idx]
            result[f"ret_{h}d"] = round((future_close - entry_price) / entry_price * 100, 2)

    result["stopped_out"] = stopped_out
    return result


def run_backtest(tickers, years=3, horizons=DEFAULT_HORIZONS, stop_pct=8.0, only_buy_signal=False):
    all_results = []
    for ticker in tickers:
        print(f"[backtest] {ticker} ...")
        try:
            df = fetch_full_history(ticker, years=years)
        except Exception as e:
            print(f"  skip: fetch failed ({e})")
            continue

        if df.empty or len(df) < 250:
            print(f"  skip: insufficient data")
            continue

        signals = walk_forward_signals(df)
        if only_buy_signal:
            signals = [s for s in signals if s["buy_signal"]]

        for sig in signals:
            res = evaluate_signal(df, sig, horizons=horizons, stop_pct=stop_pct)
            res["ticker"] = ticker
            all_results.append(res)

        print(f"  {len(signals)} signal(s) found")

    return pd.DataFrame(all_results)


def summarize(results_df, horizons=DEFAULT_HORIZONS):
    if results_df.empty:
        print("\nNo signals found in this backtest window.")
        return

    print(f"\n=== Backtest Summary ({len(results_df)} total signals across all tickers) ===")
    buy_only = results_df[results_df["buy_signal"] == True]
    print(f"Of which BuySignal=True: {len(buy_only)}")
    print(f"Stopped out before horizon (any horizon): {int(results_df['stopped_out'].sum())} "
          f"({results_df['stopped_out'].mean()*100:.1f}%)")

    for h in horizons:
        col = f"ret_{h}d"
        valid = results_df[col].dropna()
        if len(valid) == 0:
            continue
        win_rate = (valid > 0).mean() * 100
        print(f"\n-- {h}-day horizon (n={len(valid)}) --")
        print(f"  Win rate: {win_rate:.1f}%")
        print(f"  Avg return: {valid.mean():.2f}%   Median: {valid.median():.2f}%")
        print(f"  Best: {valid.max():.2f}%   Worst: {valid.min():.2f}%")

        if len(buy_only) > 0:
            valid_buy = buy_only[col].dropna()
            if len(valid_buy) > 0:
                print(f"  [BuySignal only] Win rate: {(valid_buy > 0).mean()*100:.1f}%, "
                      f"Avg return: {valid_buy.mean():.2f}% (n={len(valid_buy)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest the VCP screener against historical data.")
    parser.add_argument("--tickers", type=str, default=None,
                         help="Comma-separated tickers, e.g. RELIANCE.NS,TCS.NS")
    parser.add_argument("--tickers-file", type=str, default=None,
                         help="Path to a CSV with a 'Symbol' column (e.g. data/nifty500.csv)")
    parser.add_argument("--limit", type=int, default=None,
                         help="Only test the first N tickers from --tickers-file (for a quick run)")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--stop-pct", type=float, default=8.0, help="Stop-loss %% below entry price")
    parser.add_argument("--only-buy-signal", action="store_true",
                         help="Only evaluate signals where BuySignal was True")
    args = parser.parse_args()

    if args.tickers:
        tickers = [t.strip() for t in args.tickers.split(",")]
    elif args.tickers_file:
        df_universe = pd.read_csv(args.tickers_file)
        tickers = [f"{s.strip()}.NS" for s in df_universe["Symbol"].dropna().tolist()]
        if args.limit:
            tickers = tickers[:args.limit]
    else:
        raise SystemExit("Provide --tickers or --tickers-file")

    print(f"Backtesting {len(tickers)} ticker(s) over {args.years} year(s), "
          f"stop-loss {args.stop_pct}%...\n")

    results = run_backtest(tickers, years=args.years, stop_pct=args.stop_pct,
                            only_buy_signal=args.only_buy_signal)

    out_path = f"backtest_results_{datetime.now().strftime('%Y%m%d')}.csv"
    if not results.empty:
        results.to_csv(out_path, index=False)
        print(f"\nSaved raw results to {out_path}")

    summarize(results, horizons=DEFAULT_HORIZONS)
