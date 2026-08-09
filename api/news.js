const { google } = require("googleapis");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");
  try {
    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets.readonly"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    let rows = [];
    try {
      const response = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "News!A1:I2000",
      });
      rows = response.data.values || [];
    } catch (e) {
      // News tab may not exist yet (news_monitor.py hasn't run) -- return empty, not an error
      res.status(200).json({ news: [] });
      return;
    }

    if (rows.length === 0) {
      res.status(200).json({ news: [] });
      return;
    }

    const headers = rows[0];
    const dataRows = rows.slice(1);

    const isDismissed = (v) => v === "True" || v === "TRUE" || v === "true" || v === true;

    const news = dataRows
      .map((row) => {
        const obj = {};
        headers.forEach((h, i) => {
          obj[h] = row[i] !== undefined ? row[i] : "";
        });
        return obj;
      })
      .filter((r) => r.id && !isDismissed(r.dismissed))
      .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0)); // decreasing date order

    res.status(200).json({ news });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
