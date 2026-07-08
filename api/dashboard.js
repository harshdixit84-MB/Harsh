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
      range: "Sheet1!A1:J1000",
    });

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
