const { google } = require("googleapis");

// Datewise, per-ticker notes -- one row per note (not one cell per stock),
// so there's no practical cap on how much history you can keep. Sheets
// tabs comfortably hold millions of rows; this will never be the bottleneck.
const NOTES_SHEET = "Ticker_Notes";
const NOTES_HEADER = ["id", "symbol", "date", "comment", "created_at"];

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { symbol, comment, date } = req.body || {};

    if (!symbol || typeof symbol !== "string") {
      res.status(400).json({ error: "symbol is required" });
      return;
    }
    if (!comment || typeof comment !== "string" || !comment.trim()) {
      res.status(400).json({ error: "comment is required" });
      return;
    }

    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });
    const spreadsheetId = process.env.SHEET_ID;

    // Create the Ticker_Notes tab with headers on first-ever use.
    const meta = await sheets.spreadsheets.get({ spreadsheetId });
    const existingTitles = (meta.data.sheets || []).map((s) => s.properties.title);

    if (!existingTitles.includes(NOTES_SHEET)) {
      await sheets.spreadsheets.batchUpdate({
        spreadsheetId,
        requestBody: {
          requests: [{ addSheet: { properties: { title: NOTES_SHEET } } }],
        },
      });
      await sheets.spreadsheets.values.update({
        spreadsheetId,
        range: `${NOTES_SHEET}!A1`,
        valueInputOption: "RAW",
        requestBody: { values: [NOTES_HEADER] },
      });
    }

    const now = new Date();
    const noteDate = date && typeof date === "string" ? date : now.toISOString().slice(0, 10);
    const id = String(now.getTime());

    await sheets.spreadsheets.values.append({
      spreadsheetId,
      range: `${NOTES_SHEET}!A1`,
      valueInputOption: "RAW",
      insertDataOption: "INSERT_ROWS",
      requestBody: {
        values: [[id, symbol, noteDate, comment.trim(), now.toISOString()]],
      },
    });

    res.status(200).json({ ok: true, id, symbol, date: noteDate, comment: comment.trim() });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
