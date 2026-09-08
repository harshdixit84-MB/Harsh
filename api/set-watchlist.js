const { google } = require("googleapis");

function colLetter(index) {
  return String.fromCharCode(65 + index);
}

const ALLOWED_FIELDS = ["watchlisted", "bought"];

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { symbol, field, value } = req.body;

    if (!symbol || !field || value === undefined) {
      res.status(400).json({ error: "symbol, field, and value are required" });
      return;
    }
    if (!ALLOWED_FIELDS.includes(field)) {
      res.status(400).json({ error: `field must be one of: ${ALLOWED_FIELDS.join(", ")}` });
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
      range: "Sheet1!A1:AF1000",
    });

    const rows = getResp.data.values || [];
    if (rows.length === 0) {
      res.status(404).json({ error: "Sheet is empty" });
      return;
    }

    const headers = rows[0];
    const symbolCol = headers.indexOf("symbol");
    const fieldCol = headers.indexOf(field);

    if (symbolCol === -1 || fieldCol === -1) {
      res.status(500).json({ error: `Expected column "${field}" not found in sheet` });
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
    const cellRange = `Sheet1!${colLetter(fieldCol)}${sheetRowNumber}`;

    await sheets.spreadsheets.values.update({
      spreadsheetId: process.env.SHEET_ID,
      range: cellRange,
      valueInputOption: "RAW",
      requestBody: { values: [[value ? "TRUE" : "FALSE"]] },
    });

    res.status(200).json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
