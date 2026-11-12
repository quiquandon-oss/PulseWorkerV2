import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const WORKER_SRC = readFileSync(join(__dirname, '..', 'worker.js'), 'utf8');

// Structural tests, same philosophy as the existing "Immutability" suite in
// learning-engine.test.js: extract the real route handler's source and
// assert on what it actually does, rather than re-implementing routing
// logic here to test against. This is what proves H1 (the selection
// coverage gap) is actually fixed in the shipped file, not just described
// as fixed in a commit message.
//
// Each route block is isolated by finding its `if (url.pathname === ...)`
// start and matching braces to its end, mirroring extract.js's own
// brace-matching approach for function bodies.
function extractRouteBlock(pathname) {
  const marker = `url.pathname === '${pathname}'`;
  const markerIdx = WORKER_SRC.indexOf(marker);
  if (markerIdx === -1) throw new Error(`Could not find route ${pathname} in worker.js`);
  const braceStart = WORKER_SRC.indexOf('{', WORKER_SRC.indexOf(')', markerIdx));
  let depth = 0, i = braceStart;
  for (; i < WORKER_SRC.length; i++) {
    if (WORKER_SRC[i] === '{') depth++;
    else if (WORKER_SRC[i] === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return WORKER_SRC.slice(markerIdx, i);
}

// 2026-11 update. The routes used to call selectBestVariant directly, which inserted a
// selection_decisions row on every dashboard load, including loads that persisted no new
// prediction (measured: 277 of 2,093 cycles with duplicate rows). They now go through
// selectionForServedPrediction, which keeps the H1 guarantee -- every prediction a GET
// persists is paired with a selection -- and otherwise returns the stored decision.
// Behavioural coverage is in tests/served-predictions.test.js.
describe('Selection coverage (H1) — /predict, /link-predict, /eth-predict pair a persisted prediction with a selection', () => {
  const ROUTES = [['/predict', 'predictAndLog(', 'BTC'], ['/link-predict', 'linkPredictAndLog(', 'LINK'], ['/eth-predict', 'ethPredictAndLog(', 'ETH']];

  for (const [pathname, predictCall, coin] of ROUTES) {
    it(`${pathname} resolves the selection for ${coin} after the prediction`, () => {
      const block = extractRouteBlock(pathname);
      const predictIdx = block.indexOf(predictCall);
      const selectIdx = block.indexOf('selectionForServedPrediction(');
      expect(predictIdx).toBeGreaterThan(-1);
      expect(selectIdx).toBeGreaterThan(predictIdx); // selection happens AFTER prediction, not before
      expect(block).toMatch(new RegExp(`selectionForServedPrediction\\(env,\\s*'${coin}',\\s*horizon,\\s*result\\)`));
    });

    it(`${pathname} no longer inserts a selection row unconditionally`, () => {
      expect(extractRouteBlock(pathname)).not.toContain('selectBestVariant(');
    });
  }

  it('selectionForServedPrediction runs selectBestVariant exactly when the prediction was persisted', () => {
    const start = WORKER_SRC.indexOf('async function selectionForServedPrediction(');
    expect(start).toBeGreaterThan(-1);
    const body = WORKER_SRC.slice(start, WORKER_SRC.indexOf('\n}\n', start));
    expect(body).toMatch(/if \(predictResult && predictResult\.persisted === true\) \{\s*const selection = await selectBestVariant\(env, coin, horizon\);[\s\S]*?return selection;/);
    expect(body).toContain('getLatestSelection(env, coin, horizon)');
  });

  it('each route wraps the selection call in its own try/catch, so a selection failure cannot break the prediction response', () => {
    for (const [pathname] of ROUTES) {
      const block = extractRouteBlock(pathname);
      expect(block).toMatch(/try\s*\{\s*result\.selection\s*=\s*await selectionForServedPrediction/);
      expect(block).toMatch(/catch\s*\(selErr\)\s*\{\s*result\.selection\s*=\s*\{\s*ok:\s*false/);
    }
  });

  it('the outer route try/catch (500 on failure) still wraps everything', () => {
    for (const [pathname] of ROUTES) {
      expect(extractRouteBlock(pathname)).toContain('status: 500');
    }
  });
});
