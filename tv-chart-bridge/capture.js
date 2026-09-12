/**
 * capture.js
 * ----------------------------------------------------------------------
 * Screenshot the TradingView chart via CDP's native Page.captureScreenshot.
 * Adapted from tradesdontlie/tradingview-mcp src/core/capture.js.
 */
import { getClient, evaluate } from './connection.js';
import { writeFileSync, mkdirSync } from 'fs';
import { join } from 'path';

const SCREENSHOT_DIR = join(process.cwd(), 'screenshots');

/**
 * region: 'full' (whole window) or 'chart' (just the chart canvas).
 * filename: without extension; defaults to a timestamped name.
 * Returns the saved file path.
 */
export async function captureScreenshot({ region = 'full', filename } = {}) {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });

  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const fname = (filename || `tv_${region}_${ts}`).replace(/[\/\\]/g, '_');
  const filePath = join(SCREENSHOT_DIR, `${fname}.png`);

  const client = await getClient();
  let clip;

  if (region === 'chart') {
    const bounds = await evaluate(`
      (function() {
        var el = document.querySelector('[data-name="pane-canvas"]') || document.querySelector('canvas');
        if (!el) return null;
        var r = el.getBoundingClientRect();
        return { x: r.x, y: r.y, width: r.width, height: r.height };
      })()
    `);
    if (bounds) clip = { ...bounds, scale: 1 };
  }

  const params = { format: 'png' };
  if (clip) params.clip = clip;

  const { data } = await client.Page.captureScreenshot(params);
  writeFileSync(filePath, Buffer.from(data, 'base64'));

  return { success: true, file_path: filePath, size_bytes: Buffer.from(data, 'base64').length };
}
