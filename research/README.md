# CryptoPulse Research Ledger

## PR1 scope (this PR)

Schema only: `.ai/migrations/0005_research_schema.sql`, its tests
(`research/test_migration.py`), and this document. No event detector, no
internet requests, no LLM calls, no new scheduled workflow, no V1/V2
changes, no production deployment changes. Zero paid dependencies.

## The research principle

> Given only information available at time T, determine which
> information/event/sentiment dimensions improve the accuracy of the
> subsequent BTC prediction, measured against the actual BTC outcome.

Consequences that every later phase of this system must respect:

- Sentiment correlation alone is not the target. A source can correlate
  with BTC's *past* behavior without adding anything to a *prediction*.
- Retrospective explanations are not evidence. An article published
  after a price move that "explains" it is not predictive information,
  no matter how plausible.
- Post-event information must never enter predictive analysis. Every
  event record must be able to answer, honestly, whether the underlying
  information was available before the prediction it's being evaluated
  against.
- Coefficient optimization must never run on the discovery sample. A
  hypothesis formed by looking at a window of data must be validated
  against observations *after* that window, not re-scored on the same
  data that produced it.
- Every candidate change is validated out-of-sample before it becomes a
  build request, and every build request requires human approval before
  touching V1 or V2. Nothing in this system deploys itself.

## What's built vs. deliberately deferred

| Piece | Status | Where |
|---|---|---|
| `research_observations_v1` (view over `history`) | Built, PR1 | This migration |
| `research_events` (event ledger, no evidence columns) | Built, PR1 | This migration |
| `research_analyses` | Built, PR1 | This migration |
| V2 linkage (`selection_decisions`/`predictions`) | Deferred | PR2, only after a non-correlated join strategy is proven via `EXPLAIN QUERY PLAN` |
| Event detection (thresholds, no LLM) | Deferred | PR3 |
| Internet evidence layer (`research_event_evidence`, one-to-many from `research_events`) | Deferred | PR4 |
| Analysis engine (effectiveness, redundancy, stability) | Deferred | PR5 |
| Scheduled loop (daily/weekly, GitHub Actions only) | Deferred | PR6 |
| `research_hypotheses` / `research_build_requests` | Deferred | PR7-8 |
| Lab UI extension | Deferred | PR9, only after the backend is proven |

### Why V2 linkage isn't in PR1

The original design's observation view joined against `selection_decisions`
via a correlated subquery (`sd.prediction_ts = (SELECT MAX(p.ts) ...)`),
re-evaluated per row of `history`. This is the same shape of mistake that
caused a real 16.7M-row-read incident earlier in this project (a different,
unrelated forensic query, but the identical underlying pattern). PR1's view
is a single-table `SELECT` over `history` only — no join, no subquery, no
query-plan risk to evaluate. Confirmed via `EXPLAIN QUERY PLAN` against a
real SQLite execution (not just read from the SQL text): a `ts`-filtered
query against the view uses `history`'s existing `idx_ts` index directly.

### Why events and evidence are separate tables, not one

`research_events` holds only the event's own properties (timestamp,
category, direction, intensity, a deterministic uniqueness fingerprint).
No URL, publication timestamp, or evidence-hash columns live here. A
single real-world event is often described by multiple independent
(and sometimes disagreeing) sources — modeling evidence as a separate,
one-to-many table keyed on `event_id` is what lets disagreement between
sources be preserved rather than flattened into one record, exactly as
the earlier design review requires. That table doesn't exist yet because
PR1 makes no internet requests at all.

### Why there's no cost/run-accounting table yet

A `research_runs` table (run timestamp, GitHub Actions run ID, rows
processed, D1 reads/writes where measurable, external requests made vs.
skipped, execution status) is a real, documented requirement — but it
has nothing to account for until a scheduled run exists. It belongs
with PR6, the first PR that actually introduces a recurring workflow.

## Idempotency

`research_events.fingerprint` is a required, `UNIQUE`-indexed column —
event identity is never inferred from the autoincrement `event_id` alone.
Whichever detector eventually populates this table (PR3) must compute the
fingerprint deterministically from the event's own defining properties
(e.g. `category + event_ts`, or a hash including `direction`), so that
re-running the same detection logic against the same underlying data is
safe: a second attempt to record an already-known event fails the
uniqueness constraint rather than creating a duplicate row. Verified
directly in `research/test_migration.py` — a duplicate-fingerprint
insert raises `IntegrityError` and leaves the table unchanged, not just
asserted from the schema text.

## PR5b: outcome engine + empirical movement distribution

`research/outcome_engine.py` computes continuous BTC forward returns
for every V2 prediction in a bounded window, at fixed outcome horizons
(1h/3h/6h/12h/24h) independent of what horizon that prediction itself
targets (`predictions.horizon_hours`, currently 12 or 24 in production
— a different axis). For each prediction it as-of-joins to `btc_data`
twice: once for the price at/before the prediction timestamp, once for
the price at/before `prediction_ts + horizon`, mirroring the proven-safe
correlated-subquery shape from `resolver.py`/`selection_resolver.py`.

A prediction is only ever reported `RESOLVED` when a genuinely new
`btc_data` observation exists strictly after the prediction timestamp
within the requested horizon; otherwise it is reported
`UNRESOLVED_NO_FUTURE_PRICE_POINT` with all outcome fields `None` — it
is never dropped from the result set and never given a fabricated or
interpolated value. This directly answers PR5b's "do not silently
interpolate missing outcomes" requirement.

`research/movement_distribution.py` is a separate, pure-function module
(no database, no network) that computes descriptive statistics —
mean/median/stddev/min/max of the signed return, and P50/P75/P90/P95/P99
of the *absolute* return — plus bucket counts against the illustrative
`<1% / 1-2% / 2-3% / 3-4% / >=4%` boundaries, and reports what share of
observed movement sits below PR3's frozen `LARGE_MOVE_THRESHOLD_PCT`
(duplicated as `PR3_LARGE_MOVE_THRESHOLD_PCT`, asserted equal to
`event_detector.LARGE_MOVE_THRESHOLD_PCT` by test so the two can never
silently drift apart). Its `propose_movement_buckets()` output is
always labeled `status: "PROPOSED"` — this PR does not freeze a bucket
taxonomy anywhere; freezing is an explicit, separate, human-reviewed
step outside this module's scope.

Real production data (see PR description for the full numbers) shows
84–100% of observed forward movement sits below PR3's 4% threshold
depending on horizon — the large-move detector's frozen boundary is,
by construction, silent on the great majority of subsequent BTC
movement. That is reported here as a plain descriptive fact, not a
recommendation to change PR3's threshold.

### Why a new index (`idx_predictions_ts`) was needed

`outcome_engine.py`'s outer query cannot filter on
`predictions.horizon_hours` (it needs every prediction regardless of
its own horizon), so the existing composite
`idx_predictions_horizon_ts(horizon_hours, ts)` cannot serve as a range
seek for a `ts`-only filter — confirmed via a real `EXPLAIN QUERY PLAN`
against production before writing the migration, which showed a full
covering-index `SCAN` rather than a bounded `SEARCH`. Harmless at
today's ~1,068 total prediction rows, but the same category of
unbounded-with-scale pattern this project has avoided everywhere else.
`.ai/migrations/0008_outcome_engine_predictions_ts_index.sql` adds a
plain `CREATE INDEX idx_predictions_ts ON predictions(ts)` — purely
additive, no behavior change, confirmed via a second `EXPLAIN QUERY
PLAN` (local SQLite) to restore a genuine bounded search.
