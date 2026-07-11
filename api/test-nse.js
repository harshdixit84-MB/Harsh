module.exports = async (req, res) => {
  const symbol = req.query.symbol || "RELIANCE";

  try {
    const headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
      "Accept": "application/json",
      "Accept-Language": "en-US,en;q=0.9",
      "Referer": "https://www.nseindia.com/option-chain",
    };

    // Step 1: hit the homepage first to get session cookies -- NSE's API
    // rejects requests that don't carry a valid session cookie.
    const homeResp = await fetch("https://www.nseindia.com/", { headers });

    const rawCookies = homeResp.headers.getSetCookie
      ? homeResp.headers.getSetCookie()
      : [];
    const cookieHeader = rawCookies.map((c) => c.split(";")[0]).join("; ");

    // Step 2: use those cookies to call the actual option chain API
    const apiResp = await fetch(
      `https://www.nseindia.com/api/option-chain-equities?symbol=${symbol}`,
      {
        headers: {
          ...headers,
          Cookie: cookieHeader,
        },
      }
    );

    const status = apiResp.status;
    const text = await apiResp.text();

    let parsed = null;
    let parseError = null;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      parseError = e.message;
    }

    res.status(200).json({
      symbol,
      nse_response_status: status,
      got_cookies: rawCookies.length > 0,
      cookie_count: rawCookies.length,
      parsed_successfully: !!parsed,
      parse_error: parseError,
      raw_preview: text.slice(0, 500),
      records_count: parsed?.records?.data?.length || 0,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
