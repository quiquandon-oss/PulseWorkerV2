// Builds a local SQLite database: minimal V1/V2 base tables the Research Lab
// reads, then the REAL migration files from .ai/migrations in order, then
// synthetic Stage 7 candidates. Nothing here touches Cloudflare.
import { readdirSync, readFileSync, rmSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { D1Shim } from './d1_shim.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const MIGRATIONS = join(here, '..', '..', '.ai', 'migrations');

const BASE_TABLES = `
CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL, btc_price REAL,
  sources_json TEXT, technical_score INTEGER, gold_regime TEXT, regime_mag REAL, bottom_score INTEGER, global_mcap REAL);
CREATE INDEX idx_ts ON history(ts);
CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL);
CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, horizon_hours INTEGER, p_up REAL, realized_up INTEGER);
`;

export function buildDatabase(dbPath, { upTo = '0018' } = {}) {
  rmSync(dbPath, { force: true });
  const db = new D1Shim(dbPath);
  db.exec(BASE_TABLES);
  const applied = [], failed = [];
  for (const file of readdirSync(MIGRATIONS).filter((f) => f.endsWith('.sql')).sort()) {
    if (file.slice(0, 4) > upTo) continue;
    if (file.startsWith('0001') || file.startsWith('0002') || file.startsWith('0003') || file.startsWith('0004')) continue; // EXP-004/TimesFM: not part of this surface
    try { db.exec(readFileSync(join(MIGRATIONS, file), 'utf8')); applied.push(file); }
    catch (e) { failed.push(`${file}: ${e.message}`); }
  }
  return { db, applied, failed };
}

const HOUR = 3600000;
export function seedStage7(db, now = Date.now()) {
  const events = [
    // Events are 2+ days old (still inside the 6-day evidence window) so a source dated the day
    // AFTER an event is a real, non-future "published after the historical cutoff" case.
    { id: 101, ts: now - 50 * HOUR, cat: 'LARGE_MOVE', dir: 'UP' },
    { id: 102, ts: now - 52 * HOUR, cat: 'VOLATILITY_SPIKE', dir: null },
    { id: 103, ts: now - 54 * HOUR, cat: 'LARGE_MOVE', dir: 'DOWN' },
    { id: 104, ts: now - 8 * 24 * HOUR, cat: 'LARGE_MOVE', dir: 'DOWN' }, // outside the 6-day evidence window
    { id: 105, ts: now - 51 * HOUR, cat: 'LARGE_MOVE', dir: 'UP' },         // has a pre-existing STALE candidate
  ];
  for (const e of events) {
    db.prepare('INSERT INTO research_events (event_id, fingerprint, event_ts, detection_ts, category, direction, intensity, available_before_prediction, is_post_event_analysis) VALUES (?,?,?,?,?,?,?,?,0)')
      .bind(e.id, `fp-${e.id}`, e.ts, e.ts + 60000, e.cat, e.dir, 5.0, 1).run();
  }
  const sufficiency = 'INSUFFICIENT_EVIDENCE';
  for (const e of events.filter((x) => x.id <= 104)) {
    db.exec(`INSERT INTO stage7_research_candidates (candidate_id, event_id, proposed_ts, updated_ts, status, sufficiency_status,
      reasons_json, questions_json, missing_categories_json, historical_cutoff_ts, evidence_snapshot_json, input_fingerprint)
      VALUES ('stage7-cand-${e.id}', ${e.id}, ${now - HOUR}, ${now - HOUR}, 'PROPOSED', '${sufficiency}',
      '["No evidence rows exist for this event (synthetic acceptance data)"]',
      '["What happened, according to primary reporting, before the move?"]',
      '["primary_reporting"]', ${e.ts}, '[]', 'fp-cand-${e.id}')`);
  }
  // A candidate that is already STALE must never appear in the review batch.
  db.exec(`INSERT INTO stage7_research_candidates (candidate_id, event_id, proposed_ts, updated_ts, status, sufficiency_status,
      reasons_json, questions_json, missing_categories_json, historical_cutoff_ts, evidence_snapshot_json, input_fingerprint)
      VALUES ('stage7-cand-105', 105, ${now - HOUR}, ${now - HOUR}, 'STALE', '${sufficiency}', '[]', '[]', '[]', ${now - 51 * HOUR}, '[]', 'fp-stale')`);
  return events;
}
