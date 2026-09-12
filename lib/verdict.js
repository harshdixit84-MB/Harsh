// Shared between api/dashboard.js and api/trades.js -- same tiering logic
// as notify.py's compute_verdict(), so the dashboard, the P/L tracker, and
// Telegram all agree on what counts as buy-ready.
//
// A footprint requirement is included deliberately: a Confirmed cross alone
// was letting through buys with no actual volume/size evidence behind them,
// which is what fed the losing streak this whole verdict system exists to
// catch. BUY-READY now requires BOTH a confirmed (3+ day) EMA/delivery cross
// AND either a same-day footprint or 3-week accumulation -- not either one
// alone.
function computeVerdict(r) {
  const regime = r.market_regime;
  const decision = r.dv_decision;
  const quality = r.quality_score;
  const hasFootprintSupport =
    r.weekly_accumulation === true ||
    (r.footprint_weighted_score !== null && r.footprint_weighted_score !== undefined && r.footprint_weighted_score >= 4);

  if (regime === "Bearish") return { verdict: "AVOID", reason: "Bearish market regime" };
  if (decision === "Confirmed Sell") return { verdict: "AVOID", reason: "Confirmed Sell signal" };
  if (quality === null || quality === undefined) return { verdict: "WATCH", reason: "Quality score not available yet" };
  if (quality < 2) return { verdict: "AVOID", reason: `Quality ${quality}/5 too low` };
  if (decision === "Confirmed Buy") {
    if (hasFootprintSupport) {
      const footprintNote = r.weekly_accumulation ? "3-week accumulation" : `same-day footprint ${r.footprint_weighted_score}/9`;
      return { verdict: "BUY-READY", reason: `Confirmed Buy + ${footprintNote} · Quality ${quality}/5` };
    }
    return { verdict: "WATCH", reason: "Confirmed cross, but no volume footprint confirmation yet" };
  }
  if (decision === "Early Buy Signal") return { verdict: "WATCH", reason: "Early signal only -- not yet confirmed" };
  if (decision === "Early Sell Signal") return { verdict: "WATCH", reason: "Early sell signal -- watch, don't add" };
  return { verdict: "WATCH", reason: `No confirmed cross yet · Quality ${quality}/5` };
}

module.exports = { computeVerdict, isBigMoveSetup };

// The specific combo built to catch BIG moves, not just any valid entry:
// a Confirmed cross (already required for BUY-READY) backed by WEEKS of
// accumulation specifically -- not just a single day's footprint, since one
// good day is one day, not evidence of the kind of sustained size-building
// that tends to precede a real move -- plus a high quality score (4+),
// since the base/structure itself needs to be strong too. This is
// deliberately narrower than BUY-READY: every big-move setup is
// BUY-READY, but not every BUY-READY is a big-move setup.
function isBigMoveSetup(r) {
  return r.verdict === "BUY-READY" && r.weekly_accumulation === true && r.quality_score !== null && r.quality_score !== undefined && r.quality_score >= 4;
}
