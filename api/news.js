const { google } = require("googleapis");

const isDismissed = (v) => v === "True" || v === "TRUE" || v === "true" || v === true;

async function getSheetsClient() {
  const credentials = JSON.parse(process.env.GOOGLE_SERVICE_ACCOUNT_KEY);
  const auth = new google.auth.GoogleAuth({
    credentials,
    scopes: ["https://www.googleapis.com/auth/spreadsheets"], // read+write, covers both GET and POST below
  });
  return google.sheets({ version: "v4", auth });
}

async function handleGet(res, sheets) {
  let rows = [];
  try {
    const response = await sheets.spreadsheets.values.get({
      spreadsheetId: process.env.SHEET_ID,
      range: "News!A1:I2000",
    });
    rows = response.data.values || [];
  } catch (e) {
    // News tab may not exist yet (news_monitor.py hasn't run) -- return empty, not an error
    res.status(200).json({ news: [] });
    return;
  }

  if (rows.length === 0) {
    res.status(200).json({ news: [] });
    return;
  }

  const headers = rows[0];
  const dataRows = rows.slice(1);

  const news = dataRows
    .map((row) => {
      const obj = {};
      headers.forEach((h, i) => {
        obj[h] = row[i] !== undefined ? row[i] : "";
      });
      return obj;
    })
    .filter((r) => r.id && !isDismissed(r.dismissed))
    .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0)); // decreasing date order

  res.status(200).json({ news });
}

async function handlePost(req, res, sheets) {
  const { id } = req.body || {};
  if (!id) {
    res.status(400).json({ error: "Missing id" });
    return;
  }

  const response = await sheets.spreadsheets.values.get({
    spreadsheetId: process.env.SHEET_ID,
    range: "News!A1:I2000",
  });

  const rows = response.data.values || [];
  if (rows.length === 0) {
    res.status(404).json({ error: "News tab is empty" });
    return;
  }

  const headers = rows[0];
  const idCol = headers.indexOf("id");
  const dismissedCol = headers.indexOf("dismissed");

  const rowIndex = rows.findIndex((row, i) => i > 0 && row[idCol] === id);
  if (rowIndex === -1) {
    res.status(404).json({ error: "News item not found" });
    return;
  }

  const columnLetter = String.fromCharCode(65 + dismissedCol); // 0 -> A, 1 -> B, ...

  await sheets.spreadsheets.values.update({
    spreadsheetId: process.env.SHEET_ID,
    range: `News!${columnLetter}${rowIndex + 1}`,
    valueInputOption: "RAW",
    requestBody: { values: [["True"]] },
  });

  res.status(200).json({ success: true });
}

module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store, no-cache, must-revalidate");
  try {
    const sheets = await getSheetsClient();

    if (req.method === "GET") {
      await handleGet(res, sheets);
    } else if (req.method === "POST") {
      await handlePost(req, res, sheets);
    } else {
      res.status(405).json({ error: "Method not allowed" });
    }
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
