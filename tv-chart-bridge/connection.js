/**
 * connection.js
 * ----------------------------------------------------------------------
 * Minimal Chrome DevTools Protocol (CDP) bridge to a locally running
 * TradingView Desktop app.
 *
 * Extracted and trimmed from tradesdontlie/tradingview-mcp
 * (https://github.com/tradesdontlie/tradingview-mcp), MIT-licensed.
 * This file keeps only the connection plumbing — no MCP server, no
 * pine-script tooling, no alert/replay/strategy-tester code.
 *
 * HOW IT WORKS
 * TradingView Desktop is an Electron (Chromium) app. Chromium apps expose
 * a debugging interface when launched with --remote-debugging-port=<port>.
 * This module attaches to that port, finds the tab/window showing a
 * TradingView chart, and lets you run JS inside it (Runtime.evaluate) to
 * read chart state or take screenshots. It does NOT talk to TradingView's
 * servers directly, and does NOT bypass login/paywall — it just reads
 * whatever is already rendered in your own logged-in desktop app.
 *
 * REQUIREMENTS
 * - TradingView Desktop must be running with the debug port enabled.
 *   Mac/Linux:  open the app with --remote-debugging-port=9222 appended
 *               to the launch command (see launch_debug.sh below).
 *   Windows:    TradingView ships as an MSIX app; see the upstream repo's
 *               SETUP_GUIDE.md for the WindowsApps launch quirks.
 * - npm install chrome-remote-interface
 */
import CDP from 'chrome-remote-interface';

let client = null;
let targetInfo = null;

// Override with env vars if your setup needs a different host/port.
export const CDP_HOST = process.env.TV_CDP_HOST || '127.0.0.1';
export const CDP_PORT = Number(process.env.TV_CDP_PORT) || 9222;

const MAX_RETRIES = 5;
const BASE_DELAY = 500;

// Known in-page object paths (discovered via live probing of the TV app).
export const KNOWN_PATHS = {
  chartApi: 'window.TradingViewApi._activeChartWidgetWV.value()',
  mainSeriesBars: 'window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().mainSeries().bars()',
};

/** Safely embed a string into JS evaluated over CDP (prevents injection). */
export function safeString(str) {
  return JSON.stringify(String(str));
}

export async function getClient() {
  if (client) {
    try {
      await client.Runtime.evaluate({ expression: '1', returnByValue: true });
      return client;
    } catch {
      client = null;
      targetInfo = null;
    }
  }
  return connect();
}

export async function connect() {
  let lastError;
  for (let attempt = 0; attempt < MAX_RETRIES; attempt++) {
    try {
      const target = await findChartTarget();
      if (!target) {
        throw new Error('No TradingView chart target found. Is TradingView Desktop open, on a chart, with the debug port enabled?');
      }
      targetInfo = target;
      client = await CDP({ host: CDP_HOST, port: CDP_PORT, target: target.id });
      await client.Runtime.enable();
      await client.Page.enable();
      return client;
    } catch (err) {
      lastError = err;
      const delay = Math.min(BASE_DELAY * Math.pow(2, attempt), 30000);
      await new Promise((r) => setTimeout(r, delay));
    }
  }
  throw new Error(`CDP connection failed after ${MAX_RETRIES} attempts: ${lastError?.message}`);
}

async function findChartTarget() {
  const resp = await fetch(`http://${CDP_HOST}:${CDP_PORT}/json/list`);
  const targets = await resp.json();
  return (
    targets.find((t) => t.type === 'page' && /tradingview\.com\/chart/i.test(t.url)) ||
    targets.find((t) => t.type === 'page' && /tradingview/i.test(t.url)) ||
    null
  );
}

export async function evaluate(expression, opts = {}) {
  const c = await getClient();
  const result = await c.Runtime.evaluate({
    expression,
    returnByValue: true,
    awaitPromise: opts.awaitPromise ?? false,
    ...opts,
  });
  if (result.exceptionDetails) {
    const msg = result.exceptionDetails.exception?.description || result.exceptionDetails.text || 'Unknown evaluation error';
    throw new Error(`JS evaluation error: ${msg}`);
  }
  return result.result?.value;
}

export async function evaluateAsync(expression) {
  return evaluate(expression, { awaitPromise: true });
}

export async function disconnect() {
  if (client) {
    try { await client.close(); } catch {}
    client = null;
    targetInfo = null;
  }
}
