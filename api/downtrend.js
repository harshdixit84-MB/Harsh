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
        range: "Downtrend_Signals!A1:N1000",
      });
      rows = response.data.values || [];
    } catch (e) {
      // Tab doesn't exist yet -- the weekly workflow hasn't run for the
      // first time. Return an empty-but-valid shape rather than a 500,
      // so the page can show "no data yet" instead of an error screen.
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
      status: row[idx("downtrend_status")] || "",
      pattern: row[idx("downtrend_pattern")] || "",
      entryPrice: row[idx("downtrend_entry_price")] || "",
      entryNote: row[idx("downtrend_entry_note")] || "",
      stopLoss: row[idx("downtrend_stop_loss")] || "",
      stopReferenceWeek: row[idx("downtrend_stop_reference_week")] || "",
      sowBreakWeek: row[idx("downtrend_sow_break_week")] || "",
      upthrustWeek: row[idx("downtrend_upthrust_week")] || "",
      upthrustResistance: row[idx("downtrend_upthrust_resistance")] || "",
      regimeStructure: row[idx("downtrend_regime_structure")] || "",
      distributionWeeks: row[idx("downtrend_distribution_weeks")] || "",
      volumeTrend: row[idx("downtrend_volume_trend")] || "",
      lastUpdated: row[idx("last_updated")] || "",
    }));

    const setups = records.filter((r) => r.status === "PENDING_ENTRY" || r.status === "ENTERED");
    const pending = setups.filter((r) => r.status === "PENDING_ENTRY").length;
    const entered = setups.filter((r) => r.status === "ENTERED").length;
    const lastUpdated = records.length ? records[0].lastUpdated : null;

    res.status(200).json({
      setups,
      allSymbols: records,
      summary: {
        pending,
        entered,
        scanned: records.length,
        lastUpdated,
        notRunYet: false,
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
