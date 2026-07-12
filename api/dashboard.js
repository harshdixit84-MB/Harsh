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

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "Sheet1!A1:O1000",
    });

    let dvSummaryBysymbol = {};
    try {
      const dvResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "DV_Summary!A1:E1000",
      });
      const dvRows = dvResponse.data.values || [];
      if (dvRows.length > 0) {
        const dvHeaders = dvRows[0];
        const symbolIdx = dvHeaders.indexOf("symbol");
        const tagIdx = dvHeaders.indexOf("high_dv_tag");
        dvRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          const tag = row[tagIdx];
          if (symbol) {
            dvSummaryBysymbol[symbol] = tag === "TRUE" || tag === "true" || tag === true;
          }
        });
      }
    } catch (e) {
      // DV_Summary tab may not exist yet -- proceed without it
    }

    const rows = response.data.values || [];
    if (rows.length === 0) {
      res.status(200).json({ stocks: [], syncedAt: new Date().toISOString() });
      return;
    }

    const headers = rows[0];
    const dataRows = rows.slice(1);

    const records = dataRows
      .map((row) => {
        const obj = {};
        headers.forEach((h, i) => {
          obj[h] = row[i] !== undefined ? row[i] : "";
        });
        return obj;
      })
      .filter((r) => r.symbol);

    const withTarget = [];
    const withoutTarget = [];

    for (const r of records) {
      if (r.status === "archived") continue;

      const buyTarget = r.buy_target;
      const price = parseFloat(r.price);

      if (buyTarget && buyTarget !== "" && parseFloat(buyTarget) !== 0) {
        const target = parseFloat(buyTarget);
        const distancePct = ((price - target) / target) * 100;
        r.distance_pct = Math.round(distancePct * 100) / 100;
        r.buy_target = target;
        withTarget.push(r);
      } else {
        withoutTarget.push(r);
      }

      r.price = price;
      r.percent_change = parseFloat(r.percent_change) || 0;
      r.rsi = r.rsi !== "" ? parseFloat(r.rsi) : null;
      r.adx = r.adx !== "" ? parseFloat(r.adx) : null;
      r.signal_score = r.signal_score !== "" ? parseInt(r.signal_score) : null;
      r.signal_label = r.signal_label || null;
      r.consolidating = r.consolidating === true || r.consolidating === "TRUE" || r.consolidating === "true";
      r.high_dv = dvSummaryBysymbol[r.symbol] || false;
    }

    withTarget.sort((a, b) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct));

    res.status(200).json({
      stocks: [...withTarget, ...withoutTarget],
      syncedAt: new Date().toISOString(),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
