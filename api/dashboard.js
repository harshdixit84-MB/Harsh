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
      range: "Sheet1!A1:AF1000",
    });

    let dvSummaryBysymbol = {};
    try {
      const dvResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "DV_Summary!A1:L1000",
      });
      const dvRows = dvResponse.data.values || [];
      if (dvRows.length > 0) {
        const dvHeaders = dvRows[0];
        const symbolIdx = dvHeaders.indexOf("symbol");
        const adp5Idx = dvHeaders.indexOf("adp_5");
        const adp20Idx = dvHeaders.indexOf("adp_20");
        const adp5TrendIdx = dvHeaders.indexOf("adp5_trend");
        const adp20TrendIdx = dvHeaders.indexOf("adp20_trend");
        const crossoverIdx = dvHeaders.indexOf("crossover");
        const crossStateIdx = dvHeaders.indexOf("cross_state");
        const crossoverAgeIdx = dvHeaders.indexOf("crossover_age");
        const recentBiasIdx = dvHeaders.indexOf("recent_bias");
        const verdictIdx = dvHeaders.indexOf("buying_selling_verdict");
        const decisionIdx = dvHeaders.indexOf("decision");
        dvRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol) {
            dvSummaryBysymbol[symbol] = {
              adp5: adp5Idx !== -1 ? row[adp5Idx] : "",
              adp20: adp20Idx !== -1 ? row[adp20Idx] : "",
              adp5Trend: adp5TrendIdx !== -1 ? row[adp5TrendIdx] || "" : "",
              adp20Trend: adp20TrendIdx !== -1 ? row[adp20TrendIdx] || "" : "",
              crossover: crossoverIdx !== -1 ? row[crossoverIdx] || "" : "",
              crossState: crossStateIdx !== -1 ? row[crossStateIdx] || "" : "",
              crossoverAge: crossoverAgeIdx !== -1 ? row[crossoverAgeIdx] || "" : "",
              recentBias: recentBiasIdx !== -1 ? row[recentBiasIdx] || "" : "",
              buyingSellingVerdict: verdictIdx !== -1 ? row[verdictIdx] || "" : "",
              decision: decisionIdx !== -1 ? row[decisionIdx] || "" : "",
            };
          }
        });
      }
    } catch (e) {
      // DV_Summary tab may not exist yet -- proceed without it
    }

    let emaSignalsBysymbol = {};
    try {
      const emaResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "EMA_Signals!A1:U1000",
      });
      const emaRows = emaResponse.data.values || [];
      if (emaRows.length > 0) {
        const emaHeaders = emaRows[0];
        const idx = (name) => emaHeaders.indexOf(name);
        const symbolIdx = idx("symbol");
        emaRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol) {
            emaSignalsBysymbol[symbol] = {
              crossSignal: row[idx("ema_cross_signal")] || "",
              crossEntry: row[idx("ema_cross_entry")] || "",
              crossStop: row[idx("ema_cross_stop")] || "",
              crossTarget: row[idx("ema_cross_target")] || "",
              crossRR: row[idx("ema_cross_rr")] || "",
              pullbackSignal: row[idx("ema_pullback_signal")] || "",
              pullbackPattern: row[idx("ema_pullback_pattern")] || "",
              pullbackEntry: row[idx("ema_pullback_entry")] || "",
              pullbackStop: row[idx("ema_pullback_stop")] || "",
              pullbackTarget: row[idx("ema_pullback_target")] || "",
              pullbackRR: row[idx("ema_pullback_rr")] || "",
              pullbackPct: row[idx("ema_pullback_pct")] || "",
              retestSignal: row[idx("ema_retest_signal")] || "",
              retestTouchedEma: row[idx("ema_retest_touched_ema")] || "",
              retestDaysSinceCross: row[idx("ema_retest_days_since_cross")] || "",
              retestEntry: row[idx("ema_retest_entry")] || "",
              retestStop: row[idx("ema_retest_stop")] || "",
              retestTarget: row[idx("ema_retest_target")] || "",
              retestRR: row[idx("ema_retest_rr")] || "",
            };
          }
        });
      }
    } catch (e) {
      // EMA_Signals tab may not exist yet -- proceed without it
    }

    let footprintBysymbol = {};
    try {
      const fpResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Footprint_Signals!A1:O1000",
      });
      const fpRows = fpResponse.data.values || [];
      if (fpRows.length > 0) {
        const fpHeaders = fpRows[0];
        const idx = (name) => fpHeaders.indexOf(name);
        const symbolIdx = idx("symbol");
        fpRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol) {
            footprintBysymbol[symbol] = {
              score: row[idx("footprint_score")] || "",
              weightedScore: row[idx("footprint_weighted_score")] || "",
              volumeComponent: row[idx("footprint_volume_component")] || "",
              patternComponent: row[idx("footprint_pattern_component")] || "",
              volumeSignals: row[idx("footprint_volume_signals")] || "",
              patternSignals: row[idx("footprint_pattern_signals")] || "",
              weeklyAccumulation: row[idx("weekly_accumulation")] || "",
              weeklyVolRatio: row[idx("weekly_vol_ratio")] || "",
              weeklyBias: row[idx("weekly_bias")] || "",
              weeklyPriceRunPct: row[idx("weekly_price_run_pct")] || "",
              lastDate: row[idx("last_footprint_date")] || "",
              lastWeightedScore: row[idx("last_footprint_weighted_score")] || "",
              daysSince: row[idx("days_since_footprint")] || "",
            };
          }
        });
      }
    } catch (e) {
      // Footprint_Signals tab may not exist yet -- proceed without it
    }

    let notesBysymbol = {};
    try {
      const notesResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Ticker_Notes!A1:E20000",
      });
      const notesRows = notesResponse.data.values || [];
      if (notesRows.length > 0) {
        const notesHeaders = notesRows[0];
        const idIdx = notesHeaders.indexOf("id");
        const symbolIdx = notesHeaders.indexOf("symbol");
        const dateIdx = notesHeaders.indexOf("date");
        const commentIdx = notesHeaders.indexOf("comment");
        const createdIdx = notesHeaders.indexOf("created_at");
        notesRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (!symbol) return;
          if (!notesBysymbol[symbol]) notesBysymbol[symbol] = [];
          notesBysymbol[symbol].push({
            id: idIdx !== -1 ? row[idIdx] || "" : "",
            date: dateIdx !== -1 ? row[dateIdx] || "" : "",
            comment: commentIdx !== -1 ? row[commentIdx] || "" : "",
            created_at: createdIdx !== -1 ? row[createdIdx] || "" : "",
          });
        });
      }
    } catch (e) {
      // Ticker_Notes tab may not exist yet -- proceed without it (created on first note added)
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
      r.archived = r.status === "archived";

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

      r.target_1 = r.target_1 && r.target_1 !== "" ? parseFloat(r.target_1) : "";
      r.target_2 = r.target_2 && r.target_2 !== "" ? parseFloat(r.target_2) : "";

      r.price = price;
      r.percent_change = parseFloat(r.percent_change) || 0;
      r.rsi = r.rsi !== "" ? parseFloat(r.rsi) : null;
      r.adx = r.adx !== "" ? parseFloat(r.adx) : null;
      r.stop_loss = r.stop_loss !== "" ? parseFloat(r.stop_loss) : null;
      if (r.stop_loss && r.stop_loss > 0) {
        r.distance_to_sl_pct = Math.round(((price - r.stop_loss) / r.stop_loss) * 10000) / 100;
      } else {
        r.distance_to_sl_pct = undefined;
      }
      r.signal_score = r.signal_score !== "" ? parseInt(r.signal_score) : null;
      r.signal_label = r.signal_label || null;
      r.consolidating = r.consolidating === true || r.consolidating === "TRUE" || r.consolidating === "true";

      // Breakout-quality layer (additive) -- one score + flag string instead
      // of separate columns, so the frontend can show a single badge.
      r.rs_vs_nifty = r.rs_vs_nifty !== "" && r.rs_vs_nifty !== undefined ? parseFloat(r.rs_vs_nifty) : null;
      r.close_location = r.close_location !== "" && r.close_location !== undefined ? parseFloat(r.close_location) : null;
      r.base_days = r.base_days !== "" && r.base_days !== undefined ? parseInt(r.base_days) : null;
      r.breakout_vol_ratio = r.breakout_vol_ratio !== "" && r.breakout_vol_ratio !== undefined ? parseFloat(r.breakout_vol_ratio) : null;
      r.near_52w_high = r.near_52w_high === true || r.near_52w_high === "TRUE" || r.near_52w_high === "true";
      r.quality_score = r.quality_score !== "" && r.quality_score !== undefined ? parseInt(r.quality_score) : null;
      r.quality_flags = r.quality_flags || "";
      r.market_regime = r.market_regime || "";
      r.retest_52w_level = r.retest_52w_level !== "" && r.retest_52w_level !== undefined ? parseFloat(r.retest_52w_level) : null;
      r.days_since_52w_breakout = r.days_since_52w_breakout !== "" && r.days_since_52w_breakout !== undefined ? parseInt(r.days_since_52w_breakout) : null;
      r.retest_pct_from_52w = r.retest_pct_from_52w !== "" && r.retest_pct_from_52w !== undefined ? parseFloat(r.retest_pct_from_52w) : null;
      r.at_52w_retest = r.at_52w_retest === true || r.at_52w_retest === "TRUE" || r.at_52w_retest === "true";
      r.watchlisted = r.watchlisted === true || r.watchlisted === "TRUE" || r.watchlisted === "true";
      r.bought = r.bought === true || r.bought === "TRUE" || r.bought === "true";

      const dv = dvSummaryBysymbol[r.symbol];
      r.adp_5 = dv && dv.adp5 !== "" ? parseFloat(dv.adp5) : null;
      r.adp_20 = dv && dv.adp20 !== "" ? parseFloat(dv.adp20) : null;
      r.adp5_trend = dv?.adp5Trend || "";
      r.adp20_trend = dv?.adp20Trend || "";
      r.adp_crossover = dv?.crossover || "";
      r.adp_cross_state = dv?.crossState || "";
      r.adp_crossover_age = dv && dv.crossoverAge !== "" ? parseInt(dv.crossoverAge) : null;
      r.adp_recent_bias = dv?.recentBias || "";
      r.buying_selling_verdict = dv?.buyingSellingVerdict || "";
      r.dv_decision = dv?.decision || "";

      const fp = footprintBysymbol[r.symbol];
      r.footprint_score = fp && fp.score !== "" ? parseInt(fp.score) : null;
      r.footprint_weighted_score = fp && fp.weightedScore !== "" ? parseInt(fp.weightedScore) : null;
      r.footprint_volume_component = fp && fp.volumeComponent !== "" ? parseInt(fp.volumeComponent) : null;
      r.footprint_pattern_component = fp && fp.patternComponent !== "" ? parseInt(fp.patternComponent) : null;
      r.footprint_volume_signals = fp?.volumeSignals || "";
      r.footprint_pattern_signals = fp?.patternSignals || "";
      r.weekly_accumulation = fp?.weeklyAccumulation === "TRUE" || fp?.weeklyAccumulation === true || fp?.weeklyAccumulation === "true";
      r.weekly_vol_ratio = fp && fp.weeklyVolRatio !== "" ? parseFloat(fp.weeklyVolRatio) : null;
      r.weekly_bias = fp && fp.weeklyBias !== "" ? parseInt(fp.weeklyBias) : null;
      r.weekly_price_run_pct = fp && fp.weeklyPriceRunPct !== "" ? parseFloat(fp.weeklyPriceRunPct) : null;
      r.footprint_last_date = fp?.lastDate || "";
      r.footprint_last_weighted_score = fp && fp.lastWeightedScore !== "" ? parseInt(fp.lastWeightedScore) : null;
      r.footprint_days_since = fp && fp.daysSince !== "" ? parseInt(fp.daysSince) : null;

      const emaSig = emaSignalsBysymbol[r.symbol] || {};
      const truthy = (v) => v === true || v === "TRUE" || v === "true";
      r.ema_cross_signal = truthy(emaSig.crossSignal);
      r.ema_cross_entry = emaSig.crossEntry || "";
      r.ema_cross_stop = emaSig.crossStop || "";
      r.ema_cross_target = emaSig.crossTarget || "";
      r.ema_cross_rr = emaSig.crossRR || "";
      r.ema_pullback_signal = truthy(emaSig.pullbackSignal);
      r.ema_pullback_pattern = emaSig.pullbackPattern || "";
      r.ema_pullback_entry = emaSig.pullbackEntry || "";
      r.ema_pullback_stop = emaSig.pullbackStop || "";
      r.ema_pullback_target = emaSig.pullbackTarget || "";
      r.ema_pullback_rr = emaSig.pullbackRR || "";
      r.ema_pullback_pct = emaSig.pullbackPct || "";
      r.ema_retest_signal = truthy(emaSig.retestSignal);
      r.ema_retest_touched = emaSig.retestTouchedEma || "";
      r.ema_retest_days_since_cross = emaSig.retestDaysSinceCross || "";
      r.ema_retest_entry = emaSig.retestEntry || "";
      r.ema_retest_stop = emaSig.retestStop || "";
      r.ema_retest_target = emaSig.retestTarget || "";
      r.ema_retest_rr = emaSig.retestRR || "";
      r.comments = notesBysymbol[r.symbol] || [];
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
