"""
Experiment 5, Part 1: permanent, append-only sentiment archive.

Problem this responds to (forensic finding from the prior read-only
phase, verified directly against PulseWorker/worker.js source, not
assumed): every POST /history write immediately inserts one row and
then runs a second statement that purges every row beyond the most
recent 500, ordered newest-first (id NOT IN (SELECT id ... ORDER BY ts
DESC LIMIT 500))

-- an unconditional 500-row cap, not a passive retention window, and V1
has no backup/export of what gets deleted. This module does not touch
that DELETE or that table at all -- `history` is explicitly out of
scope and must keep working exactly as it does today. This module gives
V2/V3's research side a second, NEVER-DELETING copy of the same
observation, written alongside (not instead of) the existing write path.
Wiring an actual call site into PulseWorker's own POST /history handler
is a separate, later change (see research/README.md's Experiment 5
section, "Not yet wired") -- this module is the reusable primitive that
change would call, built and tested in isolation first.

Design:
- One row per observation_ts, UNIQUE-indexed -- the same "identity from
  a defining property, not autoincrement id" discipline
  research_events.fingerprint already established (migration 0005).
- content_hash (sha256 over a canonical join of every preserved field,
  same construction style as evidence_collector._content_hash) makes a
  second archive_observation() call for an already-archived ts a safe,
  detectable no-op ONLY when the content is byte-identical; a genuine
  conflict (same ts, different content) raises ArchiveConflictError
  instead of silently overwriting -- the literal enforcement of "do not
  silently overwrite an existing observation."
- This module issues INSERT and SELECT statements only against
  research_sentiment_archive. No other write-statement type is ever
  built here -- verified by a dedicated test that inspects this
  module's own source, not just by convention.
- source_weights_version / schema_version / written_by are required,
  non-optional arguments -- provenance is never allowed to be silently
  NULL.
- sources_json is stored EXACTLY as given (already a dict or already a
  JSON string) -- never renormalized, never re-scored, per "preserve
  exactly what was known at timestamp T."
"""
import hashlib
import json

SCHEMA_VERSION = "exp5-v1"


class ArchiveConflictError(Exception):
    """Raised when archive_observation() is called for an observation_ts
    that already exists with DIFFERENT content. Never silently
    overwritten -- the caller must resolve the conflict itself (this
    should not happen in normal operation, since observation_ts is a
    real, once-only write moment; this exists as a safety net, not an
    expected code path)."""


def _canonical_json(value):
    if value is None:
        return "null"
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def compute_content_hash(observation_ts, sources_json_text, score, technical_score,
                          btc_price, gold_regime, source_weights_version, schema_version):
    """Deterministic -- identical input always produces the identical
    hash, same guarantee/construction style as
    evidence_collector._content_hash. sources_json_text must already be
    a canonical string (archive_observation() does that canonicalization
    once, before calling this, so this function itself never needs to
    know whether the original value was a dict or a string)."""
    payload = "\n".join([
        str(observation_ts),
        sources_json_text if sources_json_text is not None else "null",
        _canonical_json(score),
        _canonical_json(technical_score),
        _canonical_json(btc_price),
        _canonical_json(gold_regime),
        str(source_weights_version),
        str(schema_version),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def archive_observation(conn, observation_ts, sources_json, score, *, technical_score=None,
                         btc_price=None, gold_regime=None, source_weights_version,
                         written_by, archived_ts, schema_version=SCHEMA_VERSION):
    """Append one observation. Idempotent: calling this twice for the
    same observation_ts with the same content is a no-op that returns
    the existing archive_id (never inserts a second row, never touches
    the existing row). Calling it twice with DIFFERENT content for the
    same observation_ts raises ArchiveConflictError.

    archived_ts is required and has no wall-clock default, keeping this
    function pure and independently testable -- callers pass an
    explicit "now" (e.g. Date.now()-equivalent from the caller's own
    environment), matching this project's existing convention
    (source_analysis.persist_analysis, hypothesis_gate.persist_hypothesis
    both take an explicit *_ts rather than calling time.time() internally).
    """
    if archived_ts is None:
        raise ValueError("archived_ts is required -- no wall-clock default")
    if not source_weights_version:
        raise ValueError("source_weights_version is required -- provenance is never optional")
    if not written_by:
        raise ValueError("written_by is required -- provenance is never optional")

    sources_json_text = sources_json if isinstance(sources_json, str) else _canonical_json(sources_json)
    content_hash = compute_content_hash(
        observation_ts, sources_json_text, score, technical_score, btc_price, gold_regime,
        source_weights_version, schema_version,
    )

    existing = conn.execute(
        "SELECT archive_id, content_hash FROM research_sentiment_archive WHERE observation_ts = ?",
        (observation_ts,),
    ).fetchone()
    if existing is not None:
        existing_id, existing_hash = existing
        if existing_hash == content_hash:
            return existing_id
        raise ArchiveConflictError(
            f"observation_ts={observation_ts} already archived with different content "
            f"(existing_hash={existing_hash}, new_hash={content_hash}) -- refusing to overwrite"
        )

    cursor = conn.execute(
        "INSERT INTO research_sentiment_archive "
        "(observation_ts, sources_json, score, technical_score, btc_price, gold_regime, "
        " source_weights_version, schema_version, written_by, content_hash, archived_ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (observation_ts, sources_json_text, score, technical_score, btc_price, gold_regime,
         source_weights_version, schema_version, written_by, content_hash, archived_ts),
    )
    return cursor.lastrowid


def get_archive_range(conn, start_ts, end_ts):
    """Read-only. Both bounds inclusive, both required -- no unbounded
    query possible by omission, matching resolver.py's own convention.
    Parses sources_json into a "sources" dict on each returned row for
    caller convenience (the raw sources_json string is also returned,
    unmodified, for anything that needs the exact original text)."""
    if start_ts is None or end_ts is None:
        raise ValueError("start_ts and end_ts are both required")
    cursor = conn.execute(
        "SELECT archive_id, observation_ts, sources_json, score, technical_score, btc_price, "
        "gold_regime, source_weights_version, schema_version, written_by, content_hash, archived_ts "
        "FROM research_sentiment_archive WHERE observation_ts BETWEEN ? AND ? ORDER BY observation_ts ASC",
        (start_ts, end_ts),
    )
    columns = [d[0] for d in cursor.description]
    rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for row in rows:
        row["sources"] = json.loads(row["sources_json"]) if row["sources_json"] else {}
    return rows
