/**
 * chart.js
 * ----------------------------------------------------------------------
 * Read and navigate the chart currently open in TradingView Desktop.
 * Built on top of connection.js. Adapted from tradesdontlie/tradingview-mcp
 * (src/core/chart.js and src/core/data.js), trimmed to what a
 * personal screening/pattern-tracking script actually needs.
 */
import { evaluate, evaluateAsync, safeString, KNOWN_PATHS } from './connection.js';

const CHART_API = KNOWN_PATHS.chartApi;
const BARS_PATH = KNOWN_PATHS.mainSeriesBars;

/** Current symbol, resolution (timeframe), chart type, and active studies. */
export async function getState() {
  const state = await evaluate(`
    (function() {
      var chart = ${CHART_API};
      var studies = [];
      try {
        studies = chart.getAllStudies().map(function(s) {
          return { id: s.id, name: s.name || s.title || 'unknown' };
        });
      } catch (e) {}
      return {
        symbol: chart.symbol(),
        resolution: chart.resolution(),
        chartType: chart.chartType(),
        studies: studies,
      };
    })()
  `);
  return { success: true, ...state };
}

/** Switch the chart to a different symbol, e.g. "NSE:RELIANCE". */
export async function setSymbol(symbol) {
  await evaluateAsync(`
    (function() {
      var chart = ${CHART_API};
      return new Promise(function(resolve) {
        chart.setSymbol(${safeString(symbol)}, {});
        setTimeout(resolve, 800);
      });
    })()
  `);
  return { success: true, symbol };
}

/** Change timeframe, e.g. "1", "5", "60", "D", "W". */
export async function setTimeframe(timeframe) {
  await evaluate(`
    (function() {
      var chart = ${CHART_API};
      chart.setResolution(${safeString(timeframe)}, {});
    })()
  `);
  return { success: true, timeframe };
}

/**
 * Pull OHLCV bars directly from the chart's in-memory series.
 * count: how many trailing bars to return (max 500).
 */
export async function getOhlcv(count = 100) {
  const limit = Math.min(count, 500);
  const data = await evaluate(`
    (function() {
      var bars = ${BARS_PATH};
      if (!bars || typeof bars.lastIndex !== 'function') return null;
      var result = [];
      var end = bars.lastIndex();
      var start = Math.max(bars.firstIndex(), end - ${limit} + 1);
      for (var i = start; i <= end; i++) {
        var v = bars.valueAt(i);
        if (v) result.push({ time: v[0], open: v[1], high: v[2], low: v[3], close: v[4], volume: v[5] || 0 });
      }
      return { bars: result, total_bars: bars.size() };
    })()
  `);
  if (!data || !data.bars || data.bars.length === 0) {
    throw new Error('Could not read OHLCV data — chart may still be loading.');
  }
  return { success: true, bar_count: data.bars.length, total_available: data.total_bars, bars: data.bars };
}

/** Latest quote (last price, OHLC, volume) for the current or a given symbol. */
export async function getQuote(symbol) {
  const requested = (symbol || '').trim();
  let originalSymbol = null;
  let switched = false;

  if (requested) {
    originalSymbol = await evaluate(`${CHART_API}.symbol()`);
    const bare = (s) => (s || '').split(':').pop().toUpperCase();
    if (bare(originalSymbol) !== bare(requested)) {
      await setSymbol(requested);
      switched = true;
    }
  }

  try {
    const data = await evaluate(`
      (function() {
        var api = ${CHART_API};
        var sym = api.symbol();
        var bars = ${BARS_PATH};
        var quote = { symbol: sym };
        if (bars && typeof bars.lastIndex === 'function') {
          var last = bars.valueAt(bars.lastIndex());
          if (last) {
            quote.time = last[0]; quote.open = last[1]; quote.high = last[2];
            quote.low = last[3]; quote.close = last[4]; quote.volume = last[5] || 0;
          }
        }
        return quote;
      })()
    `);
    return { success: true, ...data };
  } finally {
    if (switched && originalSymbol) await setSymbol(originalSymbol);
  }
}
