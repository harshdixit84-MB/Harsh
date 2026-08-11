// api/ohlc.js
//
// Fetches OHLC candles LIVE from Yahoo Finance's public chart API, directly
// on each request -- no Google Sheets caching, no daily GitHub Action needed.
//
// This means:
//   - Up to 4 years of daily history, fetched fresh every time
//   - Today's candle updates throughout the trading day (Yahoo's daily
//     endpoint returns the still-forming current day bar during market
//     hours, not just yesterday's close)
//   - Zero contact with tradingview.com -- this only talks to Yahoo Finance
//
// GET /api/ohlc?symbol=RELIANCE
//
// Response shape (same as before, so index.html needs NO changes):
// {
//   symbol: "RELIANCE",
//   candles: [
//     { timestamp: 1723075200000, open: 2900.5, high: 2935, low: 2890, close: 2920.4, volume: 1234567 },
//     ...
//   ]
// }

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");

  const { symbol } = req.query;
  if (!symbol) {
    res.status(400).json({ error: "Missing required query param: symbol" });
    return;
  }

  // NSE symbols need the .NS suffix for Yahoo Finance, and some symbols
  // contain characters (e.g. "M&M", "J&KBANK") that need URL-encoding.
  const yahooSymbol = encodeURIComponent(`${symbol}.NS`);
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${yahooSymbol}?range=4y&interval=1d`;

  try {
    const response = await fetch(url, {
      headers: {
        // Yahoo's endpoint occasionally rejects requests with no User-Agent.
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      },
    });

    if (!response.ok) {
      res.status(502).json({ error: `Yahoo Finance returned HTTP ${response.status} for ${symbol}` });
      return;
    }

    const data = await response.json();
    const result = data?.chart?.result?.[0];

    if (!result || !result.timestamp) {
      res.status(200).json({ symbol, candles: [] });
      return;
    }

    const timestamps = result.timestamp;
    const quote = result.indicators?.quote?.[0] || {};
    const { open = [], high = [], low = [], close = [], volume = [] } = quote;

    const candles = [];
    for (let i = 0; i < timestamps.length; i++) {
      const o = open[i];
      const h = high[i];
      const l = low[i];
      const c = close[i];
      const v = volume[i];

      // Yahoo returns null for non-trading days / gaps -- skip those.
      if (o == null || h == null || l == null || c == null) continue;

      candles.push({
        timestamp: timestamps[i] * 1000, // Yahoo gives seconds, KLineCharts wants ms
        open: Math.round(o * 100) / 100,
        high: Math.round(h * 100) / 100,
        low: Math.round(l * 100) / 100,
        close: Math.round(c * 100) / 100,
        volume: v || 0,
      });
    }

    res.status(200).json({ symbol, candles });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
