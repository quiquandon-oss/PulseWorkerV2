// Observability volume reduction (2026-11): every diagnostic log payload is capped at 512 bytes and no raw
// console.log remains in the Worker entry point or the event-detection logger.
import { describe, it, expect, vi, afterEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { boundedJson, logBounded, LOG_MAX_BYTES } from '../observability/bounded-log.js';

const bytes = (s) => new TextEncoder().encode(s).length;
afterEach(() => vi.restoreAllMocks());

describe('boundedJson', () => {
  it('passes small payloads through unchanged', () => {
    const o = { evt: 'chain_start', coin: 'BTC', horizon: 24, ts: 1 };
    expect(boundedJson(o)).toBe(JSON.stringify(o));
  });
  it('caps oversized payloads at 512 bytes, keeps the event name and stays valid JSON', () => {
    for (const filler of ['x', 'é', '😀', '"\\\n']) {
      const line = boundedJson({ evt: 'd1_usage', blob: filler.repeat(5000) });
      expect(bytes(line)).toBeLessThanOrEqual(LOG_MAX_BYTES);
      const parsed = JSON.parse(line);
      expect(parsed).toMatchObject({ evt: 'd1_usage', truncated: true });
      expect(parsed.bytes).toBeGreaterThan(LOG_MAX_BYTES);
    }
  });
  it('honours a smaller limit and never throws on unserialisable input', () => {
    expect(bytes(boundedJson({ evt: 'a', s: 'y'.repeat(900) }, 100))).toBeLessThanOrEqual(100);
    const cyc = {}; cyc.self = cyc;
    expect(JSON.parse(boundedJson(cyc))).toEqual({ evt: 'log_unserializable' });
  });
});

describe('logBounded', () => {
  it('logs prefix + payload within 512 bytes at the requested level', () => {
    const spy = vi.spyOn(console, 'warn').mockImplementation(() => {});
    logBounded('warn', 'sentiment v2 (cron) DEFERRED (D1 budget):', { ok: true, deferred_readings: Array(400).fill('2026-11-20T00:00:00Z') });
    const [pre, body] = spy.mock.calls[0];
    expect(bytes(pre) + 1 + bytes(body)).toBeLessThanOrEqual(LOG_MAX_BYTES);
    expect(JSON.parse(body).truncated).toBe(true);
  });
  it('logs a single argument when there is no prefix, and never throws', () => {
    const spy = vi.spyOn(console, 'log').mockImplementation(() => { throw new Error('sink down'); });
    expect(() => logBounded('log', '', { evt: 'x' })).not.toThrow();
    expect(spy.mock.calls[0]).toEqual(['{"evt":"x"}']);
  });
});

describe('static: no unbounded console.log in the Worker', () => {
  for (const f of ['worker.js', 'events/event-core.js']) {
    it(`${f} has no console.log( call and no console.*(JSON.stringify( line`, () => {
      const src = readFileSync(new URL(`../${f}`, import.meta.url), 'utf8');
      expect(src).not.toMatch(/console\.log\(/);
      expect(src).not.toMatch(/console\.(error|warn)\(JSON\.stringify\(/);
    });
  }
});
