#!/usr/bin/env python3
"""
outlook_batch.py -- the once-a-day Buy/Hold/Sell outlook run.

Scheduled by .github/workflows/outlook-daily.yml at 4:00 PM IST, Monday to
Friday (after the 3:30 PM close, so today's daily candle is final).

What it does
------------
1. Reads every non-archived symbol from the "Monthly Breakout Scan" sheet
   (the same list the dashboard shows).
2. Asks the nse-stock-chatbot API for the outlooks in small batches
   (mode=outlook_batch), with fresh=1 so yesterday's server-side cache is never
   reused. One batch request fetches Nifty and each sector once and spaces the
   stock fetches out, which keeps the run under Angel's rate limit. The
   outlook logic itself lives in that repo (core/outlook.py): Nifty trend ->
   sector trend -> stock trend/patterns -> BUY / HOLD / SELL.
   The pace adapts: every rate-limit answer slows it down and pauses it, and
   it speeds up again after a few clean batches.
3. Writes data/outlook.json, which the dashboard (index.html) reads. The
   workflow commits that file back to the repo.

The dashboard no longer calculates outlooks by itself in the background:
this run is the only scheduled calculation. (Clicking a ticker that is not
in the file yet, e.g. one added after 4 PM, still calculates that one
ticker on demand.)

If a ticker fails, its previous result is carried over and flagged, instead
of the badge disappearing. If nothing at all could be calculated, the
existing file is left untouched and the run exits with an error.

Environment
-----------
GOOGLE_SERVICE_ACCOUNT_KEY  service-account JSON (same secret the other jobs use)
OUTLOOK_API_URL             optional override of the chatbot API base URL
"""
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import requests

SHEET_NAME = "Monthly Breakout Scan"
API_URL = os.environ.get("OUTLOOK_API_URL", "https://nse-stock-chatbot.vercel.app/api")
OUT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "outlook.json")

REQUEST_TIMEOUT = 65      # Vercel functions stop at 60 s; give the response a little longer to arrive
CHUNK_SIZE = 8            # tickers per API request
PACE_START = 1.0          # seconds the server waits between stock fetches inside a batch
PACE_MAX = 8.0
PAUSE_SECONDS = 1.0       # gap between batch requests
MAX_ATTEMPTS = 2          # for ordinary errors (timeouts, 5xx, calculation errors)
RETRY_WAIT_SECONDS = 20
NON_RETRYABLE = ("valid NSE equity symbol",)
RATE_LIMIT_MARKERS = ("rate-limit", "rate limit", "too many requests", "exceeding access rate")
RATE_LIMIT_PAUSE_SECONDS = 30   # 30 s, then 60, 90 ... (max 240) while it keeps happening; see Throttle
MAX_RATE_LIMIT_RETRIES = 4      # per ticker
MAX_RATE_LIMITED_CHUNKS_IN_A_ROW = 8  # Angel is blocking us outright: stop instead of waiting for ever
BUDGET_SECONDS = 45 * 60  # stop starting new batches after this, so partial results are still saved
                          # (the GitHub job itself is cut off at 60 minutes and would save nothing)
MAX_CONSECUTIVE_FAILURES = 5  # tickers failing for real reasons in a row: something systemic is wrong

# A re-run resumes instead of starting over: results from the previous run are reused when that run
# finished after the 3:30 PM close (final daily candles) and is at most this old.
REUSE_MAX_AGE = timedelta(hours=6)
REUSE_AFTER_IST = (15, 45)

IST = timezone(timedelta(hours=5, minutes=30))


def get_active_symbols():
    "Every non-archived symbol on the dashboard's sheet (dashboard.js treats status == 'archived' as hidden)."
    import gspread
    from google.oauth2.service_account import Credentials

    key_dict = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    client = gspread.authorize(Credentials.from_service_account_info(key_dict, scopes=scopes))
    records = client.open(SHEET_NAME).sheet1.get_all_records()
    return sorted({
        str(r["symbol"]).strip()
        for r in records
        if r.get("symbol") and str(r.get("status", "")).strip().lower() != "archived"
    })


