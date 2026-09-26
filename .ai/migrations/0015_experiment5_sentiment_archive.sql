-- Experiment 5, Part 1: permanent, append-only sentiment archive.
--
-- Purely additive. No existing table touched, no existing column
-- changed, no existing row affected. Does not modify, replace, or
-- interact with PulseWorker's own `history` table or its 500-row
-- retention DELETE in any way -- that table and its behavior are
-- explicitly out of scope and continue unchanged.
--
-- Forensic finding this responds to (prior read-only phase, verified
-- directly against PulseWorker/worker.js source): every POST /history
-- write is immediately followed by
--   DELETE FROM history WHERE id NOT IN (SELECT id FROM history ORDER BY ts DESC LIMIT 500)
-- -- an unconditional cap, not a passive retention window, and no
-- backup/export of the deleted rows exists anywhere in V1. This
-- migration gives V2/V3's research side a second, append-only copy of
-- the same observation, written alongside (never instead of) the V1
-- write path.
--
-- One row per observation timestamp -- UNIQUE-indexed, the same
-- "identity from a defining property, not autoincrement id" discipline
-- research_events.fingerprint already established (migration 0005).
-- research/sentiment_archive.py's archive_observation() is the only
-- writer this project defines against this table, and it only ever
-- INSERTs (idempotent no-op on an exact repeat, ArchiveConflictError on
-- a genuine same-ts/different-content conflict) -- never UPDATE, never
-- DELETE. Nothing about that discipline is enforced by a SQL trigger
-- here (SQLite/D1 has no row-level write-permission trigger primitive
-- available at zero cost); it is enforced by that module's own code and
-- its tests (test_sentiment_archive.py::test_never_issues_update_or_delete).
--
-- Provenance columns (source_weights_version, schema_version,
-- written_by) are NOT NULL -- provenance is required, not optional,
-- per the build brief's own explicit requirement.
CREATE TABLE research_sentiment_archive (
  archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_ts INTEGER NOT NULL,
  sources_json TEXT,
  score REAL,
  technical_score REAL,
  btc_price REAL,
  gold_regime TEXT,
  source_weights_version TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  written_by TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  archived_ts INTEGER NOT NULL
);
CREATE UNIQUE INDEX idx_research_sentiment_archive_observation_ts ON research_sentiment_archive(observation_ts);
CREATE INDEX idx_research_sentiment_archive_archived_ts ON research_sentiment_archive(archived_ts);

-- Note: this migration is proposed, not applied to production, by this
-- change -- consistent with this project's own established process
-- (e.g. migration 0008's own README note: "not deployed to production").
-- Experiment 5's Python modules are built and tested against an
-- in-memory SQLite fixture carrying this exact schema; applying it to
-- the real `sentiment-history` D1 database is a separate, later,
-- explicitly-authorized deployment step.
