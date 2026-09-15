const { google } = require("googleapis");

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");

  try {
    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    let rows = [];
    try {
      const response = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Uptrend_Signals!A1:L1000",
      });
      rows = response.data.values || [];
    } catch (e) {
      res.status(200).json({
        setups: [],
        allSymbols: [],
        summary: { pending: 0, entered: 0, scanned: 0, lastUpdated: null, notRunYet: true },
      });
      return;
    }

    if (rows.length === 0) {
      res.status(200).json({
        setups: [],
        allSymbols: [],
        summary: { pending: 0, entered: 0, scanned: 0, lastUpdated: null, notRunYet: true },
      });
      return;
    }

    const headers = rows[0];
    const idx = (name) => headers.indexOf(name);

    const records = rows.slice(1).filter((r) => r[idx("symbol")]).map((row) => ({
      symbol: row[idx("symbol")] || "",
      status: row[idx("uptrend_status")] || "",
      pattern: row[idx("uptrend_pattern")] || "",
      entryPrice: row[idx("uptrend_entry_price")] || "",
      entryNote: row[idx("uptrend_entry_note")] || "",
      stopLoss: row[idx("uptrend_stop_loss")] || "",
      lpsWeek: row[idx("uptrend_lps_week")] || "",
      sosWeek: row[idx("uptrend_sos_week")] || "",
      sosLegOrigin: row[idx("uptrend_sos_leg_origin")] || "",
      regimeStructure: row[idx("uptrend_regime_structure")] || "",
      volumeTrend: row[idx("uptrend_volume_trend")] || "",
      lastUpdated: row[idx("last_updated")] || "",
    }));

    const setups = records.filter((r) => r.status === "PENDING_ENTRY" || r.status === "ENTERED");
    const pending = setups.filter((r) => r.status === "PENDING_ENTRY").length;
    const entered = setups.filter((r) => r.status === "ENTERED").length;
    const lastUpdated = records.length ? records[0].lastUpdated : null;

    res.status(200).json({
      setups,
      allSymbols: records,
      summary: { pending, entered, scanned: records.length, lastUpdated, notRunYet: false },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
