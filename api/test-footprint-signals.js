/*
 * Large-player "footprint" signal test.
 *
 * Ported from a standalone React component that pulled daily OHLCV from
 * Alpha Vantage via an Anthropic MCP call. This version instead uses the
 * same Angel SmartAPI login + scrip-master pattern already used elsewhere
 * in this repo (see scripts/test-angel-quote.js) to fetch real NSE daily
 * candles, then runs the identical signal math against them.
 *
 * This is a TEST endpoint only -- it doesn't write anywhere or feed the
 * dashboard/notify pipeline. Call it, look at the output, and once the
 * signals look right we can decide where (sync_dashboard.py? a new
 * Telegram filter?) it should actually plug in.
 *
 * Usage (once deployed, or via `vercel dev` locally):
 *   /api/test-footprint-signals?symbol=RELIANCE
 *   /api/test-footprint-signals?symbol=RELIANCE&days=250   (default 250)
 *
 * Required env vars (same ones fno-list.js / option-chain.js already use):
 *   ANGEL_TOTP_SECRET, ANGEL_CLIENT_ID, ANGEL_PASSWORD, ANGEL_API_KEY
 */
const OTPAuth = require("otpauth");

const SIGNAL_META = [
  { key: "volumeSpike", label: "Volume spike" },
  { key: "absorption", label: "Absorption" },
  { key: "rejectionWick", label: "Rejection wick" },
  { key: "tightConsolidation", label: "Tight consolidation" },
  { key: "srDefense", label: "S/R defense" },
  { key: "breakoutVolume", label: "Breakout + volume" },
];

function rollingMean(arr, i, window) {
  if (i < window - 1) return null;
  let sum = 0;
  for (let j = i - window + 1; j <= i; j++) sum += arr[j];
  return sum / window;
}
function rollingMax(arr, i, window) {
  if (i < window - 1) return null;
  let m = -Infinity;
  for (let j = i - window + 1; j <= i; j++) m = Math.max(m, arr[j]);
  return m;
}
function rollingMin(arr, i, window) {
  if (i < window - 1) return null;
  let m = Infinity;
  for (let j = i - window + 1; j <= i; j++) m = Math.min(m, arr[j]);
  return m;
}

// Identical signal logic to the original React version -- only the data
// source changed, not the math.
function computeSignals(rows) {
  const n = rows.length;
  const open = rows.map((r) => r.open);
  const high = rows.map((r) => r.high);
  const low = rows.map((r) => r.low);
  const close = rows.map((r) => r.close);
  const volume = rows.map((r) => r.volume);

  const out = rows.map(() => ({}));

  for (let i = 0; i < n; i++) {
    const avgVol20 = rollingMean(volume, i, 20);
    const avgVol10 = rollingMean(volume, i, 10);

    out[i].volumeSpike = avgVol20 != null && volume[i] > avgVol20 * 2;

    const pctMove = Math.abs(close[i] - open[i]) / open[i];
    const pctMoves = [];
    for (let j = Math.max(0, i - 19); j <= i; j++) {
      pctMoves.push(Math.abs(close[j] - open[j]) / open[j]);
    }
    const avgMove = pctMoves.reduce((a, b) => a + b, 0) / pctMoves.length;
    out[i].absorption = avgVol10 != null && volume[i] > avgVol10 * 1.5 && pctMove < avgMove * 0.5;

    const body = Math.abs(close[i] - open[i]) || 0.0001;
    const upperWick = high[i] - Math.max(close[i], open[i]);
    const lowerWick = Math.min(close[i], open[i]) - low[i];
    const longWick = upperWick > 2 * body || lowerWick > 2 * body;
    const recentHigh = rollingMax(high, i, 20);
    const recentLow = rollingMin(low, i, 20);
    const nearHigh = recentHigh != null && Math.abs(recentHigh - high[i]) / recentHigh <= 0.02;
    const nearLow = recentLow != null && Math.abs(low[i] - recentLow) / recentLow <= 0.02;
    out[i].rejectionWick = longWick && (nearHigh || nearLow);

    const rHigh10 = rollingMax(high, i, 10);
    const rLow10 = rollingMin(low, i, 10);
    out[i].tightConsolidation = rHigh10 != null && (rHigh10 - rLow10) / close[i] < 0.08;

    let srTouches = 0;
    if (i >= 10) {
      for (let j = Math.max(0, i - 60); j < i; j++) {
        if (Math.abs(low[j] - low[i]) / low[i] <= 0.015) srTouches++;
        if (Math.abs(high[j] - high[i]) / high[i] <= 0.015) srTouches++;
      }
    }
    out[i].srDefense = srTouches >= 3;

    const priorHigh = i > 0 ? rollingMax(high, i - 1, 20) : null;
    out[i].breakoutVolume =
      priorHigh != null && avgVol20 != null && close[i] > priorHigh && volume[i] > avgVol20 * 2;

    out[i].score = SIGNAL_META.reduce((s, m) => s + (out[i][m.key] ? 1 : 0), 0);
  }
  return rows.map((r, i) => ({ ...r, ...out[i] }));
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
  if (!jwtToken) {
    throw new Error(`Angel login failed: ${JSON.stringify(loginData)}`);
  }
  return { jwtToken, commonHeaders };
}

