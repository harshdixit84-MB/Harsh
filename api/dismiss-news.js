const { google } = require("googleapis");

module.exports = async (req, res) => {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed" });
    return;
  }

  try {
    const { id } = req.body || {};
    if (!id) {
      res.status(400).json({ error: "Missing id" });
      return;
    }

    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "News!A1:I2000",
    });

    const rows = response.data.values || [];
    if (rows.length === 0) {
      res.status(404).json({ error: "News tab is empty" });
      return;
    }

    const headers = rows[0];
    const idCol = headers.indexOf("id");
    const dismissedCol = headers.indexOf("dismissed");

    const rowIndex = rows.findIndex((row, i) => i > 0 && row[idCol] === id);
    if (rowIndex === -1) {
      res.status(404).json({ error: "News item not found" });
      return;
    }

    const columnLetter = String.fromCharCode(65 + dismissedCol); // 0 -> A, 1 -> B, ...

    await sheets.spreadsheets.values.update({
      spreadsheetId: process.env.SHEET_ID,
      range: `News!${columnLetter}${rowIndex + 1}`,
      valueInputOption: "RAW",
      requestBody: { values: [["True"]] },
    });

    res.status(200).json({ success: true });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
