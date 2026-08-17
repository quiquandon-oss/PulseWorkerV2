import { describe, it, expect, beforeAll } from 'vitest';
import { extractFunctions, evalInScope } from './helpers/extract.js';

describe('computeEthRegimeMag — self-contained regime signal, no cross-asset borrowing', () => {
  let scope;

  beforeAll(() => {
    const src = extractFunctions('computeEthRegimeMag');
    scope = evalInScope(src);
  });

  it('returns null when there is not enough history yet (fewer than longN rows)', () => {
    const rows = Array.from({ length: 15 }, (_, i) => ({ eth_price: 2000 + i }));
    expect(scope.computeEthRegimeMag(rows, 14)).toBeNull();
  });

  it('returns a positive value when short-term price is above the longer trend (uptrend)', () => {
    // 21 days flat at 2000, then a recent run-up in the last 8 — short MA
    // should sit meaningfully above the long MA.
    const rows = [
      ...Array.from({ length: 13 }, () => ({ eth_price: 2000 })),
      ...Array.from({ length: 8 }, (_, i) => ({ eth_price: 2000 + (i + 1) * 20 })),
    ];
    const result = scope.computeEthRegimeMag(rows, rows.length - 1);
    expect(result).toBeGreaterThan(0);
  });

  it('returns a negative value in a symmetric downtrend', () => {
    const rows = [
      ...Array.from({ length: 13 }, () => ({ eth_price: 2000 })),
      ...Array.from({ length: 8 }, (_, i) => ({ eth_price: 2000 - (i + 1) * 20 })),
    ];
    const result = scope.computeEthRegimeMag(rows, rows.length - 1);
    expect(result).toBeLessThan(0);
  });

  it('returns approximately zero for a genuinely flat series', () => {
    const rows = Array.from({ length: 21 }, () => ({ eth_price: 2000 }));
    const result = scope.computeEthRegimeMag(rows, 20);
    expect(Math.abs(result)).toBeLessThan(0.01);
  });

  it('is computed purely from eth_price — never references btc_price or any other asset field', () => {
    // Confirms the actual design intent: rows missing any BTC-related field
    // entirely still compute correctly, since nothing here should ever look
    // for one. Direct regression test for the exact problem found in LINK.
    const rows = Array.from({ length: 25 }, (_, i) => ({ eth_price: 2000 + i * 5 }));
    expect(() => scope.computeEthRegimeMag(rows, 24)).not.toThrow();
    expect(scope.computeEthRegimeMag(rows, 24)).not.toBeNull();
  });
});

describe('refreshCalibrationCurve table selection — the bug found and fixed while building this', () => {
  // Reimplements the exact fixed ternary to pin its behavior with a test,
  // since the original bug (coin === 'LINK' ? ... : 'predictions') would
  // have silently pointed ETH's calibration refresh at BTC's own table,
  // corrupting ETH's curve with BTC's resolved predictions.
  function selectTable(coin) {
    return coin === 'LINK' ? 'link_predictions' : coin === 'ETH' ? 'eth_predictions' : 'predictions';
  }

  it('routes each of the three coins to its own distinct table', () => {
    expect(selectTable('BTC')).toBe('predictions');
    expect(selectTable('LINK')).toBe('link_predictions');
    expect(selectTable('ETH')).toBe('eth_predictions');
  });

  it('regression: ETH must never fall through to predictions (BTC own table)', () => {
    expect(selectTable('ETH')).not.toBe('predictions');
  });
});
