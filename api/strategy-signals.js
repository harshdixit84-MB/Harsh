const { google } = require("googleapis");

// Consolidates what used to be 3 separate serverless functions
// (api/downtrend.js, api/sideways.js, api/uptrend.js) into ONE, selected
// via ?strategy=downtrend|sideways|uptrend. Reason: Vercel's Hobby plan
// caps serverless functions at 12 -- this repo had 10 before those 3
// were added (13 total, one over the cap), which broke every deployment
// from that point on with no useful error surfaced in the build log.
// One function with a query param avoids the cap regardless of how many
// more strategies get added later.

const STRATEGY_CONFIG = {
  downtrend: {
    sheetRange: "Downtrend_Signals!A1:N1000",
    prefix: "downtrend_",
    hasDirection: false,
    fields: [
      ["status", "status"], ["pattern", "pattern"],
      ["entryPrice", "entry_price"], ["entryNote", "entry_note"],
      ["stopLoss", "stop_loss"], ["stopReferenceWeek", "stop_reference_week"],
      ["sowBreakWeek", "sow_break_week"], ["upthrustWeek", "upthrust_week"],
      ["upthrustResistance", "upthrust_resistance"],
      ["regimeStructure", "regime_structure"],
      ["distributionWeeks", "distribution_weeks"],
      ["volumeTrend", "volume_trend"],
    ],
  },
  sideways: {
    sheetRange: "Sideways_Signals!A1:O1000",
    prefix: "sideways_",
    hasDirection: true,
    fields: [
      ["status", "status"], ["direction", "direction"], ["pattern", "pattern"],
      ["entryPrice", "entry_price"], ["entryNote", "entry_note"],
      ["stopLoss", "stop_loss"], ["target", "target"],
      ["patternWeek", "pattern_week"], ["confirmWeek", "confirm_week"],
      ["rangeResistance", "range_resistance"], ["rangeSupport", "range_support"],
      ["rangeWidthPct", "range_width_pct"], ["volumeTrend", "volume_trend"],
    ],
  },
  uptrend: {
    sheetRange: "Uptrend_Signals!A1:L1000",
    prefix: "uptrend_",
    hasDirection: false,
    fields: [
      ["status", "status"], ["pattern", "pattern"],
      ["entryPrice", "entry_price"], ["entryNote", "entry_note"],
      ["stopLoss", "stop_loss"],
      ["lpsWeek", "lps_week"], ["sosWeek", "sos_week"], ["sosLegOrigin", "sos_leg_origin"],
      ["regimeStructure", "regime_structure"], ["volumeTrend", "volume_trend"],
    ],
  },
};

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");

  const strategy = req.query.strategy;
  const cfg = STRATEGY_CONFIG[strategy];
  if (!cfg) {
    res.status(400).json({ error: `Unknown strategy '${strategy}'. Use one of: ${Object.keys(STRATEGY_CONFIG).join(", ")}` });
    return;
  }

  const emptyResponse = {
    setups: [],
    allSymbols: [],
    summary: { pending: 0, entered: 0, longCount: 0, shortCount: 0, scanned: 0, lastUpdated: null, notRunYet: true },
  };

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
        range: cfg.sheetRange,
      });
      rows = response.data.values || [];
    } catch (e) {
      res.status(200).json(emptyResponse);
      return;
    }

    if (rows.length === 0) {
      res.status(200).json(emptyResponse);
      return;
    }

    const headers = rows[0];
    const idx = (name) => headers.indexOf(name);
    const symbolIdx = idx("symbol");
    const lastUpdatedIdx = idx("last_updated");

    // Short Term Long badge is computed once in EMA_Signals (shared across
    // every dashboard, not per-strategy) -- fetch and merge it in here too,
    // same pattern as api/dashboard.js and api/trades.js use.
    let stLongBysymbol = {};
    try {
      const emaResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "EMA_Signals!A1:AF1000",
      });
      const emaRows = emaResponse.data.values || [];
      if (emaRows.length > 0) {
        const emaHeaders = emaRows[0];
        const emaIdx = (name) => emaHeaders.indexOf(name);
        const emaSymbolIdx = emaIdx("symbol");
        const truthy = (v) => v === true || v === "TRUE" || v === "true";
        emaRows.slice(1).forEach((row) => {
          const symbol = row[emaSymbolIdx];
          if (symbol) {
            stLongBysymbol[symbol] = {
              signal: truthy(row[emaIdx("st_long_signal")]),
              daysSinceCross: row[emaIdx("st_long_days_since_cross")] || "",
              pctAboveEma50: row[emaIdx("st_long_pct_above_ema50")] || "",
            };
          }
        });
      }
    } catch (e) {
      // EMA_Signals tab may not exist yet -- proceed without the badge
    }

    const records = rows.slice(1).filter((r) => r[symbolIdx]).map((row) => {
      const rec = { symbol: row[symbolIdx] || "", lastUpdated: row[lastUpdatedIdx] || "" };
      for (const [jsKey, colName] of cfg.fields) {
        rec[jsKey] = row[idx(cfg.prefix + colName)] || "";
      }
      const stLong = stLongBysymbol[rec.symbol];
      rec.stLongSignal = stLong ? stLong.signal : false;
      rec.stLongDaysSinceCross = stLong && stLong.daysSinceCross !== "" ? parseInt(stLong.daysSinceCross) : null;
      rec.stLongPctAboveEma50 = stLong && stLong.pctAboveEma50 !== "" ? parseFloat(stLong.pctAboveEma50) : null;
      return rec;
    });

    const setups = records.filter((r) => r.status === "PENDING_ENTRY" || r.status === "ENTERED");
    const pending = setups.filter((r) => r.status === "PENDING_ENTRY").length;
    const entered = setups.filter((r) => r.status === "ENTERED").length;
    const longCount = cfg.hasDirection ? setups.filter((r) => r.direction === "LONG").length : undefined;
    const shortCount = cfg.hasDirection ? setups.filter((r) => r.direction === "SHORT").length : undefined;
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
