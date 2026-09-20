/*
 * Shared "Analyze" modal widget -- used by index.html, uptrend.html,
 * downtrend.html, sideways.html, and pl.html so this ~150-line block
 * exists in ONE place instead of being copy-pasted across five files.
 *
 * Usage on any page:
 *   <script src="/analyze-widget.js"></script>
 * then, on any button meant to open it:
 *   <button class="analyze-btn" data-symbol="RELIANCE">Analyze</button>
 * with a delegated click listener on that page calling:
 *   openAnalyze(e.target.dataset.symbol)
 * (this file does not auto-wire row buttons itself, since each page's
 * table markup differs -- it only owns the modal itself, once opened.)
 *
 * Nothing here fetches automatically. The modal opens empty with three
 * buttons -- "Get Plan", "Get AI Narrative" and "Get Outlook" -- and none of
 * the Angel SmartAPI-backed plan, the Gemini-backed narrative or the
 * Nifty/sector/stock outlook is called until the person explicitly clicks
 * one of them.
 */

const ANALYZE_API_BASE = "https://nse-stock-chatbot.vercel.app/api";

(function injectAnalyzeWidgetStyles() {
  const style = document.createElement("style");
  style.id = "analyze-widget-styles";
  style.textContent = `
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 1000; align-items: center; justify-content: center; }
    .modal-overlay.open { display: flex; }
    .modal-box { background: var(--surface-1, #ffffff); border: 1px solid var(--border, #cbd5e1); border-radius: 10px; width: min(720px, 92vw); max-height: 82vh; display: flex; flex-direction: column; overflow: hidden; }
    .modal-header { display: flex; align-items: center; justify-content: space-between; padding: 14px 18px; border-bottom: 1px solid var(--border, #2a3352); font-weight: 700; font-size: 15px; }
    .modal-close { background: transparent; border: none; color: inherit; font-size: 16px; cursor: pointer; opacity: 0.7; }
    .modal-close:hover { opacity: 1; }
    .modal-body { padding: 14px 18px; overflow-y: auto; }
    .analyze-btn { background: #dbeafe; color: #1d4ed8; border: none; border-radius: 5px; padding: 4px 9px; font-size: 11px; font-weight: 700; cursor: pointer; }
    .verdict-strategy-row { display: flex; align-items: center; gap: 8px; font-size: 12px; padding: 2px 0; }
    .bs-pill { display: inline-block; padding: 3px 9px; border-radius: 12px; font-size: 11px; font-weight: 700; }
    .bs-pill.buy { background: #d1fae5; color: #065f46; }
    .empty { padding: 10px; color: var(--text-secondary, #666); }
    .loading { padding: 10px; color: var(--text-secondary, #666); }
  `;
  document.head.appendChild(style);
})();

