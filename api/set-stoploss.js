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
    const { symbol, stop_loss } = req.body;
    if (!symbol || stop_loss === undefined || stop_loss === null || stop_loss === "") {
      res.status(400).json({ error: "symbol and stop_loss are required" });
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
      range: "Sheet1!A1:T1000",
    });
    const rows = getResp.data.values || [];
    if (rows.length === 0) {
      res.status(404).json({ error: "Sheet is empty" });
      return;
    }
    const headers = rows[0];
    const symbolCol = headers.indexOf("symbol");
    const slCol = headers.indexOf("stop_loss");
    if (symbolCol === -1 || slCol === -1) {
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
    const sheetRowNumber = rowIndex + 1;
    const cellRange = `Sheet1!${colLetter(slCol)}${sheetRowNumber}`;
    await sheets.spreadsheets.values.update({
      spreadsheetId: process.env.SHEET_ID,
      range: cellRange,
      valueInputOption: "RAW",
      requestBody: { values: [[stop_loss]] },
    });
    res.status(200).json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
