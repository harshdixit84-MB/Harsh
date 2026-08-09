module.exports = async (req, res) => {
  const symbol = req.query.symbol || "RELIANCE";

  try {
    const startTime = Date.now();

    const resp = await fetch(
      "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    );

    const fetchTime = Date.now() - startTime;
    const allInstruments = await resp.json();
    const parseTime = Date.now() - startTime - fetchTime;

    const matches = allInstruments.filter(
      (inst) =>
        inst.exch_seg === "NFO" &&
        inst.name === symbol &&
        (inst.instrumenttype === "OPTSTK" || inst.instrumenttype === "OPTIDX")
    );

    // Group by expiry so we can see what expiries are even available
    const expiries = [...new Set(matches.map((m) => m.expiry))];

    res.status(200).json({
      symbol,
      total_instruments_in_file: allInstruments.length,
      fetch_time_ms: fetchTime,
      parse_time_ms: parseTime,
      matching_option_contracts: matches.length,
      available_expiries: expiries.slice(0, 5),
      sample_contract: matches[0] || null,
    });
  } catch (err) {
    res.status(500).json({ error: err.message, error_name: err.name });
  }
};
