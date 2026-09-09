const { google } = require("googleapis");

function colLetter(index) {
  let letter = "";
  index += 1; // convert to 1-based
  while (index > 0) {
    const rem = (index - 1) % 26;
    letter = String.fromCharCode(65 + rem) + letter;
    index = Math.floor((index - 1) / 26);
  }
  return letter;
}

const ALLOWED_TARGET_FIELDS = ["buy_target", "target_1", "target_2"];

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { symbol, target, field } = req.body;
    const targetField = field || "buy_target"; // default keeps old callers (main table Entry field) working unchanged

    if (!symbol || target === undefined || target === null || target === "") {
      res.status(400).json({ error: "symbol and target are required" });
      return;
    }
    if (!ALLOWED_TARGET_FIELDS.includes(targetField)) {
      res.status(400).json({ error: `field must be one of: ${ALLOWED_TARGET_FIELDS.join(", ")}` });
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
    const targetCol = headers.indexOf(targetField);

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
