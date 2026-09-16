/*
 * Shared "Short Term Long" badge -- one copy instead of duplicating the
 * markup/styling across index.html, uptrend/downtrend/sideways.html, and
 * pl.html. Backed by ema_signals.py's check_short_term_long(): a recent
 * (last 10 trading days) 20/50 EMA bullish crossover, a full bullish EMA
 * stack (20 and 50 both above 100, 100 above 200), and today's close
 * sitting 0.5%-1% above EMA50.
 *
 * Usage: renderStLongBadge(signal, daysSinceCross, pctAboveEma50)
 * -- returns an HTML string, or "" if signal is falsy. Include this
 * wherever a symbol is rendered, alongside the Analyze button.
 */

(function injectBadgeStyles() {
  const style = document.createElement("style");
  style.id = "badge-utils-styles";
  style.textContent = `
    .st-long-badge {
      display: inline-block;
      background: #dcfce7;
      color: #166534;
      border: 1px solid #86efac;
      border-radius: 12px;
      padding: 2px 8px;
      font-size: 10px;
      font-weight: 700;
      white-space: nowrap;
    }
  `;
  document.head.appendChild(style);
})();

function renderStLongBadge(signal, daysSinceCross, pctAboveEma50) {
  if (!signal) return "";
  const detail = (daysSinceCross !== null && daysSinceCross !== undefined && daysSinceCross !== "")
    ? ` title="20/50 EMA crossed ${daysSinceCross}d ago, ${pctAboveEma50}% above EMA50"`
    : "";
  return `<span class="st-long-badge"${detail}>Short term long ( 10%)</span>`;
}
