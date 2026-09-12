/**
 * example.js — sample usage for a swing-trade watchlist scan.
 *
 * Run: node example.js
 * (TradingView Desktop must already be open with the debug port on —
 *  run ./launch_debug_mac.sh or ./launch_debug_linux.sh first.)
 */
import { setSymbol, getOhlcv, getQuote, getState } from './chart.js';
import { captureScreenshot } from './capture.js';
import { disconnect } from './connection.js';

const WATCHLIST = ['NSE:RELIANCE', 'NSE:TCS', 'NSE:INFY'];

async function main() {
  for (const symbol of WATCHLIST) {
    console.log(`\n--- ${symbol} ---`);
    await setSymbol(symbol);

    const state = await getState();
    console.log('chart state:', state.symbol, state.resolution);

    const quote = await getQuote();
    console.log('latest close:', quote.close, 'volume:', quote.volume);

    const { bars } = await getOhlcv(60); // last 60 bars for pattern checks
    console.log(`pulled ${bars.length} bars`);

    const shot = await captureScreenshot({ region: 'chart', filename: symbol.replace(':', '_') });
    console.log('screenshot saved to', shot.file_path);
  }

  await disconnect();
}

main().catch((err) => {
  console.error('Error:', err.message);
  process.exit(1);
});
