module.exports = async (req, res) => {
  res.setHeader("Cache-Control", "no-store");
  try {
    const resp = await fetch(
      "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
    );
    const all = await resp.json();

    const names = new Set();
    for (const inst of all) {
      if (
        inst.exch_seg === "NFO" &&
        (inst.instrumenttype === "OPTSTK" || inst.instrumenttype === "OPTIDX")
      ) {
        names.add(inst.name);
      }
    }

    res.status(200).json({ symbols: Array.from(names) });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
};
