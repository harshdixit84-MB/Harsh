const { google } = require("googleapis");

async function getLivePrice(symbol) {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}.NS`;
    const resp = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
    const data = await resp.json();
    const price = data?.chart?.result?.[0]?.meta?.regularMarketPrice;
    return price ?? null;
  } catch (e) {
    return null;
  }
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");

  try {
    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "Trades!A1:F1000",
    });

    const rows = response.data.values || [];
    if (rows.length === 0) {
      res.status(200).json({
        open: [],
        closed: [],
        summary: { realized: 0, unrealized: 0, openCount: 0, closedCount: 0 },
      });
      return;
    }

    const headers = rows[0];
    const symbolCol = headers.indexOf("symbol");

    const records = rows
      .slice(1)
      .filter((r) => r[symbolCol])
      .map((row) => {
        const obj = {};
        headers.forEach((h, i) => {
          obj[h] = row[i] !== undefined ? row[i] : "";
        });
        return obj;
      });

    let dvSummaryBysymbol = {};
    try {
      const dvResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "DV_Summary!A1:G1000",
      });
      const dvRows = dvResponse.data.values || [];
      if (dvRows.length > 0) {
        const dvHeaders = dvRows[0];
        const dvSymbolIdx = dvHeaders.indexOf("symbol");
        const tagIdx = dvHeaders.indexOf("high_dv_tag");
        const verdictIdx = dvHeaders.indexOf("buying_selling_verdict");
        dvRows.slice(1).forEach((row) => {
          const symbol = row[dvSymbolIdx];
          const tag = row[tagIdx];
          const verdict = verdictIdx !== -1 ? row[verdictIdx] : "";
          if (symbol) {
            dvSummaryBysymbol[symbol] = {
              highDv: tag === "TRUE" || tag === "true" || tag === true,
              buyingSellingVerdict: verdict || "",
            };
          }
        });
      }
    } catch (e) {
      // DV_Summary tab may not exist yet, or symbol isn't in the tracked
      // screener list -- proceed without it, badges just won't show
    }

    function attachDvInfo(t) {
      t.high_dv = dvSummaryBysymbol[t.symbol]?.highDv || false;
      t.buying_selling_verdict = dvSummaryBysymbol[t.symbol]?.buyingSellingVerdict || "";
      return t;
    }

    const openTrades = records.filter((r) => !r.sell_price).map(attachDvInfo);
    const closedTrades = records.filter((r) => r.sell_price).map(attachDvInfo);

    const livePrices = await Promise.all(openTrades.map((t) => getLivePrice(t.symbol)));

    let totalUnrealized = 0;
    openTrades.forEach((t, i) => {
      const buy = parseFloat(t.buy_price);
      const qty = parseFloat(t.quantity) || 1;
      const current = livePrices[i];
      t.current_price = current;
      t.quantity = qty;
      if (current !== null && !isNaN(current)) {
        t.pl_amount = Math.round((current - buy) * qty * 100) / 100;
        t.pl_percent = Math.round(((current - buy) / buy) * 10000) / 100;
        totalUnrealized += t.pl_amount;
      } else {
        t.pl_amount = null;
        t.pl_percent = null;
      }
    });

    let totalRealized = 0;
    closedTrades.forEach((t) => {
      const buy = parseFloat(t.buy_price);
      const sell = parseFloat(t.sell_price);
      const qty = parseFloat(t.quantity) || 1;
      t.quantity = qty;
      t.pl_amount = Math.round((sell - buy) * qty * 100) / 100;
      t.pl_percent = Math.round(((sell - buy) / buy) * 10000) / 100;
      totalRealized += t.pl_amount;
    });

    res.status(200).json({
      open: openTrades,
      closed: closedTrades.reverse(),
      summary: {
        realized: Math.round(totalRealized * 100) / 100,
        unrealized: Math.round(totalUnrealized * 100) / 100,
        openCount: openTrades.length,
        closedCount: closedTrades.length,
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
