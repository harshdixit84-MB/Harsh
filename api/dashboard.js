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
      range: "Sheet1!A1:T1000",
    });

    let dvSummaryBysymbol = {};
    try {
      const dvResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "DV_Summary!A1:G1000",
      });
      const dvRows = dvResponse.data.values || [];
      if (dvRows.length > 0) {
        const dvHeaders = dvRows[0];
        const symbolIdx = dvHeaders.indexOf("symbol");
        const tagIdx = dvHeaders.indexOf("high_dv_tag");
        const verdictIdx = dvHeaders.indexOf("buying_selling_verdict");
        dvRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          const tag = row[tagIdx];
          const verdict = verdictIdx !== -1 ? row[verdictIdx] : "";
          if (symbol) {
            dvSummaryBysymbol[symbol] = {
              highDv: tag === "TRUE" || tag === "true" || tag === true,
              buyingSellingVerdict: verdict || "",
            };
          }
        });
      }
    } catch (e) {
      // DV_Summary tab may not exist yet -- proceed without it
    }

    let harmonicBysymbol = {};
    try {
      const hpResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Harmonic_Patterns!A1:F1000",
      });
      const hpRows = hpResponse.data.values || [];
      if (hpRows.length > 0) {
        const hpHeaders = hpRows[0];
        const symbolIdx = hpHeaders.indexOf("symbol");
        const patternIdx = hpHeaders.indexOf("pattern_name");
        const statusIdx = hpHeaders.indexOf("status");
        const dPriceIdx = hpHeaders.indexOf("d_price");
        const confidenceIdx = hpHeaders.indexOf("confidence");
        hpRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          const pattern = row[patternIdx];
          if (symbol && pattern) {
            harmonicBysymbol[symbol] = {
              pattern,
              status: row[statusIdx] || "",
              dPrice: row[dPriceIdx] || "",
              confidence: row[confidenceIdx] || "",
            };
          }
        });
      }
    } catch (e) {
      // Harmonic_Patterns tab may not exist yet -- proceed without it
    }

    let wmBysymbol = {};
    try {
      const wmResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "WM_Patterns!A1:H1000",
      });
      const wmRows = wmResponse.data.values || [];
      if (wmRows.length > 0) {
        const wmHeaders = wmRows[0];
        const symbolIdx = wmHeaders.indexOf("symbol");
        const patternIdx = wmHeaders.indexOf("pattern");
        const statusIdx = wmHeaders.indexOf("status");
        const breakoutLevelIdx = wmHeaders.indexOf("breakout_level");
        const distanceIdx = wmHeaders.indexOf("distance_to_breakout_pct");
        const symmetryIdx = wmHeaders.indexOf("symmetry_pct");
        wmRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          const pattern = row[patternIdx];
          if (symbol && pattern) {
            wmBysymbol[symbol] = {
              pattern,
              status: row[statusIdx] || "",
              breakoutLevel: row[breakoutLevelIdx] || "",
              distancePct: row[distanceIdx] || "",
              symmetryPct: row[symmetryIdx] || "",
            };
          }
        });
      }
    } catch (e) {
      // WM_Patterns tab may not exist yet -- proceed without it
    }

    let rsiDivBysymbol = {};
    try {
      const rsiResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "RSI_Divergence!A1:F1000",
      });
      const rsiRows = rsiResponse.data.values || [];
      if (rsiRows.length > 0) {
        const rsiHeaders = rsiRows[0];
        const symbolIdx = rsiHeaders.indexOf("symbol");
        const dailyDivIdx = rsiHeaders.indexOf("daily_divergence");
        const dailyDaysIdx = rsiHeaders.indexOf("daily_days_ago");
        const weeklyDivIdx = rsiHeaders.indexOf("weekly_divergence");
        const weeklyDaysIdx = rsiHeaders.indexOf("weekly_days_ago");
        rsiRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol) {
            rsiDivBysymbol[symbol] = {
              dailyDivergence: row[dailyDivIdx] || "",
              dailyDaysAgo: row[dailyDaysIdx] || "",
              weeklyDivergence: row[weeklyDivIdx] || "",
              weeklyDaysAgo: row[weeklyDaysIdx] || "",
            };
          }
        });
      }
    } catch (e) {
      // RSI_Divergence tab may not exist yet -- proceed without it
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
      r.stop_loss = r.stop_loss !== "" ? parseFloat(r.stop_loss) : null;
      r.signal_score = r.signal_score !== "" ? parseInt(r.signal_score) : null;
      r.signal_label = r.signal_label || null;
      r.consolidating = r.consolidating === true || r.consolidating === "TRUE" || r.consolidating === "true";
      r.high_dv = dvSummaryBysymbol[r.symbol]?.highDv || false;
      r.buying_selling_verdict = dvSummaryBysymbol[r.symbol]?.buyingSellingVerdict || "";
      r.harmonic_pattern = harmonicBysymbol[r.symbol]?.pattern || null;
      r.harmonic_status = harmonicBysymbol[r.symbol]?.status || "";
      r.harmonic_d_price = harmonicBysymbol[r.symbol]?.dPrice || "";
      r.harmonic_confidence = harmonicBysymbol[r.symbol]?.confidence || "";
      r.wm_pattern = wmBysymbol[r.symbol]?.pattern || null;
      r.wm_status = wmBysymbol[r.symbol]?.status || "";
      r.wm_breakout_level = wmBysymbol[r.symbol]?.breakoutLevel || "";
      r.wm_distance_pct = wmBysymbol[r.symbol]?.distancePct || "";
      r.wm_symmetry_pct = wmBysymbol[r.symbol]?.symmetryPct || "";
      r.rsi_daily_divergence = rsiDivBysymbol[r.symbol]?.dailyDivergence || null;
      r.rsi_daily_days_ago = rsiDivBysymbol[r.symbol]?.dailyDaysAgo !== "" ? rsiDivBysymbol[r.symbol]?.dailyDaysAgo : null;
      r.rsi_weekly_divergence = rsiDivBysymbol[r.symbol]?.weeklyDivergence || null;
      r.rsi_weekly_days_ago = rsiDivBysymbol[r.symbol]?.weeklyDaysAgo !== "" ? rsiDivBysymbol[r.symbol]?.weeklyDaysAgo : null;
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
