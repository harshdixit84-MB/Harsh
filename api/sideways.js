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
        range: "Sideways_Signals!A1:O1000",
      });
      rows = response.data.values || [];
    } catch (e) {
      res.status(200).json({
        setups: [],
        allSymbols: [],
        summary: { pending: 0, entered: 0, longCount: 0, shortCount: 0, scanned: 0, lastUpdated: null, notRunYet: true },
      });
      return;
    }

    if (rows.length === 0) {
      res.status(200).json({
        setups: [],
        allSymbols: [],
        summary: { pending: 0, entered: 0, longCount: 0, shortCount: 0, scanned: 0, lastUpdated: null, notRunYet: true },
      });
      return;
    }

    const headers = rows[0];
    const idx = (name) => headers.indexOf(name);

    const records = rows.slice(1).filter((r) => r[idx("symbol")]).map((row) => ({
      symbol: row[idx("symbol")] || "",
      status: row[idx("sideways_status")] || "",
      direction: row[idx("sideways_direction")] || "",
      pattern: row[idx("sideways_pattern")] || "",
      entryPrice: row[idx("sideways_entry_price")] || "",
      entryNote: row[idx("sideways_entry_note")] || "",
      stopLoss: row[idx("sideways_stop_loss")] || "",
      target: row[idx("sideways_target")] || "",
      patternWeek: row[idx("sideways_pattern_week")] || "",
      confirmWeek: row[idx("sideways_confirm_week")] || "",
      rangeResistance: row[idx("sideways_range_resistance")] || "",
      rangeSupport: row[idx("sideways_range_support")] || "",
      rangeWidthPct: row[idx("sideways_range_width_pct")] || "",
      volumeTrend: row[idx("sideways_volume_trend")] || "",
      lastUpdated: row[idx("last_updated")] || "",
    }));

    const setups = records.filter((r) => r.status === "PENDING_ENTRY" || r.status === "ENTERED");
    const pending = setups.filter((r) => r.status === "PENDING_ENTRY").length;
    const entered = setups.filter((r) => r.status === "ENTERED").length;
    const longCount = setups.filter((r) => r.direction === "LONG").length;
    const shortCount = setups.filter((r) => r.direction === "SHORT").length;
    const lastUpdated = records.length ? records[0].lastUpdated : null;

    res.status(200).json({
      setups,
      allSymbols: records,
      summary: { pending, entered, longCount, shortCount, scanned: records.length, lastUpdated, notRunYet: false },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