async function resolveNseToken(symbol) {
  const scripResp = await fetch(
    "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
  );
  const allInstruments = await scripResp.json();
  // Cash-market equity rows use a "SYMBOL-EQ" trading symbol on exch_seg NSE.
  const match = allInstruments.find(
    (inst) => inst.exch_seg === "NSE" && inst.symbol === `${symbol}-EQ`
  );
  if (!match) {
    throw new Error(`Could not resolve an NSE-EQ token for "${symbol}" in the scrip master.`);
  }
  return match.token;
}

function formatAngelDate(d) {
  const pad = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} 09:15`;
}

async function fetchDailyCandles(symbol, jwtToken, commonHeaders, lookbackDays) {
  const token = await resolveNseToken(symbol);

  const todate = new Date();
  const fromdate = new Date();
  // Calendar-day lookback, padded, since only ~5/7 days are trading days.
  fromdate.setDate(fromdate.getDate() - Math.ceil((lookbackDays * 7) / 5) - 10);

  const resp = await fetch(
    "https://apiconnect.angelone.in/rest/secure/angelbroking/historical/v1/getCandleData",
    {
      method: "POST",
      headers: { ...commonHeaders, Authorization: `Bearer ${jwtToken}` },
      body: JSON.stringify({
        exchange: "NSE",
        symboltoken: token,
        interval: "ONE_DAY",
        fromdate: formatAngelDate(fromdate),
        todate: formatAngelDate(todate),
      }),
    }
  );
  const data = await resp.json();
  if (!data?.status || !Array.isArray(data.data)) {
    throw new Error(`Angel getCandleData failed for ${symbol}: ${JSON.stringify(data)}`);
  }

  return data.data.map((c) => ({
    date: c[0].slice(0, 10),
    open: c[1],
    high: c[2],
    low: c[3],
    close: c[4],
    volume: c[5],
  }));
}

module.exports = async (req, res) => {
  const required = ["ANGEL_TOTP_SECRET", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD", "ANGEL_API_KEY"];
  const missing = required.filter((key) => !process.env[key]);
  if (missing.length > 0) {
    res.status(200).json({ error: "Missing environment variables", missing_vars: missing });
    return;
  }

  const symbol = (req.query.symbol || "RELIANCE").toUpperCase();
  const lookbackDays = Number(req.query.days) || 250;

  try {
    const { jwtToken, commonHeaders } = await angelLogin();
    const rows = await fetchDailyCandles(symbol, jwtToken, commonHeaders, lookbackDays);

    if (rows.length < 30) {
      res.status(200).json({
        symbol,
        error: `Only got ${rows.length} candles back -- not enough history for the rolling-window signals (need 60+).`,
      });
      return;
    }

    const scored = computeSignals(rows);
    const recent = scored.slice(-60);
    const avgScore = recent.reduce((s, d) => s + d.score, 0) / recent.length;
    const highScoreDays = recent.filter((d) => d.score >= 3).reverse();

    res.status(200).json({
      symbol,
      candles_fetched: rows.length,
      last_60_sessions: recent.length,
      avg_footprint_score: Number(avgScore.toFixed(2)),
      high_footprint_days: highScoreDays.map((d) => ({
        date: d.date,
        close: d.close,
        score: d.score,
        signals: SIGNAL_META.filter((m) => d[m.key]).map((m) => m.label),
      })),
      latest_day: {
        date: recent[recent.length - 1].date,
        close: recent[recent.length - 1].close,
        score: recent[recent.length - 1].score,
        signals: SIGNAL_META.filter((m) => recent[recent.length - 1][m.key]).map((m) => m.label),
      },
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
