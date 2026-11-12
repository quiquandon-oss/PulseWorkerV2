// CI guard against new unbounded D1 reads.
//
// Every SELECT in worker.js, events/ and learning/ that can read a whole table (no LIMIT, no selective
// key equality, no timestamp range) is listed in tests/query-guard/unbounded-baseline.json with its
// occurrence count. The baseline records the state reviewed on 2026-11-12. This test fails when:
//   - a new unbounded statement appears, or an existing one is used more often;
// and asks for the baseline to be shrunk when one disappears, so the list only ever gets shorter.
// To accept a new unbounded read deliberately, regenerate the baseline
// (node tests/query-guard/scan.mjs --write-baseline .) and justify it in review.
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { scan, unboundedCounts, isBounded } from './query-guard/scan.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BASELINE = JSON.parse(readFileSync(join(ROOT, 'tests/query-guard/unbounded-baseline.json'), 'utf8'));

describe('unbounded-query guard', () => {
  const current = unboundedCounts(ROOT);

  it('no new or more frequent unbounded SELECT compared with the reviewed baseline', () => {
    const added = Object.entries(current)
      .filter(([sql, n]) => n > (BASELINE[sql] || 0))
      .map(([sql, n]) => `${n - (BASELINE[sql] || 0)} new: ${sql}  (${scan(ROOT).filter((q) => q.sql === sql).map((q) => `${q.file}:${q.line}`).join(', ')})`);
    expect(added, 'Add a LIMIT, a key lookup or a time window, or regenerate the baseline and justify it in review').toEqual([]);
  });

  it('the baseline lists nothing that no longer exists (keep it shrinking)', () => {
    const stale = Object.entries(BASELINE).filter(([sql, n]) => (current[sql] || 0) < n).map(([sql]) => sql);
    expect(stale, 'Regenerate the baseline: node tests/query-guard/scan.mjs --write-baseline .').toEqual([]);
  });

  it('classifier: LIMIT, key equality and time windows count as bounded; coin/horizon filters do not', () => {
    expect(isBounded('SELECT * FROM t ORDER BY ts DESC LIMIT 1')).toBe(true);
    expect(isBounded('SELECT * FROM t WHERE id = ?')).toBe(true);
    expect(isBounded('SELECT * FROM t WHERE ts >= ?')).toBe(true);
    expect(isBounded('SELECT * FROM t WHERE slot_ts >= ? AND source_id = ?')).toBe(true);
    expect(isBounded('SELECT ts, btc_price FROM btc_data ORDER BY ts ASC')).toBe(false);
    expect(isBounded('SELECT p_up FROM predictions WHERE realized_up IS NOT NULL AND horizon_hours = ?')).toBe(false);
    expect(isBounded('SELECT * FROM c WHERE coin=? AND horizon_hours=? ORDER BY ts ASC')).toBe(false);
    expect(isBounded('SELECT COUNT(*) AS n FROM research_events')).toBe(false);
  });
});
