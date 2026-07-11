const OTPAuth = require("otpauth");
const { google } = require("googleapis");

const SNAPSHOT_SHEET = "OI_Snapshots";

async function getSheetsClient() {
  const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
  const auth = new google.auth.GoogleAuth({
    credentials,
    scopes: ["https://www.googleapis.com/auth/spreadsheets"],
  });
  return google.sheets({ version: "v4", auth });
}

async function angelLogin() {
  const totp = new OTPAuth.TOTP({
    secret: OTPAuth.Secret.fromBase32(process.env.ANGEL_TOTP_SECRET),
    digits: 6,
    period: 30,
  });

  const commonHeaders = {
    "Content-Type": "application/json",
    Accept: "application/json",
    "X-UserType": "USER",
    "X-SourceID": "WEB",
    "X-ClientLocalIP": "127.0.0.1",
    "X-ClientPublicIP": "127.0.0.1",
    "X-MACAddress": "00:00:00:00:00:00",
    "X-PrivateKey": process.env.ANGEL_API_KEY,
  };

  const loginResp = await fetch(
    "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword",
    {
      method: "POST",
      headers: commonHeaders,
      body: JSON.stringify({
        clientcode: process.env.ANGEL_CLIENT_ID,
        password: process.env.ANGEL_PASSWORD,
        totp: totp.generate(),
      }),
    }
  );
  const loginData = await loginResp.json();
  const jwtToken = loginData?.data?.jwtToken;
  if (!jwtToken) throw new Error("Angel One login failed: " + JSON.stringify(loginData));
  return { jwtToken, commonHeaders };
}

