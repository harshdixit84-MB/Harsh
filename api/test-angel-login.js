const OTPAuth = require("otpauth");

module.exports = async (req, res) => {
  try {
    const totp = new OTPAuth.TOTP({
      secret: OTPAuth.Secret.fromBase32(process.env.ANGEL_TOTP_SECRET),
      digits: 6,
      period: 30,
    });
    const code = totp.generate();

    const loginResp = await fetch(
      "https://apiconnect.angelone.in/rest/auth/angelbroking/user/v1/loginByPassword",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
          "X-UserType": "USER",
          "X-SourceID": "WEB",
          "X-ClientLocalIP": "127.0.0.1",
          "X-ClientPublicIP": "127.0.0.1",
          "X-MACAddress": "00:00:00:00:00:00",
          "X-PrivateKey": process.env.ANGEL_API_KEY,
        },
        body: JSON.stringify({
          clientcode: process.env.ANGEL_CLIENT_ID,
          password: process.env.ANGEL_PASSWORD,
          totp: code,
        }),
      }
    );

    const status = loginResp.status;
    const data = await loginResp.json();

    res.status(200).json({
      login_status: status,
      success: data?.status === true || data?.status === "true",
      message: data?.message,
      error_code: data?.errorcode,
      got_jwt_token: !!data?.data?.jwtToken,
      got_feed_token: !!data?.data?.feedToken,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