class Throttle:
    """
    Brake for Angel's rate limit: after a "rate limit" answer everything waits
    (30 s, then 60 s, then 90 s ... if it keeps happening); a clean batch
    resets it.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.until = 0.0
        self.level = 0

    def wait(self):
        while True:
            remaining = self.until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 5))

    def hit(self):
        with self.lock:
            self.level += 1
            pause = min(RATE_LIMIT_PAUSE_SECONDS * self.level, 240)
            self.until = max(self.until, time.time() + pause)
            return pause

    def ok(self):
        with self.lock:
            self.level = 0


def is_rate_limited(error_text):
    lowered = str(error_text).lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def request_chunk(symbols, pace):
    "-> ({SYMBOL: outlook_or_error}, None), or (None, error_text) when the request itself failed."
    try:
        resp = requests.get(
            API_URL,
            params={"mode": "outlook_batch", "symbols": ",".join(sym.upper() for sym in symbols),
                    "pace": f"{pace:.1f}", "fresh": "1"},
            timeout=REQUEST_TIMEOUT,
        )
    except Exception as e:  # timeout or connection error
        return None, f"{type(e).__name__}: {e}"
    try:
        data = resp.json()
    except ValueError:  # e.g. Vercel's HTML "504 gateway timeout" page
        return None, f"HTTP {resp.status_code}, not JSON: {resp.text[:150]!r}"
    if not isinstance(data, dict) or not isinstance(data.get("results"), dict):
        return None, str(data.get("error", "unexpected response")) if isinstance(data, dict) else "unexpected response"
    return {str(k).upper(): v for k, v in data["results"].items()}, None


def load_previous_file():
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_previous():
    return load_previous_file().get("results", {})


def reusable_results(previous_file, symbols, now):
    """
    Results a re-run can keep instead of recalculating: only when the previous
    run finished after the close (so its candles are final) and recently, and
    only tickers it actually calculated (not ones it carried over).
    """
    try:
        generated = datetime.strptime(previous_file["generated_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        return {}
    if now - generated > REUSE_MAX_AGE:
        return {}
    generated_ist = generated.astimezone(IST)
    if (generated_ist.hour, generated_ist.minute) < REUSE_AFTER_IST:
        return {}
    previous = previous_file.get("results", {})
    return {
        sym: previous[sym]
        for sym in symbols
        if sym in previous and not previous[sym].get("carried_over") and (previous[sym].get("action") or {}).get("label")
    }


def merge(previous_results, fresh, symbols):
    "Today's results, with a flagged carry-over of the last good result for any ticker that failed."
    results, carried = {}, []
    note = "Today's 4 PM run could not refresh this ticker; showing the last successful result."
    for symbol in symbols:
        if symbol in fresh:
            results[symbol] = fresh[symbol]
        elif symbol in previous_results:
            old = dict(previous_results[symbol])
            old["carried_over"] = True
            notes = list(old.get("data_notes") or [])
            if note not in notes:
                notes.append(note)
            old["data_notes"] = notes
            results[symbol] = old
            carried.append(symbol)
    return results, carried


def write_atomic(payload):
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    tmp = OUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, OUT_PATH)


def log(message):
    print(message, flush=True)  # flush: GitHub buffers piped output, and a cancelled job would otherwise show nothing


def main():
    started = time.time()
    symbols = get_active_symbols()
    log(f"{len(symbols)} active symbols; batches of {CHUNK_SIZE} via {API_URL} "
        f"(stop starting new batches after {BUDGET_SECONDS // 60} min)")

    now = datetime.now(timezone.utc)
    previous_file = load_previous_file()
    reused = reusable_results(previous_file, symbols, now)
    todo = [sym for sym in symbols if sym not in reused]
    if reused:
        log(f"{len(reused)} ticker(s) reused from the run at {previous_file.get('generated_at_ist')} "
            f"(after the close, less than {REUSE_MAX_AGE.seconds // 3600} hours ago); {len(todo)} left to calculate")

    fresh, failed = {}, {}
    ordinary_attempts, rate_limit_retries = {}, {}
    pending = deque(todo)
    throttle = Throttle()
    pace, chunk_size = PACE_START, CHUNK_SIZE
    consecutive_failures = rate_limited_in_a_row = clean_in_a_row = 0
    abort_reason = None

    while pending:
        if time.time() - started > BUDGET_SECONDS:
            abort_reason = f"the {BUDGET_SECONDS // 60}-minute time budget ran out"
            break
        throttle.wait()
        chunk = [pending.popleft() for _ in range(min(chunk_size, len(pending)))]
        t0 = time.time()
        results, transport_error = request_chunk(chunk, pace)
        took = time.time() - t0
        requeue = []

        if transport_error:
            log(f"batch of {len(chunk)} failed after {took:.0f}s: {transport_error}")
            chunk_size = max(2, chunk_size // 2)  # smaller batches finish inside Vercel's 60 s
            for sym in chunk:
                ordinary_attempts[sym] = ordinary_attempts.get(sym, 0) + 1
                if ordinary_attempts[sym] >= MAX_ATTEMPTS:
                    failed[sym] = transport_error
                    consecutive_failures += 1
                else:
                    requeue.append(sym)
            time.sleep(RETRY_WAIT_SECONDS)
        else:
            rate_limited = False
            ok = []
            problems = []
            for sym in chunk:
                r = results.get(sym.upper())
                if isinstance(r, dict) and "error" not in r and (r.get("action") or {}).get("label"):
                    fresh[sym] = r
                    ok.append(f"{sym}:{r['action']['label']}")
                    consecutive_failures = 0
                    continue
                error = str((r or {}).get("error", "missing from the response"))
                if (r or {}).get("retry") or is_rate_limited(error):
                    rate_limited = True
                    rate_limit_retries[sym] = rate_limit_retries.get(sym, 0) + 1
                    if rate_limit_retries[sym] > MAX_RATE_LIMIT_RETRIES:
                        failed[sym] = error
                    else:
                        requeue.append(sym)
                elif any(marker in error for marker in NON_RETRYABLE):
                    failed[sym] = error
                    problems.append(f"{sym}: {error}")
                else:
                    ordinary_attempts[sym] = ordinary_attempts.get(sym, 0) + 1
                    if ordinary_attempts[sym] >= MAX_ATTEMPTS:
                        failed[sym] = error
                        consecutive_failures += 1
                        problems.append(f"{sym}: {error}")
                    else:
                        requeue.append(sym)
            done = len(fresh) + len(failed)
            log(f"[{done}/{len(todo)}] {len(ok)}/{len(chunk)} ok in {took:.0f}s (pace {pace:.1f}s): {' '.join(ok)}")
            for line in problems:
                log(f"  FAILED {line}")

            if rate_limited:
                clean_in_a_row = 0
                rate_limited_in_a_row += 1
                pace = min(PACE_MAX, pace * 1.6)
                pause = throttle.hit()
                log(f"  Angel rate limit: pausing {pause}s, slowing the pace to {pace:.1f}s per ticker")
                if rate_limited_in_a_row >= MAX_RATE_LIMITED_CHUNKS_IN_A_ROW:
                    abort_reason = f"Angel kept rate-limiting {MAX_RATE_LIMITED_CHUNKS_IN_A_ROW} batches in a row"
            else:
                rate_limited_in_a_row = 0
                throttle.ok()
                chunk_size = min(CHUNK_SIZE, chunk_size + 1)  # recover from a shrink after a timeout
                clean_in_a_row += 1
                if clean_in_a_row >= 3:
                    pace = max(PACE_START, pace * 0.85)

        pending.extendleft(reversed(requeue))
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not abort_reason:
            abort_reason = f"{MAX_CONSECUTIVE_FAILURES} tickers in a row failed"
        if abort_reason:
            log(f"STOPPING EARLY: {abort_reason}")
            break
        time.sleep(PAUSE_SECONDS)

    for sym in pending:
        failed.setdefault(sym, f"skipped: {abort_reason or 'not reached'}")

    skipped = [sym for sym, err in failed.items() if str(err).startswith("skipped:")]
    if todo and not fresh and not reused:
        first_error = next(iter(failed.values()), "unknown")
        print(f"::error::No outlook could be calculated for any symbol ({first_error}); "
              "leaving data/outlook.json untouched.", flush=True)
        sys.exit(1)

    results, carried = merge(previous_file.get("results", {}), {**reused, **fresh}, symbols)
    now = datetime.now(timezone.utc)
    write_atomic({
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_at_ist": now.astimezone(IST).strftime("%Y-%m-%d %H:%M IST"),
        "symbols": len(symbols),
        "refreshed": len(fresh),
        "reused": len(reused),
        "carried_over": carried,
        "failed": failed,
        "results": results,
    })

    counts = {}
    for r in results.values():
        label = (r.get("action") or {}).get("label", "?")
        counts[label] = counts.get(label, 0) + 1
    log(f"Done in {time.time() - started:.0f}s: {len(fresh)} calculated, {len(reused)} reused, {len(carried)} carried over from an "
        f"earlier run, {len(failed) - len(carried)} without any result. Totals: {counts}")
    if abort_reason:
        log(f"::warning::Run stopped early because {abort_reason}. {len(skipped)} ticker(s) were not attempted.")
    elif failed:
        log(f"::warning::{len(failed)} ticker(s) failed today: {', '.join(sorted(failed))}")


if __name__ == "__main__":
    main()
