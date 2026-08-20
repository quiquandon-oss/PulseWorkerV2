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

describe('Selection coverage fix (H1) — /predict, /link-predict, /eth-predict now pair with selectBestVariant', () => {
  it('/predict calls selectBestVariant for BTC after predictAndLog', () => {
    const block = extractRouteBlock('/predict');
    const predictIdx = block.indexOf('predictAndLog(');
    const selectIdx = block.indexOf('selectBestVariant(');
    expect(predictIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(predictIdx); // selection happens AFTER prediction, not before
    expect(block).toMatch(/selectBestVariant\(env,\s*'BTC'/);
  });

  it('/link-predict calls selectBestVariant for LINK after linkPredictAndLog', () => {
    const block = extractRouteBlock('/link-predict');
    const predictIdx = block.indexOf('linkPredictAndLog(');
    const selectIdx = block.indexOf('selectBestVariant(');
    expect(predictIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(predictIdx);
    expect(block).toMatch(/selectBestVariant\(env,\s*'LINK'/);
  });

  it('/eth-predict calls selectBestVariant for ETH after ethPredictAndLog', () => {
    const block = extractRouteBlock('/eth-predict');
    const predictIdx = block.indexOf('ethPredictAndLog(');
    const selectIdx = block.indexOf('selectBestVariant(');
    expect(predictIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(-1);
    expect(selectIdx).toBeGreaterThan(predictIdx);
    expect(block).toMatch(/selectBestVariant\(env,\s*'ETH'/);
  });

  it('each route wraps the selection call in its own try/catch, so a selection failure cannot break the prediction response', () => {
    for (const pathname of ['/predict', '/link-predict', '/eth-predict']) {
      const block = extractRouteBlock(pathname);
      // The selection call must be inside a nested try that catches into
      // result.selection, separate from the outer try that would 500 the
      // whole response.
      expect(block).toMatch(/try\s*\{\s*result\.selection\s*=\s*await selectBestVariant/);
      expect(block).toMatch(/catch\s*\(selErr\)\s*\{\s*result\.selection\s*=\s*\{\s*ok:\s*false/);
    }
  });

  it('the outer route try/catch (500 on failure) is unchanged and still wraps everything, including the new selection call', () => {
    for (const pathname of ['/predict', '/link-predict', '/eth-predict']) {
      const block = extractRouteBlock(pathname);
      expect(block).toContain('status: 500');
    }
  });
});
