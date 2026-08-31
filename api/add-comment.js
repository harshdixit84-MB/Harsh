const { google } = require("googleapis");
const crypto = require("crypto");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { symbol, comment } = req.body;

    if (!symbol || !comment || comment.trim() === "") {
      res.status(400).json({ error: "symbol and comment are required" });
      return;
    }

    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const id = crypto.randomUUID();
    const now = new Date().toISOString();

    await sheets.spreadsheets.values.append({
      spreadsheetId: process.env.SHEET_ID,
      range: "Ticker_Notes!A1:E1",
      valueInputOption: "RAW",
      insertDataOption: "INSERT_ROWS",
      requestBody: { values: [[id, symbol, now, comment.trim(), now]] },
    });

    res.status(200).json({ success: true, id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
