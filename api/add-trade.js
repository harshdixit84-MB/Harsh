const { google } = require("googleapis");

function colLetter(index) {
  return String.fromCharCode(65 + index);
}

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { action, symbol, price } = req.body;

    if (!action || !symbol || !price) {
      res.status(400).json({ error: "action, symbol, and price are required" });
      return;
    }

    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const today = new Date().toISOString().split("T")[0];
    const cleanSymbol = symbol.toUpperCase().trim();

    if (action === "buy") {
      await sheets.spreadsheets.values.append({
        spreadsheetId: process.env.SHEET_ID,
        range: "Trades!A1",
        valueInputOption: "RAW",
        requestBody: {
          values: [[cleanSymbol, price, "", today, "", ""]],
        },
      });
      res.status(200).json({ success: true });
      return;
    }

    if (action === "sell") {
      const getResp = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Trades!A1:F1000",
      });

      const rows = getResp.data.values || [];
      const headers = rows[0];
      const symbolCol = headers.indexOf("symbol");
      const sellPriceCol = headers.indexOf("sell_price");
      const sellDateCol = headers.indexOf("sell_date");

      let rowIndex = -1;
      for (let i = 1; i < rows.length; i++) {
        if (rows[i][symbolCol] === cleanSymbol && !rows[i][sellPriceCol]) {
          rowIndex = i;
          break;
        }
      }

      if (rowIndex === -1) {
        res.status(404).json({ error: `No open position found for ${cleanSymbol}` });
        return;
      }

      const sheetRowNumber = rowIndex + 1;

      await sheets.spreadsheets.values.update({
        spreadsheetId: process.env.SHEET_ID,
        range: `Trades!${colLetter(sellPriceCol)}${sheetRowNumber}`,
        valueInputOption: "RAW",
        requestBody: { values: [[price]] },
      });

      await sheets.spreadsheets.values.update({
        spreadsheetId: process.env.SHEET_ID,
        range: `Trades!${colLetter(sellDateCol)}${sheetRowNumber}`,
        valueInputOption: "RAW",
        requestBody: { values: [[today]] },
      });

      res.status(200).json({ success: true });
      return;
    }

    res.status(400).json({ error: "Unknown action" });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
