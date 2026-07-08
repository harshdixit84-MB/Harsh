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
    const { symbol, target } = req.body;

    if (!symbol || target === undefined || target === null || target === "") {
      res.status(400).json({ error: "symbol and target are required" });
      return;
    }

    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const getResp = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "Sheet1!A1:J1000",
    });

    const rows = getResp.data.values || [];
    if (rows.length === 0) {
      res.status(404).json({ error: "Sheet is empty" });
      return;
    }

    const headers = rows[0];
    const symbolCol = headers.indexOf("symbol");
    const targetCol = headers.indexOf("buy_target");

    if (symbolCol === -1 || targetCol === -1) {
      res.status(500).json({ error: "Expected columns not found in sheet" });
      return;
    }

    let rowIndex = -1;
    for (let i = 1; i < rows.length; i++) {
      if (rows[i][symbolCol] === symbol) {
        rowIndex = i;
        break;
      }
    }

    if (rowIndex === -1) {
      res.status(404).json({ error: `Symbol ${symbol} not found in sheet` });
      return;
    }

    const sheetRowNumber = rowIndex + 1; // rows array is 0-indexed, sheet rows are 1-indexed
    const cellRange = `Sheet1!${colLetter(targetCol)}${sheetRowNumber}`;

    await sheets.spreadsheets.values.update({
      spreadsheetId: process.env.SHEET_ID,
      range: cellRange,
      valueInputOption: "RAW",
      requestBody: { values: [[target]] },
    });

    res.status(200).json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
