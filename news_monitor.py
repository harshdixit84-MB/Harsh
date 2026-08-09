"""
Scheduled job: fetches recent news for every actively-tracked stock (read
from Sheet1, same as sync_dashboard.py), classifies each new headline as
Good / Bad / Neutral news for a swing trader using Claude, and appends it
to the "News" tab of the same Google Sheet -- sorted by date descending.

Articles are deduped by a hash of their link, so re-running this job never
creates duplicate rows. Dismissing an article from the dashboard UI (via
api/dismiss-news.js) sets a "dismissed" flag on that row -- this job never
un-dismisses or removes rows, it only appends genuinely new ones, so a
dismissed article will not reappear even though Google News keeps
returning it for a couple of days.

Environment variables required:
  GOOGLE_SERVICE_ACCOUNT_KEY (the full JSON key content, as a string)
  ANTHROPIC_API_KEY
"""

import hashlib
import json
import os
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import anthropic
import feedparser
import gspread
from google.oauth2.service_account import Credentials

SHEET_NAME = "Monthly Breakout Scan"
NEWS_SHEET = "News"
NEWS_HEADERS = ["id", "date", "symbol", "headline", "link", "source", "sentiment", "reason", "dismissed"]
LOOKBACK_WINDOW = "2d"       # Google News RSS "when:" filter
MAX_ARTICLES_PER_SYMBOL = 5  # cap per stock per run, keeps classification cost predictable
CLASSIFY_MODEL = "claude-haiku-4-5-20251001"

client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


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


def classify_news(symbol, headline):
    prompt = (
        f"You are classifying an Indian stock-market headline for the stock {symbol}.\n"
        f'Headline: "{headline}"\n\n'
        "Respond with ONLY a JSON object, no other text, in this exact format:\n"
        '{"sentiment": "Good", "reason": "<one short sentence, under 15 words>"}\n\n'
        'sentiment must be exactly one of: "Good", "Bad", "Neutral".\n'
        "Classify from the perspective of a swing trader deciding whether this news is likely to push "
        "the stock price up (Good), down (Bad), or have negligible/unclear near-term price impact (Neutral)."
    )
    try:
        response = client.messages.create(
            model=CLASSIFY_MODEL,
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        text = text.replace("```json", "").replace("```", "").strip()
        parsed = json.loads(text)

        sentiment = parsed.get("sentiment", "Neutral")
        if sentiment not in ("Good", "Bad", "Neutral"):
            sentiment = "Neutral"
        reason = parsed.get("reason", "")
        return sentiment, reason
    except Exception as e:
        print(f"Classification failed for '{headline}': {e}")
        return "Neutral", "Could not classify automatically"


def main():
    gc = get_sheet_client()
    spreadsheet = gc.open(SHEET_NAME)

    stocks = get_tracked_stocks(spreadsheet)
    news_ws = get_or_create_news_sheet(spreadsheet)

    existing_records = news_ws.get_all_records()
    existing_ids = {r["id"] for r in existing_records if r.get("id")}
    all_rows = list(existing_records)

    new_count = 0
    for stock in stocks:
        symbol = stock["symbol"]
        name = stock["name"]

        try:
            articles = fetch_news_for_stock(name)
        except Exception as e:
            print(f"Could not fetch news for {symbol}: {e}")
            continue

        for article in articles:
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
            time.sleep(0.5)  # gentle pacing against the classification API

    all_rows.sort(key=lambda r: r.get("date", ""), reverse=True)

    rows_for_sheet = [[str(r.get(h, "")) for h in NEWS_HEADERS] for r in all_rows]
    news_ws.update([NEWS_HEADERS] + rows_for_sheet, "A1")

    print(f"News sync complete. {new_count} new article(s) added, {len(all_rows)} total rows.")


if __name__ == "__main__":
    main()