(function injectAnalyzeModalMarkup() {
  if (document.getElementById("analyze-overlay")) return; // already present on the page
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.id = "analyze-overlay";
  overlay.innerHTML = `
    <div class="modal-box">
      <div class="modal-header">
        <span id="analyze-title">Analyze</span>
        <button class="modal-close" id="close-analyze">✕</button>
      </div>
      <div class="modal-body" id="analyze-body">
        <div class="loading">Analyzing… this can take up to 30 seconds.</div>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);

  document.getElementById("close-analyze").addEventListener("click", () => {
    document.getElementById("analyze-overlay").classList.remove("open");
  });
  document.getElementById("analyze-overlay").addEventListener("click", (e) => {
    if (e.target.id === "analyze-overlay") e.target.classList.remove("open");
  });
  document.getElementById("analyze-body").addEventListener("click", (e) => {
    if (e.target.id === "get-plan-btn") {
      fetchPlan(e.target.dataset.symbol, e.target);
    } else if (e.target.id === "get-narrative-btn" || e.target.id === "load-narrative-btn") {
      loadNarrative(e.target.dataset.symbol, e.target);
    } else if (e.target.id === "get-outlook-btn") {
      loadOutlook(e.target.dataset.symbol, e.target);
    }
  });
})();

function renderAnalyzeResult(result) {
  if (result.error) {
    return `<div class="empty">⚠️ ${result.error}</div>`;
  }

  let html = `<div style="margin-bottom:10px;"><b>${result.symbol}</b> — as of ${result.as_of}<br/>Last close: ₹${result.last_close}</div>`;

  if (result.verdict === "BUY") {
    const p = result.trade_plan;
    const bt = result.backtest;
    html += `<div style="margin-bottom:10px;">
      <span class="bs-pill buy">✅ BUY via ${result.strategy}</span><br/><br/>
      Entry: ₹${p.entry_price} · Stop: ₹${p.stop_loss} · Target: ₹${p.target}<br/>
      Risk:Reward = 1:${p.reward_risk_ratio}<br/>
      Backtest: ${bt.win_rate_pct}% win rate over ${bt.signals} signals, profit factor ${bt.profit_factor}
      ${bt.low_sample_warning ? '<br/>⚠️ Small sample size -- treat with caution' : ""}
    </div>`;
  } else if (result.verdict === "BUY_NO_TRACK_RECORD") {
    const p = result.trade_plan;
    html += `<div style="margin-bottom:10px;">
      <span class="bs-pill buy">⚠️ ${result.strategy} setup active (no track record yet)</span><br/><br/>
      Entry: ₹${p.entry_price} · Stop: ₹${p.stop_loss} · Target: ₹${p.target}
    </div>`;
  } else {
    html += `<div style="margin-bottom:10px;">No active setup today.</div>`;
  }

  if (result.momentum_confirmation) {
    html += `<div style="margin-bottom:10px;">Momentum (MACD): ${result.momentum_confirmation.status}</div>`;
  }

  const chart = result.chart_read;
  if (chart && !chart.error) {
    const biasColor = { bullish: "var(--accent-buy, #0d6e38)", bearish: "var(--accent-loss, #b91c1c)", neutral: "var(--text-secondary, #666)" }[chart.todays_candle_bias] || "inherit";
    html += `<div style="margin-bottom:10px; padding:10px; border:1px solid var(--border,#ddd); border-radius:8px;">
      <div style="font-weight:700; margin-bottom:6px;">📊 Chart Read</div>
      <div>Trend: <b>${chart.trend}</b></div>
      ${chart.nearest_support !== null ? `<div>Support: ₹${chart.nearest_support} (${chart.distance_to_support_pct}% below)</div>` : ""}
      ${chart.nearest_resistance !== null ? `<div>Resistance: ₹${chart.nearest_resistance} (${chart.distance_to_resistance_pct}% above)</div>` : ""}
      <div>Today's candle: <span style="color:${biasColor}; font-weight:700;">${chart.todays_candle_pattern}</span> (${chart.todays_candle_bias})</div>
      <div>RSI(14): ${chart.rsi_14} — ${chart.rsi_zone}</div>
      <div>Volume: ${chart.volume_vs_20d_avg}</div>
      <div style="margin-top:6px; font-style:italic;">Reversal watch: ${chart.reversal_watch}</div>
    </div>`;
  }

  if (result.strategy_ranking) {
    html += `<div style="font-size:11px; color:var(--text-secondary); margin-bottom:6px;">Ranking by backtested win rate:</div>`;
    result.strategy_ranking.forEach((r) => {
      const flag = r.active_today ? "🟢" : "⚪";
      const wr = r.win_rate_pct !== null ? `${r.win_rate_pct}%` : "n/a";
      html += `<div class="verdict-strategy-row">${flag} ${r.strategy}: ${wr} (${r.signals} signals)</div>`;
    });
  }

  if (result.narrative) {
    if (result.narrative.error) {
      html += `<div style="margin-top:12px; font-size:12px; color:var(--text-secondary,#666);">🧠 AI Narrative unavailable: ${result.narrative.error}</div>`;
    } else {
      html += `<div style="margin-top:12px; padding:10px; border:1px solid var(--border,#cbd5e1); border-radius:8px; background:var(--surface-2,#f8fafc);">
        <div style="font-weight:700; margin-bottom:6px;">🧠 AI Narrative</div>
        <div style="white-space:pre-wrap; line-height:1.5;">${result.narrative.text}</div>
        <div style="margin-top:6px; font-size:11px; color:var(--text-secondary,#666);">Explains the chart above — the trade numbers stay whatever's shown above, this doesn't change them.</div>
      </div>`;
    }
  } else {
    html += `<div style="margin-top:12px;"><button class="analyze-btn" id="load-narrative-btn" data-symbol="${result.symbol}">🧠 Get AI Narrative</button></div>`;
  }

  html += `<div style="margin-top:8px;"><button class="analyze-btn" id="get-outlook-btn" data-symbol="${result.symbol}">🔭 Get Outlook</button></div>`;

  return html;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Nifty -> sector -> stock outlook (api ?mode=outlook, core/outlook.py in
// the nse-stock-chatbot repo). Rule-based: no Gemini call, no backtest, so
// it is much lighter than "Get Plan" / "Get AI Narrative".
function renderOutlookResult(result) {
  if (result.error) {
    return `<div class="empty">⚠️ ${escapeHtml(result.error)}</div>`;
  }

  const bias = String(result.bias || "");
  const act = (result.action && result.action.label) || "";
  const pillBg = act === "BUY" ? "#d1fae5" : act === "SELL" ? "#fee2e2" : "#fef3c7";
  const pillFg = act === "BUY" ? "#065f46" : act === "SELL" ? "#b91c1c" : "#92400e";

  const marketTxt = result.market && !result.market.error ? `${result.market.label} (${result.market.score >= 0 ? "+" : ""}${result.market.score})` : "n/a";
  const sectorTxt = result.sector ? `${escapeHtml(result.sector.name)}: ${result.sector.label} (${result.sector.score >= 0 ? "+" : ""}${result.sector.score})` : "n/a";
  const st = result.stock || {};
  const stockScore = st.score_with_patterns !== undefined ? st.score_with_patterns : st.score;
  const stockTxt = st.label ? `${st.label} (${stockScore >= 0 ? "+" : ""}${stockScore})` : "n/a";

  let html = `<div style="margin-bottom:10px;"><b>${escapeHtml(result.symbol)}</b> — as of ${escapeHtml(result.as_of)}<br/>Last close: ₹${result.last_close}</div>`;

  html += `<div style="margin-bottom:10px;">
    <span class="bs-pill" style="background:${pillBg}; color:${pillFg}; font-size:14px; padding:4px 14px;">${escapeHtml(act || "n/a")}</span>
    <span style="font-size:12px; margin-left:8px;">${escapeHtml(bias)} · score ${result.score >= 0 ? "+" : ""}${result.score} / 100</span>
  </div>`;

  html += `<div style="margin-bottom:10px; padding:10px; border:1px solid var(--border,#ddd); border-radius:8px; white-space:pre-wrap; line-height:1.5;">${escapeHtml(result.summary || "")}</div>`;

  html += `<div style="font-size:12px; margin-bottom:6px;">Market: <b>${marketTxt}</b> · Sector: <b>${sectorTxt}</b> · Stock: <b>${stockTxt}</b></div>`;

  if (result.data_notes && result.data_notes.length) {
    html += `<div style="font-size:11px; color:var(--text-secondary,#666); margin-top:6px;">${result.data_notes.map(escapeHtml).join("<br/>")}</div>`;
  }
  if (result.disclaimer) {
    html += `<div style="font-size:11px; color:var(--text-secondary,#666); margin-top:8px; font-style:italic;">${escapeHtml(result.disclaimer)}</div>`;
  }

  html += `<div style="margin-top:12px; display:flex; gap:10px; flex-wrap:wrap;">
    <button class="analyze-btn" id="get-plan-btn" data-symbol="${escapeHtml(result.symbol)}">📊 Get Plan</button>
    <button class="analyze-btn" id="get-narrative-btn" data-symbol="${escapeHtml(result.symbol)}">🧠 Get AI Narrative</button>
  </div>`;

  return html;
}

function openAnalyze(symbol) {
  // No automatic fetch on open -- neither the plan nor the narrative call
  // Angel/the LLM until the person explicitly clicks one of these buttons.
  // This keeps Angel SmartAPI load fully on-demand (see daily-scan.yml,
  // which was disabled for the same shared-rate-limit reason).
  document.getElementById("analyze-title").textContent = `Analyze — ${symbol}`;
  document.getElementById("analyze-body").innerHTML = `
    <div style="margin-bottom:14px;">Choose what to load for <b>${symbol}</b>:</div>
    <div style="display:flex; gap:10px; flex-wrap:wrap;">
      <button class="analyze-btn" id="get-plan-btn" data-symbol="${symbol}">📊 Get Plan</button>
      <button class="analyze-btn" id="get-narrative-btn" data-symbol="${symbol}">🧠 Get AI Narrative</button>
      <button class="analyze-btn" id="get-outlook-btn" data-symbol="${symbol}">🔭 Get Outlook</button>
    </div>
  `;
  document.getElementById("analyze-overlay").classList.add("open");
}

async function fetchPlan(symbol, btn) {
  btn.disabled = true;
  btn.textContent = "Loading… up to 30s";
  try {
    const res = await fetch(`${ANALYZE_API_BASE}?symbol=${encodeURIComponent(symbol)}`);
    const result = await res.json();
    document.getElementById("analyze-body").innerHTML = renderAnalyzeResult(result);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "📊 Get Plan";
    alert("Could not fetch plan: " + err.message);
  }
}

async function loadNarrative(symbol, btn) {
  // Separate, opt-in call (adds &narrative=true) -- only fires when this
  // explicit button is clicked, whether that's from the initial screen
  // or after a plan has already been loaded.
  btn.disabled = true;
  btn.textContent = "Generating… up to 30s more";
  try {
    const res = await fetch(`${ANALYZE_API_BASE}?symbol=${encodeURIComponent(symbol)}&narrative=true`);
    const result = await res.json();
    document.getElementById("analyze-body").innerHTML = renderAnalyzeResult(result);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "🧠 Get AI Narrative";
    alert("Could not generate narrative: " + err.message);
  }
}

// ---- Outlook client (shared by the Analyze modal and the dashboard's
// Buy/Hold/Sell badges). Results are cached in localStorage for 3 hours and
// concurrent requests for the same symbol are merged, so each ticker costs
// the Angel account at most one outlook call per 3 hours. ----
const OUTLOOK_CACHE_PREFIX = "outlook-v2:";
const OUTLOOK_CACHE_TTL_MS = 3 * 60 * 60 * 1000;
const outlookInflight = {};

function readOutlookCache(symbol) {
  try {
    const raw = localStorage.getItem(OUTLOOK_CACHE_PREFIX + symbol);
    if (!raw) return null;
    const entry = JSON.parse(raw);
    if (!entry || Date.now() - entry.t > OUTLOOK_CACHE_TTL_MS) return null;
    return entry.r;
  } catch (e) {
    return null;
  }
}

function writeOutlookCache(symbol, result) {
  try {
    localStorage.setItem(OUTLOOK_CACHE_PREFIX + symbol, JSON.stringify({ t: Date.now(), r: result }));
  } catch (e) { /* storage full or blocked: caching is optional */ }
}

function fetchOutlook(symbol, force) {
  symbol = String(symbol).toUpperCase();
  if (!force) {
    const cached = readOutlookCache(symbol);
    if (cached) return Promise.resolve(cached);
  }
  if (outlookInflight[symbol]) return outlookInflight[symbol];
  const url = `${ANALYZE_API_BASE}?symbol=${encodeURIComponent(symbol)}&mode=outlook${force ? "&fresh=1" : ""}`;
  outlookInflight[symbol] = fetch(url)
    .then((res) => res.json())
    .then((result) => {
      if (result && !result.error) writeOutlookCache(symbol, result);
      return result;
    })
    .finally(() => { delete outlookInflight[symbol]; });
  return outlookInflight[symbol];
}

async function loadOutlook(symbol, btn) {
  // Opt-in button inside the Analyze modal: only fires on click.
  btn.disabled = true;
  btn.textContent = "Loading… up to 30s";
  try {
    const result = await fetchOutlook(symbol);
    document.getElementById("analyze-body").innerHTML = renderOutlookResult(result);
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "🔭 Get Outlook";
    alert("Could not load outlook: " + err.message);
  }
}

// Opens the popup straight on the outlook summary (used by the dashboard's
// BUY / HOLD / SELL badges). Shows the cached result instantly if there is one.
async function openOutlook(symbol) {
  symbol = String(symbol).toUpperCase();
  document.getElementById("analyze-title").textContent = `Outlook — ${symbol}`;
  const body = document.getElementById("analyze-body");
  document.getElementById("analyze-overlay").classList.add("open");
  const cached = readOutlookCache(symbol);
  if (cached) {
    body.innerHTML = renderOutlookResult(cached);
    return;
  }
  body.innerHTML = `<div class="loading">Loading outlook for ${escapeHtml(symbol)}… up to 30 seconds.</div>`;
  try {
    body.innerHTML = renderOutlookResult(await fetchOutlook(symbol));
  } catch (err) {
    body.innerHTML = `<div class="empty">⚠️ Could not load outlook: ${escapeHtml(err.message)}</div>`;
  }
}
