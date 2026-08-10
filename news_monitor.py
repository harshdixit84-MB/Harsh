"""
Scheduled job: fetches recent news for every actively-tracked stock (read
from Sheet1, same as sync_dashboard.py), classifies each new headline as
Good / Bad / Neutral news for a swing trader using a free keyword-rule
engine (no paid API), and appends it to the "News" tab of the same Google
Sheet -- sorted by date descending.

Articles are deduped by a hash of their link, so re-running this job never
creates duplicate rows. Dismissing an article from the dashboard UI (via
api/dismiss-news.js) sets a "dismissed" flag on that row -- this job never
un-dismisses or removes rows, it only appends genuinely new ones, so a
dismissed article will not reappear even though Google News keeps
returning it for a couple of days.

NOTE on accuracy: keyword rules are a blunt instrument. They catch clear,
literal cases ("profit rises", "downgrade", "fraud probe") but will
mis-classify or shrug (Neutral) on sarcasm, relative-to-estimate framing
("beats muted expectations"), and anything phrased unusually. Treat the
flag as a rough first pass, not a verdict -- always read the headline.

Environment variables required:
  GOOGLE_SERVICE_ACCOUNT_KEY (the full JSON key content, as a string)
"""

import hashlib
import json
import os
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
NEWS_SHEET = "News"
NEWS_HEADERS = ["id", "date", "symbol", "headline", "link", "source", "sentiment", "reason", "dismissed"]
LOOKBACK_WINDOW = "2d"       # Google News RSS "when:" filter
MAX_ARTICLES_PER_SYMBOL = 10  # raw candidates fetched per stock -- many get filtered out by is_relevant()

# A headline must contain at least one of these to be kept at all -- this is
# what filters out generic "Buy/Sell/Hold" listicles, "Top 5 stocks today"
# roundups, and other noise that isn't actually about this company's own
# financial performance or corporate activity. Extend this list as you spot
# genuinely relevant headlines getting filtered out, or noise slipping through.
RELEVANCE_KEYWORDS = [
    # Financial performance
    "profit", "loss", "revenue", "sales", "turnover", "earnings", "ebitda", "margin", "eps",
    "quarter", "q1", "q2", "q3", "q4", "quarterly results", "results", "guidance", "outlook",
    # Institutional / ownership activity
    "fii", "dii", "institutional", "mutual fund", "promoter", "pledge", "pledged shares",
    "stake", "insider", "bulk deal", "block deal",
    # Corporate actions
    "acquisition", "acquires", "merger", "amalgamation", "stake sale", "stake buy",
    "dividend", "buyback", "bonus issue", "stock split", "rights issue",
    "ipo", "listing", "delisting",
    # Growth / capacity / operations
    "capex", "capital expenditure", "expansion", "capacity expansion", "new plant", "new unit",
    "order", "contract", "tender", "wins order", "bags order", "secures order",
    "jv", "joint venture", "capacity",
    # Ratings / market reaction
    "rating", "upgrade", "downgrade", "target price", "record high", "record low",
    "52-week", "all-time high", "surge", "rally", "plunge", "crash", "jump", "soar", "tumble",
    # Regulatory / legal / governance
    "raid", "ed raid", "probe", "sebi", "penalty", "fine", "litigation", "notice", "tribunal",
    "npa", "debt", "credit rating", "default", "resign", "resignation", "steps down",
    # Workforce / trade
    "layoff", "hiring", "gst", "tax notice", "income tax", "export", "import", "tariff", "duty", "subsidy",
]


def is_relevant(headline):
    """Keeps only headlines that actually touch on financial performance,
    corporate activity, or market-moving events -- filters out generic
    "Buy/Sell/Hold" comparisons, roundup listicles, and similar noise."""
    text = headline.lower()
    return any(kw in text for kw in RELEVANCE_KEYWORDS)


# Keyword lists -- lowercase, checked as substrings against the lowercased headline.
# Extend these over time as you notice misses in your own News tab.
GOOD_KEYWORDS = [
    "profit rises", "profit jumps", "profit surges", "profit up", "net profit rises",
    "beats estimates", "beat estimates", "record high", "record profit", "record revenue",
    "order win", "wins order", "bags order", "secures order", "large order",
    "upgrade", "upgraded to buy", "raises target", "target price raised",
    "buyback", "bonus issue", "stock split", "dividend announced",
    "acquisition", "acquires", "stake buy", "expansion plan", "capacity expansion",
    "strong guidance", "raises guidance", "outlook raised", "all-time high", "52-week high",
    "stock surges", "stock rallies", "shares jump", "shares soar", "block deal buy",
]

