// Drives the real CryptoPulseV2 index.html in Chromium against a local PulseWorkerV2 world (world-server.mjs).
// Requests to pulseworker-v2.quiquandon.workers.dev go to the local world; the page's CDN scripts are served from
// tests/e2e/cdn-cache/ (download them once, see README); every other host is blocked. Prints a JSON summary.
import { createRequire } from 'node:module';
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const [,, port, html, outDir, tag = ''] = process.argv;
mkdirSync(outDir, { recursive: true });
const H = 3600 * 1000;
const NOW = Date.UTC(2026, 10, 1, 0, 0, 0) - 2 * H;
const WORLD = `http://127.0.0.1:${port}`;
const workerCalls = []; const blocked = []; const consoleErrors = []; const pageErrors = [];
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 1800 } });
await page.addInitScript((now) => { const D = Date; const off = now - D.now(); globalThis.Date = class extends D { constructor(...a) { super(...(a.length ? a : [D.now() + off])); } static now() { return D.now() + off; } }; }, NOW);
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text()); });
page.on('pageerror', (e) => pageErrors.push(String(e)));
const cacheDir = process.env.CDN_CACHE_DIR || new URL('./cdn-cache/', import.meta.url).pathname; mkdirSync(cacheDir, { recursive: true });
await page.route('**/*', async (route) => {
  const url = new URL(route.request().url());
  if (url.host === 'dashboard.local') return route.fulfill({ status: 200, contentType: 'text/html', body: readFileSync(html, 'utf8') });
  if (url.host === 'pulseworker-v2.quiquandon.workers.dev') {
    const res = await fetch(WORLD + url.pathname + url.search, { method: route.request().method() });
    const body = await res.text();
    workerCalls.push({ path: url.pathname + url.search, status: res.status, served_from: (() => { try { return JSON.parse(body).served_from || null; } catch { return null; } })() });
    return route.fulfill({ status: res.status, headers: Object.fromEntries([...res.headers.entries()].filter(([k]) => !['content-length', 'transfer-encoding', 'connection', 'keep-alive', 'date'].includes(k))), body });
  }
  if (url.host === 'cdn.jsdelivr.net' || url.host === 'cdn.tailwindcss.com') {
    const f = cacheDir + encodeURIComponent(url.href.replace(/\/$/, ''));
    if (!existsSync(f)) { blocked.push(url.href + ' (not cached)'); return route.abort(); }
    return route.fulfill({ status: 200, contentType: 'application/javascript', body: readFileSync(f) });
  }
  blocked.push(url.host); return route.abort();
});
await page.goto('http://dashboard.local/');
await page.waitForFunction(() => { const e = document.getElementById('predPUp'); return e && /\d+%/.test(e.textContent); }, null, { timeout: 30000 });
await page.waitForTimeout(2500);
const read = async (id) => page.evaluate((i) => { const e = document.getElementById(i); return e ? e.textContent.trim() : null; }, id);
const snap = await (await fetch(WORLD + '/__snapshot')).json();
const result = {
  headline: await read('predPUp'), asOf: await read('predAsOf'), variantLabel: await read('predVariantLabel'), title: await read('predictionTitle'),
  snapshot: { p_up: snap.p_up, chosen_variant: snap.selection.chosen_variant, chosen_p_up: snap.selection.chosen_p_up, cycle_p_up: snap.selection.cycle_p_up },
  worker_calls: workerCalls.length, distinct_paths: [...new Set(workerCalls.map((c) => c.path.split('?')[0]))].length,
  predict_calls: workerCalls.filter((c) => /predict\?/.test(c.path)).map((c) => `${c.path} -> ${c.served_from}`),
  non_200: workerCalls.filter((c) => c.status !== 200), blocked_hosts: [...new Set(blocked)], console_errors: consoleErrors.slice(0, 5), page_errors: pageErrors.slice(0, 5),
};
await page.screenshot({ path: `${outDir}/dashboard${tag}.png`, fullPage: false });
await browser.close();
process.stdout.write(JSON.stringify(result, null, 1) + '\n');
