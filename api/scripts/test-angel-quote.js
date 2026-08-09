const OTPAuth = require("otpauth");

module.exports = async (req, res) => {
  const symbol = req.query.symbol || "RELIANCE";

  try {
    // Step 1: login
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
      res.status(200).json({ step: "login", error: loginData });
      return;
    }

    // Step 2: get scrip master, filter to nearest expiry
    const scripResp = await fetch(
      "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    );
    const allInstruments = await scripResp.json();

    const matches = allInstruments.filter(
      (inst) =>
        inst.exch_seg === "NFO" &&
        inst.name === symbol &&
        (inst.instrumenttype === "OPTSTK" || inst.instrumenttype === "OPTIDX")
    );

    const expiries = [...new Set(matches.map((m) => m.expiry))].sort();
    const nearestExpiry = expiries[0];
    const nearestContracts = matches.filter((m) => m.expiry === nearestExpiry);

    // Take just the first 10 tokens for this field-inspection test
    const testTokens = nearestContracts.slice(0, 10).map((c) => c.token);

    // Step 3: batch quote call
    const quoteResp = await fetch(
      "https://apiconnect.angelone.in/rest/secure/angelbroking/market/v1/quote/",
      {
        method: "POST",
        headers: {
          ...commonHeaders,
          Authorization: `Bearer ${jwtToken}`,
        },
        body: JSON.stringify({
          mode: "FULL",
          exchangeTokens: { NFO: testTokens },
        }),
      }
    );

    const quoteStatus = quoteResp.status;
    const quoteData = await quoteResp.json();

    res.status(200).json({
      symbol,
      nearest_expiry: nearestExpiry,
      total_contracts_this_expiry: nearestContracts.length,
      quote_http_status: quoteStatus,
      quote_response: quoteData,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
