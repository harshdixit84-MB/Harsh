module.exports = async (req, res) => {
  // NSE publishes a daily "full bhavdata" file containing delivery quantity
  // per security. Testing with a recent date to see if this archive
  // subdomain is blocked the same way www.nseindia.com was.
  const dateStr = req.query.date || "10072026"; // DDMMYYYY format NSE uses

  const url = `https://archives.nseindia.com/products/content/sec_bhavdata_full_${dateStr}.csv`;

  try {
    const resp = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
      },
    });

    const status = resp.status;
    const text = await resp.text();

    res.status(200).json({
      url_tried: url,
      response_status: status,
      body_preview: text.slice(0, 500),
      looks_like_csv: text.slice(0, 50).includes(","),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
