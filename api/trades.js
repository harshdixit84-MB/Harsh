const { google } = require("googleapis");
const { computeVerdict, isBigMoveSetup } = require("../lib/verdict");

async function getLivePrice(symbol) {
  try {
    const url = `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}.NS`;
    const resp = await fetch(url, { headers: { "User-Agent": "Mozilla/5.0" } });
    const data = await resp.json();
    const price = data?.chart?.result?.[0]?.meta?.regularMarketPrice;
    return price ?? null;
  } catch (e) {
    return null;
  }
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");

  try {
    const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
    const auth = new google.auth.GoogleAuth({
      credentials,
      scopes: ["https://www.googleapis.com/auth/spreadsheets"],
    });
    const sheets = google.sheets({ version: "v4", auth });

    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "Trades!A1:F1000",
    });

    const rows = response.data.values || [];
    if (rows.length === 0) {
      res.status(200).json({
        open: [],
        closed: [],
        summary: { realized: 0, unrealized: 0, openCount: 0, closedCount: 0 },
      });
      return;
    }

    const headers = rows[0];
    const symbolCol = headers.indexOf("symbol");

    const records = rows
      .slice(1)
      .filter((r) => r[symbolCol])
      .map((row) => {
        const obj = {};
        headers.forEach((h, i) => {
          obj[h] = row[i] !== undefined ? row[i] : "";
        });
        return obj;
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
        const dvSymbolIdx = dvHeaders.indexOf("symbol");
        const verdictIdx = dvHeaders.indexOf("buying_selling_verdict");
        const decisionIdx = dvHeaders.indexOf("decision");
        dvRows.slice(1).forEach((row) => {
          const symbol = row[dvSymbolIdx];
          const verdict = verdictIdx !== -1 ? row[verdictIdx] : "";
          const decision = decisionIdx !== -1 ? row[decisionIdx] : "";
          if (symbol) {
            dvSummaryBysymbol[symbol] = {
              buyingSellingVerdict: verdict || "",
              decision: decision || "",
            };
          }
        });
      }
    } catch (e) {
      // DV_Summary tab may not exist yet, or symbol isn't in the tracked
      // screener list -- proceed without it, badges just won't show
    }

    let scanBysymbol = {};
    try {
      const scanResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Sheet1!A1:AF1000",
      });
      const scanRows = scanResponse.data.values || [];
      if (scanRows.length > 0) {
        const scanHeaders = scanRows[0];
        const idx = (name) => scanHeaders.indexOf(name);
        const symbolIdx = idx("symbol");
        scanRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol) {
            scanBysymbol[symbol] = {
              qualityScore: row[idx("quality_score")],
              marketRegime: row[idx("market_regime")],
            };
          }
        });
      }
    } catch (e) {
      // Sheet1 lookup failed -- verdict will just show "Quality score not available yet"
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
      // Footprint_Signals tab may not exist yet -- proceed without it, badges just won't show
    }

    let harmonicBysymbol = {};
    try {
      const hpResponse = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: "Harmonic_Patterns!A1:U1000",
      });
      const hpRows = hpResponse.data.values || [];
      if (hpRows.length > 0) {
        const hpHeaders = hpRows[0];
        const idx = (name) => hpHeaders.indexOf(name);
        const symbolIdx = idx("symbol");
        hpRows.slice(1).forEach((row) => {
          const symbol = row[symbolIdx];
          if (symbol && row[idx("pattern")]) {
            harmonicBysymbol[symbol] = {
              pattern: row[idx("pattern")],
              direction: row[idx("direction")],
              xPrice: row[idx("x_price")], aPrice: row[idx("a_price")],
              bPrice: row[idx("b_price")], cPrice: row[idx("c_price")], cDate: row[idx("c_date")],
              przLow: row[idx("prz_low")], przHigh: row[idx("prz_high")],
              currentPrice: row[idx("current_price")], distanceToPrzPct: row[idx("distance_to_prz_pct")],
              stopLoss: row[idx("stop_loss")],
              target1: row[idx("target_1")], target2: row[idx("target_2")], target3: row[idx("target_3")],
              daysSinceC: row[idx("days_since_c")],
            };
          }
        });
      }
    } catch (e) {
      // Harmonic_Patterns tab may not exist yet -- proceed without it
    }

    function attachDvInfo(t) {
      t.buying_selling_verdict = dvSummaryBysymbol[t.symbol]?.buyingSellingVerdict || "";
      t.dv_decision = dvSummaryBysymbol[t.symbol]?.decision || "";
      const scan = scanBysymbol[t.symbol];
      t.quality_score = scan && scan.qualityScore !== undefined && scan.qualityScore !== "" ? parseInt(scan.qualityScore) : null;
      t.market_regime = scan?.marketRegime || "";
      const fp = footprintBysymbol[t.symbol];
      t.footprint_weighted_score = fp && fp.weightedScore !== "" ? parseInt(fp.weightedScore) : null;
      t.footprint_volume_component = fp && fp.volumeComponent !== "" ? parseInt(fp.volumeComponent) : null;
      t.footprint_pattern_component = fp && fp.patternComponent !== "" ? parseInt(fp.patternComponent) : null;
      t.footprint_volume_signals = fp?.volumeSignals || "";
      t.footprint_pattern_signals = fp?.patternSignals || "";
      t.weekly_accumulation = fp?.weeklyAccumulation === "TRUE" || fp?.weeklyAccumulation === true || fp?.weeklyAccumulation === "true";
      t.weekly_vol_ratio = fp && fp.weeklyVolRatio !== "" ? parseFloat(fp.weeklyVolRatio) : null;
      t.weekly_bias = fp && fp.weeklyBias !== "" ? parseInt(fp.weeklyBias) : null;
      t.weekly_price_run_pct = fp && fp.weeklyPriceRunPct !== "" ? parseFloat(fp.weeklyPriceRunPct) : null;
      t.footprint_last_date = fp?.lastDate || "";
      t.footprint_last_weighted_score = fp && fp.lastWeightedScore !== "" ? parseInt(fp.lastWeightedScore) : null;
      t.footprint_days_since = fp && fp.daysSince !== "" ? parseInt(fp.daysSince) : null;
      const v = computeVerdict(t);
      t.verdict = v.verdict;
      t.verdict_reason = v.reason;
      t.big_move_setup = isBigMoveSetup(t);

      const hp = harmonicBysymbol[t.symbol];
      if (hp) {
        t.harmonic_pattern = hp.pattern;
        t.harmonic_direction = hp.direction;
        t.harmonic_x = parseFloat(hp.xPrice);
        t.harmonic_a = parseFloat(hp.aPrice);
        t.harmonic_b = parseFloat(hp.bPrice);
        t.harmonic_c = parseFloat(hp.cPrice);
        t.harmonic_c_date = hp.cDate;
        t.harmonic_prz_low = parseFloat(hp.przLow);
        t.harmonic_prz_high = parseFloat(hp.przHigh);
        t.harmonic_current_price = parseFloat(hp.currentPrice);
        t.harmonic_distance_to_prz_pct = parseFloat(hp.distanceToPrzPct);
        t.harmonic_stop_loss = parseFloat(hp.stopLoss);
        t.harmonic_target_1 = parseFloat(hp.target1);
        t.harmonic_target_2 = parseFloat(hp.target2);
        t.harmonic_target_3 = parseFloat(hp.target3);
        t.harmonic_days_since_c = hp.daysSinceC !== "" ? parseInt(hp.daysSinceC) : null;
      } else {
        t.harmonic_pattern = null;
      }
      return t;
    }

    const openTrades = records.filter((r) => !r.sell_price).map(attachDvInfo);
    const closedTrades = records.filter((r) => r.sell_price).map(attachDvInfo);

    const livePrices = await Promise.all(openTrades.map((t) => getLivePrice(t.symbol)));

    let totalUnrealized = 0;
    openTrades.forEach((t, i) => {
      const buy = parseFloat(t.buy_price);
      const qty = parseFloat(t.quantity) || 1;
      const current = livePrices[i];
      t.current_price = current;
      t.quantity = qty;
      if (current !== null && !isNaN(current)) {
        t.pl_amount = Math.round((current - buy) * qty * 100) / 100;
        t.pl_percent = Math.round(((current - buy) / buy) * 10000) / 100;
        totalUnrealized += t.pl_amount;
      } else {
        t.pl_amount = null;
        t.pl_percent = null;
      }
    });

    let totalRealized = 0;
    closedTrades.forEach((t) => {
      const buy = parseFloat(t.buy_price);
      const sell = parseFloat(t.sell_price);
      const qty = parseFloat(t.quantity) || 1;
      t.quantity = qty;
      t.pl_amount = Math.round((sell - buy) * qty * 100) / 100;
      t.pl_percent = Math.round(((sell - buy) / buy) * 10000) / 100;
      totalRealized += t.pl_amount;
    });

    res.status(200).json({
      open: openTrades,
      closed: closedTrades.reverse(),
      summary: {
        realized: Math.round(totalRealized * 100) / 100,
        unrealized: Math.round(totalUnrealized * 100) / 100,
        openCount: openTrades.length,
        closedCount: closedTrades.length,
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