async function fetchQuotesInChunks(tokens, jwtToken, commonHeaders) {
  const CHUNK_SIZE = 50;
  const allFetched = [];
  for (let i = 0; i < tokens.length; i += CHUNK_SIZE) {
    const chunk = tokens.slice(i, i + CHUNK_SIZE);
    const resp = await fetch(
      "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
      {
        method: "POST",
        headers: { ...commonHeaders, Authorization: `Bearer ${jwtToken}` },
        body: JSON.stringify({ mode: "FULL", exchangeTokens: { NFO: chunk } }),
      }
    );
    const data = await resp.json();
    const fetched = data?.data?.fetched || [];
    allFetched.push(...fetched);
  }
  return allFetched;
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  const symbol = (req.query.symbol || "").toUpperCase();

  if (!symbol) {
    res.status(400).json({ error: "symbol query param required" });
    return;
  }

  try {
    const { jwtToken, commonHeaders } = await angelLogin();

    const scripResp = await fetch(
      "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    );
    const allInstruments = await scripResp.json();

    const optionContracts = allInstruments.filter(
      (inst) =>
        inst.exch_seg === "NFO" &&
        inst.name === symbol &&
        (inst.instrumenttype === "OPTSTK" || inst.instrumenttype === "OPTIDX")
    );

    if (optionContracts.length === 0) {
      res.status(404).json({ error: `No F&O contracts found for ${symbol}` });
      return;
    }

    const expiries = [...new Set(optionContracts.map((c) => c.expiry))].sort(
      (a, b) => new Date(a) - new Date(b)
    );
    const nearestExpiry = expiries[0];
    const nearestContracts = optionContracts.filter((c) => c.expiry === nearestExpiry);

    const equityMatch = allInstruments.find(
      (inst) => inst.exch_seg === "NSE" && inst.symbol === `${symbol}-EQ`
    );

    const optionTokens = nearestContracts.map((c) => c.token);
    const tokensToFetch = equityMatch ? [...optionTokens, equityMatch.token] : optionTokens;

    const fetched = await fetchQuotesInChunks(tokensToFetch, jwtToken, commonHeaders);

    const quoteByToken = {};
    fetched.forEach((q) => {
      quoteByToken[q.symbolToken] = q;
    });

    const spotQuote = equityMatch ? quoteByToken[equityMatch.token] : null;
    const spotPrice = spotQuote ? spotQuote.ltp : null;

    const rows = nearestContracts.map((c) => {
      const q = quoteByToken[c.token];
      const strike = parseFloat(c.strike) / 100;
      const type = c.symbol.endsWith("CE") ? "CE" : "PE";
      return {
        strike,
        type,
        oi: q ? q.opnInterest : 0,
        ltp: q ? q.ltp : 0,
      };
    });

    const calls = rows.filter((r) => r.type === "CE");
    const puts = rows.filter((r) => r.type === "PE");

    // ----- Load previous OI snapshots for change tracking -----
    const sheets = await getSheetsClient();
    let existingSnapshots = {};
    try {
      const snapResp = await sheets.spreadsheets.values.get({
        spreadsheetId: process.env.SHEET_ID,
        range: `${SNAPSHOT_SHEET}!A1:C20000`,
      });
      const snapRows = snapResp.data.values || [];
      for (let i = 1; i < snapRows.length; i++) {
        const [key, oi] = snapRows[i];
        existingSnapshots[key] = parseFloat(oi);
      }
    } catch (e) {
      // Sheet may not have any data yet -- proceed with no history
    }

    const hasHistory = Object.keys(existingSnapshots).some((k) =>
      k.startsWith(`${symbol}_${nearestExpiry}_`)
    );

    function attachChange(list) {
      return list.map((r) => {
        const key = `${symbol}_${nearestExpiry}_${r.strike}_${r.type}`;
        const prevOi = existingSnapshots[key];
        const changeOi = prevOi !== undefined ? r.oi - prevOi : null;
        existingSnapshots[key] = r.oi; // update in place for the merged write-back
        return { ...r, changeOi };
      });
    }

    const callsWithChange = attachChange(calls);
    const putsWithChange = attachChange(puts);

    // Write merged snapshots back
    const now = new Date().toISOString();
    const allSnapshotRows = Object.entries(existingSnapshots).map(([key, oi]) => [key, oi, now]);
    await sheets.spreadsheets.values.update({
      spreadsheetId: process.env.SHEET_ID,
      range: `${SNAPSHOT_SHEET}!A1`,
      valueInputOption: "RAW",
      requestBody: { values: [["key", "oi", "updated_at"], ...allSnapshotRows] },
    });

    // ----- Calculations -----
    const totalCallOi = calls.reduce((s, c) => s + c.oi, 0);
    const totalPutOi = puts.reduce((s, p) => s + p.oi, 0);
    const pcr = totalCallOi > 0 ? totalPutOi / totalCallOi : null;

    const topSupport = [...putsWithChange].sort((a, b) => b.oi - a.oi).slice(0, 5);
    const topResistance = [...callsWithChange].sort((a, b) => b.oi - a.oi).slice(0, 5);

    const allStrikes = [...new Set(rows.map((r) => r.strike))].sort((a, b) => a - b);
    let maxPainStrike = null;
    let minPayout = Infinity;
    for (const s of allStrikes) {
      let payout = 0;
      for (const c of calls) {
        if (s > c.strike) payout += (s - c.strike) * c.oi;
      }
      for (const p of puts) {
        if (s < p.strike) payout += (p.strike - s) * p.oi;
      }
      if (payout < minPayout) {
        minPayout = payout;
        maxPainStrike = s;
      }
    }

    // Bias + confidence -- a heuristic composite, not an industry-standard formula
    let bias = "Neutral";
    if (pcr !== null) {
      if (pcr >= 1.2) bias = "Bullish";
      else if (pcr <= 0.8) bias = "Bearish";
    }
    const confidence = pcr !== null ? Math.min(95, Math.round(40 + Math.abs(pcr - 1) * 60)) : 50;

    // Institutional positioning narrative -- simple rules on OI change
    const narrative = [];
    if (!hasHistory) {
      narrative.push("First check for this stock's option chain — check again later to see how positioning is shifting over time.");
    } else {
      const topSupportStrike = topSupport[0];
      const topResistanceStrike = topResistance[0];

      if (topSupportStrike && topSupportStrike.changeOi !== null) {
        if (topSupportStrike.changeOi > 0) {
          narrative.push(`Heavy Put writing at ${topSupportStrike.strike} — support strengthening.`);
        } else if (topSupportStrike.changeOi < 0) {
          narrative.push(`Put unwinding at ${topSupportStrike.strike} — support weakening.`);
        }
      }
      if (topResistanceStrike && topResistanceStrike.changeOi !== null) {
        if (topResistanceStrike.changeOi > 0) {
          narrative.push(`Call writers defending ${topResistanceStrike.strike}.`);
        } else if (topResistanceStrike.changeOi < 0) {
          narrative.push(`Call OI decreasing at ${topResistanceStrike.strike} — resistance weakening.`);
        }
      }
      if (narrative.length === 0) {
        narrative.push("No significant OI shift detected since the last check.");
      }
    }

    // Expected range -- nearest support below spot, nearest resistance above spot
    let expectedLow = null;
    let expectedHigh = null;
    if (spotPrice) {
      const supportsBelow = topSupport
        .filter((s) => s.strike <= spotPrice)
        .sort((a, b) => b.strike - a.strike);
      const resistancesAbove = topResistance
        .filter((r) => r.strike >= spotPrice)
        .sort((a, b) => a.strike - b.strike);
      expectedLow = supportsBelow[0]?.strike ?? null;
      expectedHigh = resistancesAbove[0]?.strike ?? null;
    }

    // Final verdict -- plain-language summary combining the pieces
    let verdict = `${bias} bias (PCR ${pcr !== null ? pcr.toFixed(2) : "n/a"}).`;
    if (maxPainStrike) verdict += ` Max Pain sits at ${maxPainStrike}.`;
    if (expectedLow && expectedHigh) verdict += ` Expected range: ${expectedLow}–${expectedHigh}.`;

    res.status(200).json({
      symbol,
      expiry: nearestExpiry,
      spot_price: spotPrice,
      pcr: pcr !== null ? Math.round(pcr * 100) / 100 : null,
      bias,
      confidence,
      max_pain: maxPainStrike,
      support: topSupport.map((s) => ({ strike: s.strike, oi: s.oi, change_oi: s.changeOi })),
      resistance: topResistance.map((r) => ({ strike: r.strike, oi: r.oi, change_oi: r.changeOi })),
      expected_range: { low: expectedLow, high: expectedHigh },
      narrative,
      verdict,
      is_first_check: !hasHistory,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