BAD_KEYWORDS = [
    "profit falls", "profit drops", "profit declines", "net loss", "widens loss",
    "misses estimates", "miss estimates", "below estimates",
    "downgrade", "downgraded to sell", "cuts target", "target price cut",
    "fraud", "probe", "raid", "show-cause notice", "penalty", "fined", "sebi action",
    "resigns", "resignation", "steps down", "promoter pledge", "pledged shares", "stake sale",
    "default", "debt concern", "credit rating cut", "rating downgrade",
    "weak guidance", "cuts guidance", "outlook cut", "52-week low", "stock crashes",
    "shares plunge", "shares tumble", "block deal sell", "order cancelled", "contract terminated",
]


def classify_news(symbol, headline):
    """Free keyword-rule classifier. Counts good/bad keyword hits in the
    headline and picks whichever side has more matches; ties or no
    matches fall back to Neutral."""
    text = headline.lower()

    good_hits = [kw for kw in GOOD_KEYWORDS if kw in text]
    bad_hits = [kw for kw in BAD_KEYWORDS if kw in text]

    if len(good_hits) > len(bad_hits):
        sentiment = "Good"
        reason = f"Matched: {good_hits[0]}"
    elif len(bad_hits) > len(good_hits):
        sentiment = "Bad"
        reason = f"Matched: {bad_hits[0]}"
    else:
        sentiment = "Neutral"
        reason = "No clear keyword match -- read the headline"

    return sentiment, reason


def get_sheet_client():
    key_json = os.environ["GOOGLE_SERVICE_ACCOUNT_KEY"]
    key_dict = json.loads(key_json)
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_info(key_dict, scopes=scopes)
    return gspread.authorize(creds)


def get_tracked_stocks(spreadsheet):
    """Same source list your price/signal sync already tracks -- active,
    non-archived symbols on Sheet1."""
    records = spreadsheet.sheet1.get_all_records()
    stocks = []
    for r in records:
        if r.get("status") == "archived":
            continue
        symbol = r.get("symbol")
        if symbol:
            stocks.append({"symbol": symbol, "name": r.get("name") or symbol})
    return stocks


def get_or_create_news_sheet(spreadsheet):
    try:
        ws = spreadsheet.worksheet(NEWS_SHEET)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=NEWS_SHEET, rows=2000, cols=len(NEWS_HEADERS))
        ws.update([NEWS_HEADERS], "A1")
    return ws


def fetch_news_for_stock(name):
    query = name.replace(" ", "+")
    url = f"https://news.google.com/rss/search?q={query}+when:{LOOKBACK_WINDOW}&hl=en-IN&gl=IN&ceid=IN:en"
    feed = feedparser.parse(url)

    articles = []
    for entry in feed.entries[:MAX_ARTICLES_PER_SYMBOL]:
        try:
            published = parsedate_to_datetime(entry.published)
        except Exception:
            published = datetime.now(timezone.utc)

        source = ""
        if getattr(entry, "source", None):
            source = entry.source.get("title", "")

        articles.append({
            "headline": entry.title,
            "link": entry.link,
            "date": published.strftime("%Y-%m-%d %H:%M"),
            "source": source,
        })
    return articles


def make_id(link):
    return hashlib.sha256(link.encode()).hexdigest()[:16]


def main():
    gc = get_sheet_client()
    spreadsheet = gc.open(SHEET_NAME)

    stocks = get_tracked_stocks(spreadsheet)
    news_ws = get_or_create_news_sheet(spreadsheet)

    existing_records = news_ws.get_all_records()
    existing_ids = {r["id"] for r in existing_records if r.get("id")}
    all_rows = list(existing_records)

    new_count = 0
    skipped_count = 0
    for stock in stocks:
        symbol = stock["symbol"]
        name = stock["name"]

        try:
            articles = fetch_news_for_stock(name)
        except Exception as e:
            print(f"Could not fetch news for {symbol}: {e}")
            continue

        for article in articles:
            if not is_relevant(article["headline"]):
                skipped_count += 1
                continue  # generic/listicle noise, not worth tracking

            article_id = make_id(article["link"])
            if article_id in existing_ids:
                continue  # already seen (including previously dismissed) -- skip

            sentiment, reason = classify_news(symbol, article["headline"])

            all_rows.append({
                "id": article_id,
                "date": article["date"],
                "symbol": symbol,
                "headline": article["headline"],
                "link": article["link"],
                "source": article["source"],
                "sentiment": sentiment,
                "reason": reason,
                "dismissed": False,
            })
            existing_ids.add(article_id)
            new_count += 1

    all_rows.sort(key=lambda r: r.get("date", ""), reverse=True)

    rows_for_sheet = [[str(r.get(h, "")) for h in NEWS_HEADERS] for r in all_rows]
    news_ws.update([NEWS_HEADERS] + rows_for_sheet, "A1")

    print(f"News sync complete. {new_count} new article(s) added, {skipped_count} filtered out as irrelevant, {len(all_rows)} total rows.")


if __name__ == "__main__":
    main()
