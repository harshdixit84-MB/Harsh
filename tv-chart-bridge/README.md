# tv-chart-bridge

A minimal script (not an MCP server) that reads OHLCV bars, quotes, and
chart screenshots straight out of your own **TradingView Desktop** app,
via Chrome DevTools Protocol (CDP).

Extracted and trimmed down from
[tradesdontlie/tradingview-mcp](https://github.com/tradesdontlie/tradingview-mcp)
(MIT license) — that project is a full MCP server exposing ~84 tools
(Pine Script editing, alerts, replay, strategy tester, drawings, etc.)
built for driving TradingView from an AI agent. This folder keeps only
the three files needed to pull chart data into your own scripts:
`connection.js`, `chart.js`, `capture.js`.

## How it works

TradingView Desktop is an Electron app. Electron/Chromium apps expose a
debugging interface when launched with `--remote-debugging-port=<port>`.
This connects to that port, finds the tab showing your chart, and runs
JS in that page's context to read whatever's already rendered — it does
not call TradingView's servers directly and does not bypass your login
or subscription tier.

## Setup

```bash
npm install
```

Launch TradingView Desktop with the debug port open:

```bash
# macOS
./launch_debug_mac.sh        # port 9222 by default

# Linux
./launch_debug_linux.sh
```

(Windows: TradingView ships as an MSIX package under WindowsApps, which
needs a slightly different launch approach — see the upstream repo's
`SETUP_GUIDE.md` if you need that platform.)

## Usage

```js
import { setSymbol, getOhlcv, getQuote } from './chart.js';
import { captureScreenshot } from './capture.js';

await setSymbol('NSE:RELIANCE');
const { bars } = await getOhlcv(100);      // last 100 OHLCV bars
const quote = await getQuote();             // latest price/volume
await captureScreenshot({ region: 'chart' }); // saves to ./screenshots/
```

See `example.js` for a small watchlist loop — the shape you'd plug into
your NSE VCP dashboard's Python jobs (call this from Node, write results
to the same Google Sheets DB, or just use it standalone for the chart
image + target/stoploss overlay project).

## Security notes — read before wiring this into anything scheduled

- The debug port has **no authentication**. Anything running on your
  machine that can reach `127.0.0.1:9222` can drive your TradingView
  session — read your charts, watchlists, and (if you extend this)
  place drawings or alerts. Don't bind it to `0.0.0.0` or expose it
  over a network; keep it on localhost only.
- Don't run this against a machine you don't control, and don't leave
  the debug port open on a shared or public machine.
- This reads data already visible in your logged-in session — it's
  bound by whatever your TradingView subscription tier already shows
  you (delayed data stays delayed, paywalled symbols stay paywalled).
- If you ever automate this on a schedule (cron, GitHub Actions
  self-hosted runner, etc.), make sure the debug port isn't reachable
  from outside that machine.

## Attribution

Core CDP-bridge logic and known in-page API paths (`connection.js`,
the `getOhlcv`/`getQuote`/`setSymbol` implementations, the screenshot
capture approach) are adapted from tradesdontlie/tradingview-mcp,
MIT-licensed. See that repo for the full tool surface (Pine Script
dev, alerts, replay, strategy tester, drawings) if you need more than
read-only chart access.
