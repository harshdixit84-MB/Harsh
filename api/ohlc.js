const { google } = require("googleapis");

// Serves daily OHLC candles for a single symbol, read from the
// "OHLC_Daily" tab written by scripts/fetch_ohlc.py.
//
// GET /api/ohlc?symbol=RELIANCE
//
// Response shape (matches what KLineCharts expects, roughly):
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

  try {
    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "OHLC_Daily!A1:G1000000",
    });

    const rows = response.data.values || [];
    if (rows.length === 0) {
      res.status(200).json({ symbol, candles: [] });
      return;
    }

    const headers = rows[0];
    const symbolIdx = headers.indexOf("symbol");
    const dateIdx = headers.indexOf("date");
    const openIdx = headers.indexOf("open");
    const highIdx = headers.indexOf("high");
    const lowIdx = headers.indexOf("low");
    const closeIdx = headers.indexOf("close");
    const volumeIdx = headers.indexOf("volume");

    const candles = rows
      .slice(1)
      .filter((row) => row[symbolIdx] === symbol)
      .map((row) => ({
        timestamp: new Date(row[dateIdx]).getTime(),
        open: parseFloat(row[openIdx]),
        high: parseFloat(row[highIdx]),
        low: parseFloat(row[lowIdx]),
        close: parseFloat(row[closeIdx]),
        volume: parseInt(row[volumeIdx], 10) || 0,
      }))
      .sort((a, b) => a.timestamp - b.timestamp);

    res.status(200).json({ symbol, candles });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
