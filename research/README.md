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
| Analysis engine (effectiveness, redundancy, stability) | Deferred | PR5b-e |
| `research_hypotheses` table + selection-decision resolver | Built, PR5a | This migration + `research/selection_resolver.py` |
| Scheduled loop (daily/weekly, GitHub Actions only) | Deferred | PR6 |
| `research_build_requests` | Deferred | PR8 |
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

## PR5a: research_hypotheses + selection_resolver.py

Scope: an additive `research_hypotheses` table (`.ai/migrations/
0008_research_hypotheses.sql`) for tracking a hypothesis's lifecycle
across repeated analysis runs (something the single-run
`research_analyses` table can't represent), plus a new read-only module,
`research/selection_resolver.py`, that resolves which `selection_decisions`
row was "live" for a given BTC prediction. No PR5 analysis code exists
yet -- this PR only builds the schema and the one join PR5b-e will need.

### Why selection_decisions.prediction_ts is not a usable join key

This was meant to be a simple decision (earliest vs. latest duplicate row
per prediction_ts), but real production data shows the premise itself
was wrong. selectBestVariant() in worker.js scores each candidate
variant using only rows where realized_up IS NOT NULL -- which, by
construction, excludes the very prediction it's nominally attached to via
prediction_ts (that prediction hasn't resolved yet at write time). So
chosen_p_up reflects whichever OTHER, already-resolved prediction
happened to be most recent at that moment, not the p_up of the prediction
named by prediction_ts.

Real proof, one BTC/24h prediction (prediction_ts=1789614039967): 7
selection_decisions rows exist for it, ts ranging from ~1.4h to ~29h
AFTER the prediction was made. chosen_p_up drifts from 0.667 (earliest
row) to 0.333 (latest rows) across that single prediction_ts -- proof
that no single row of the seven was ever "the" answer; the join key
itself doesn't mean what it looks like it means. worker.js confirms this
directly in its own comments: production reads selection_decisions via
ORDER BY ts DESC LIMIT 1 relative to now -- a rolling as-of-now state,
not a per-prediction attribute.

selection_resolver.py therefore joins on wall-clock time, not
prediction_ts: MAX(sd.ts) WHERE sd.ts <= predictions.ts -- the same
as-of shape PR2's resolver.py already uses for V1<->V2 alignment.
Applied to the real chain above, this correctly returns no selection
decision at all for that prediction (all 7 rows are logged after it),
rather than attaching one of seven hindsight-drifted candidates.
Confirmed index-driven via a real EXPLAIN QUERY PLAN against production
D1 -- idx_predictions_horizon_ts and the existing
idx_selection_decisions_coin_time cover every step, no new index needed.
Verified in research/test_selection_resolver.py, including a direct
reproduction of the real 7-row chain.

Scope is BTC only (predictions/selection_decisions) for PR5a, matching
PR2's own precedent of not generalizing across coins until each coin's
table shape is independently verified -- LINK/ETH are explicitly
deferred.

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
`.ai/migrations/0009_outcome_engine_predictions_ts_index.sql` adds a
plain `CREATE INDEX idx_predictions_ts ON predictions(ts)` — purely
additive, no behavior change, confirmed via a second `EXPLAIN QUERY
PLAN` (local SQLite) to restore a genuine bounded search. (Renumbered
from 0008 to 0009 after PR5a's own 0008 migration merged first — see
git history for the coordination note.)

## PR5c: V1 source effectiveness, redundancy, and incremental information

Scope: read-only research analysis of the 21 raw V1 sentiment sources
found in `history.sources_json`, plus the three-level framework
(signal exists / associated with outcome / adds incremental
information) the PR5 specification requires. No V1/V2 change, no
coefficient change, no production write, no migration, no LLM/paid
dependency, no build request, no PR5d.

### Why no new migration is needed

Every new query this PR adds is bounded by `ts` against `history` and
`btc_data`, both already covered by their existing `idx_ts` /
`idx_btc_data_ts` indexes. Confirmed via real `EXPLAIN QUERY PLAN`
runs against production D1 (`sentiment-history`) before writing any
code: the history-anchored outcome query shows `SEARCH h USING INDEX
idx_ts` plus `SEARCH b/b2 USING INDEX idx_btc_data_ts` for all three
correlated subqueries — no scan anywhere; the source-discovery query
(`json_each` over `sources_json`) shows the same indexed `SEARCH h`
feeding the JSON virtual table, with only a small in-memory `DISTINCT`
b-tree over the discovered keys (harmless at 21 keys / 500 rows).
`research_analyses` (PR1) already has an unconstrained `metric_json
TEXT` column, which is where this PR's entire nested findings payload
is persisted — no new column, table, or index was required, so none
was added.

### The three levels (implemented in `research/source_analysis.py`)

- **Level 1** (`source_coverage_report`): per-source coverage,
  missingness, distinct-value count, raw min/max/mean/stddev, first/last
  ts present. A source with zero variation or zero data is flagged
  `NO_VARIATION`/`NO_DATA` and excluded from Level 2+ (there is nothing
  to associate).
- **Level 2** (`level2_association_for_source` / `run_level2_battery`):
  Pearson correlation between each eligible source and the actual BTC
  forward return at that same observation time (`history.ts`), at all
  five horizons (1h/3h/6h/12h/24h), with a 95% CI and p-value via the
  Fisher z-transformation (`research/stats_utils.py`), corrected across
  the *entire* source x horizon battery with Benjamini-Hochberg (BH)
  FDR (never per-test raw p < 0.05). BH, not Bonferroni, is used here —
  stated neutrally: Bonferroni does not require independent tests
  either, but BH's false-discovery-rate control is the better fit for a
  discovery-stage screen across this many related source x horizon
  tests (the same BTC forward return is reused as the outcome across
  every source at a given horizon). The correction method is recorded
  explicitly alongside every result, never left implicit.
- **Level 3** (`level3_incremental_for_source`): incremental information
  **strictly beyond the V1 composite** — partial correlation controlling
  for the V1 composite only, plus a chronological (never shuffled)
  discovery/validation split comparing a composite-only OLS baseline
  against a composite+source OLS model's out-of-sample RMSE. This does
  **not** condition on any other, empirically correlated/redundant
  source — Section 9's own redundancy findings (below) are computed
  entirely separately and never feed into Level 3's regression. PR5c's
  Level 3 result must therefore be read as "incremental beyond the V1
  composite" only; "incremental beyond correlated/redundant sources" is
  a separate, larger research question this PR does not address (a
  multi-source ablation/regression was deliberately not added here — see
  PR review notes). A source reaching Level 2 significance is NOT
  assumed to reach Level 3 — real production data confirms this
  distinction matters (see PR description: several Level-2-significant
  sources show `oos.status: NOT_IMPROVED`, i.e. no measurable
  incremental value once the V1 composite is already in the model).

### Source enumeration, missingness, and scale (Sections 5/6)

`discover_sources()`/`extract_source_matrix()` derive the source list
from the actual `sources_json` keys present in the requested window —
nothing is hard-coded. A key absent from a given row is `None`, never
`0`; `structural_shape_report()` reports how many distinct key-sets
actually occurred (24, across the full current 500-row table).
Raw values are never normalized before correlation (Pearson r is
scale-invariant to any positive linear rescaling — see
`stats_utils.py`'s docstring for the full argument); every reported
regression coefficient is instead paired with that source's own
Level 1 range so it is never misread as cross-source-comparable.

### No-lookahead (Section 4)

`research/outcome_engine.py` gained
`compute_forward_returns_from_history()`, the same as-of, no-lookahead
BTC forward-return computation as PR5b's `compute_forward_returns()`
(predictions-anchored), anchored on `history.ts` instead — refactored
to share one resolution rule (`_resolve_outcome()`) rather than
duplicating PR5b's safety-critical logic. `research/evidence_temporal.py`
adds an independent, orthogonal prediction-time eligibility check
(`publication_ts < prediction_ts`, strict) for the one place PR4
evidence could later be joined against a prediction — kept separate
from PR4's own event-relative `PRE_EVENT/SAME_WINDOW/POST_EVENT`
classification per the spec's explicit requirement not to conflate the
two. PR4 evidence integration itself remains optional and unused by
this PR's core analysis (Section 16); `source_analysis.py` has no
dependency on `research_event_evidence` at all.

### Persistence (Section 15)

`source_analysis.persist_analysis()` is the only function in this PR
that writes anything — a single `INSERT INTO research_analyses` using
its existing, unmodified schema. It is exercised only against an
in-memory SQLite fixture in `test_source_analysis.py` and is never
invoked against a production connection anywhere in this PR (Section
19: production D1 access stays read-only throughout). A separate,
future authorized step decides if/when to actually run this against
production.

### Real production findings (see PR description for the full report)

Run once, read-only, against the real 500-row `history` /
2091-row `btc_data` tables (never persisted back): of 105 source x
horizon tests, Benjamini-Hochberg marks 39 `STATISTICALLY_SIGNIFICANT`,
20 `CONTRADICTED` (sign reverses between the chronological discovery
and validation halves with an adequate, independent sample in both),
20 `STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE`, and 26 `INCONCLUSIVE`.
`nasdaq`/`sp500` show strong pairwise redundancy (r=+0.82, expected --
both are traditional macro proxies); `etfflows` shows strong
redundancy against the V1 composite itself (r=+0.75). Because the
entire history table currently spans only ~34 days, the chronological
discovery/validation split is NOT an independent market regime in any
strong sense -- it is largely the same regime split in time -- so the
`CONTRADICTED` count above should be read as "sign instability within
one short window," not as proof of a reversing macro relationship;
this is stated explicitly, not smoothed over, per the spec's own
prohibition on treating insufficient replication as more than it is.

## PR5d: V2 error classification against resolved outcomes and event context

Scope: BTC only (`predictions`), matching PR2/PR5a/PR5c's own precedent.
Read-only against `predictions`, `history`, `btc_data`, `research_events`,
`research_event_evidence`. No V1/V2 change, no coefficient change, no
migration (verified genuinely unnecessary -- see below), no LLM/paid
dependency, no build request, no PR5e.

`research/error_classification.py` deterministically classifies every
resolved BTC prediction into one of the nine required error types (or
no error) via a single, versioned, first-match-wins decision rule
(`CLASSIFICATION_RULE_VERSION = "pr5d-v1"`), then reports counts/rates
by horizon, V1 composite bucket, PR3 event category, regime, confidence
band, model version, and chronological period -- every bucket carrying
its own sample size, never a bare rate.

### Why no new migration

The base query (`BASE_SQL`) reuses `resolver.py`'s exact as-of join
shape (`h.ts = (SELECT MAX(h2.ts) FROM history h2 WHERE h2.ts <= p.ts)`),
extended with the extra `predictions` columns PR5d needs (`p_up`,
`realized_return`, `model_version`, `git_commit_sha`). Confirmed via a
real `EXPLAIN QUERY PLAN` against production before writing any code:
`SEARCH p USING INDEX idx_predictions_horizon_ts`, `SEARCH h USING
INDEX idx_ts` for the join, `SEARCH h2 USING COVERING INDEX idx_ts` for
the correlated subquery -- identical plan shape to `resolver.py`'s own
proven query, no scan anywhere. `research_analyses`' existing
`metric_json` TEXT column holds `methodology_version` alongside the
rest of the findings payload, exactly as PR5c's `persist_analysis()`
already established -- no schema change needed there either.

### Event linkage: computed live, not read from the sparse persisted table

Production `research_events` has only 3 rows (all `LARGE_MOVE`, from a
single manual verification run) and `research_event_evidence` has 0
rows (both confirmed by direct read-only count before writing any
code). Joining against that table as the primary signal would make
almost every prediction spuriously `MISSING_EVENT` for lack of
persistence, not for a genuine absence of a market event. Instead,
`fetch_events_for_window()` calls PR3's own, UNCHANGED public detector
functions (`detect_large_moves`, `detect_regime_reversals`,
`detect_volatility_expansion`, `detect_v1_btc_divergence`) once per
analysis window and reuses the results across every prediction in that
window (no N+1 query pattern). `find_evidence_for_event()` still offers
an OPTIONAL, bounded cross-reference against the persisted
`research_events`/`research_event_evidence` tables for Section 16 --
tested against a fixture that has evidence, and expected to return
`NO_EVIDENCE_FOUND` against real production data today, which is
reported honestly, not hidden.

### No new thresholds invented

- V1 composite bucketing reuses `event_detector.FROZEN_V1_BEARISH_EXTREME`
  (45) / `FROZEN_V1_BULLISH_EXTREME` (58) verbatim (imported, not
  duplicated) -- the same boundary PR3's own `V1_BTC_DIVERGENCE`
  detector already uses.
- The magnitude cut separating `CORRECT_DIRECTION_WRONG_MAGNITUDE` from
  a directionally-correct-and-close prediction is never hard-coded:
  `empirical_magnitude_threshold()` reuses PR5b's
  `movement_distribution.summarize_absolute_returns()` over the
  window's own directionally-correct sample and proposes the median as
  the cut (PROPOSED, not frozen -- human review required before
  freezing), exactly mirroring `propose_movement_buckets()`'s own
  convention.
- The staleness gap for `STALE_SENTIMENT` is likewise derived from the
  window's own `(prediction_ts - v1_observation_ts)` distribution
  (`empirical_staleness_threshold()`, 90th percentile proposed as the
  cut), never an invented number of hours.
- `REGIME_CHANGE` and `UNEXPECTED_SHOCK` are driven entirely by PR3's
  own, unmodified detector functions -- this module never re-derives or
  re-freezes their thresholds.

### Temporal safety

Every source of explanatory signal is checked against
`information available at prediction_ts -> prediction -> realized
outcome at target_ts` explicitly: V1 context comes from the as-of join
(never a later observation, by construction); PR3 events are only ever
used from the set with `prediction_ts < event_ts <= target_ts` (checked
via `_events_overlapping_window()`, with constructive boundary tests
proving an event at or before `prediction_ts` is excluded and one at
exactly `target_ts` is included); `V1_BTC_DIVERGENCE` events are
additionally always `is_post_event_analysis=1` (PR3's own flag, read
verbatim) and used here strictly as post-outcome context for
`MISLEADING_SENTIMENT`, never as information available to the original
prediction -- proven, not just commented, by
`test_no_lookahead_event_before_prediction_never_used` and
`test_v1_btc_divergence_always_used_as_post_outcome_only`.

### Real production findings (read-only, never persisted)

Run once against the real 1070-row `predictions` table (395 at 12h,
675 at 24h) joined with the real 500-row `history` and 2095-row
`btc_data` snapshots:

- 12h: 391 resolved -- 128 no-error, 55 `WRONG_DIRECTION`, 67
  `CORRECT_DIRECTION_WRONG_MAGNITUDE`, 83 `INSUFFICIENT_INFORMATION`
  (predictions predating `history`'s own ~34-day retention window --
  `predictions` spans ~49 days), 24 `UNEXPECTED_SHOCK`, 24
  `TECHNICAL_SENTIMENT_CONFLICT`, 7 `STALE_SENTIMENT`, 3 `MISSING_EVENT`.
- 24h: 673 resolved -- 228 no-error, 54 `WRONG_DIRECTION`, 85
  `CORRECT_DIRECTION_WRONG_MAGNITUDE`, 174 `INSUFFICIENT_INFORMATION`,
  68 `UNEXPECTED_SHOCK`, 48 `MISSING_EVENT`, 13
  `TECHNICAL_SENTIMENT_CONFLICT`, 3 `STALE_SENTIMENT`.
- `REGIME_CHANGE` and `MISLEADING_SENTIMENT` were never assigned in
  this run -- verified independently (not just from the classifier's
  own output) that every wrong-direction prediction whose window
  overlapped a `REGIME_REVERSAL` or `V1_BTC_DIVERGENCE` event ALSO
  overlapped a `LARGE_MOVE` event, which this module's fixed,
  documented priority order ranks above both. This is a real,
  deterministic consequence of the priority order and this window's
  specific event co-occurrence pattern, not a detection failure --
  stated explicitly as a limitation of the current priority design,
  not hidden.
- `model_version` isolation shows a real, stark difference: `legacy`
  (420 resolved @24h) is dominated by `INSUFFICIENT_INFORMATION` (174)
  because `legacy` predictions predate `history`'s retention window;
  `knn-core-v1` (244 resolved @24h) has zero `INSUFFICIENT_INFORMATION`
  but 77 `CORRECT_DIRECTION_WRONG_MAGNITUDE` and 67 `UNEXPECTED_SHOCK`.
  Comparing raw error rates between these two model versions without
  accounting for this V1-availability confound would be misleading --
  reported here as a limitation, not smoothed into a single combined rate.
- Two candidate observations (evidence_gate_status `OBSERVATION`, not
  `RESEARCH_HYPOTHESIS` -- neither recurred across >=2 independent
  breakdown dimensions in this run): V1 `BEARISH` bucket shows
  `WRONG_DIRECTION` as its dominant error (58-67% share, n=45-57); the
  earliest chronological period shows `INSUFFICIENT_INFORMATION` as
  dominant (53-60% share) -- both are exactly the direct, expected
  consequence of `history`'s shorter retention window and BEARISH's
  smaller/noisier sample, not surprising findings.

### Limitations

- `predictions` spans ~49 days but `history` only ~34 -- roughly a
  third of `predictions` rows have no V1 context at all
  (`INSUFFICIENT_INFORMATION` dominates early rows and the `legacy`
  model_version almost entirely as a result of this, not necessarily
  of model quality).
- `research_events`/`research_event_evidence` are too sparse in
  production for the OPTIONAL PR4-evidence cross-reference to surface
  anything real today -- `find_evidence_for_event()` is implemented and
  tested, but a real run reports `NO_EVIDENCE_FOUND` throughout.
- The fixed priority order (`UNEXPECTED_SHOCK` > `REGIME_CHANGE` >
  `MISSING_EVENT` > `MISLEADING_SENTIMENT` > `TECHNICAL_SENTIMENT_CONFLICT`
  > `STALE_SENTIMENT` > `WRONG_DIRECTION`) means a prediction matching
  multiple candidate causes is only ever attributed to the highest-
  priority one -- real data shows this order has visible consequences
  (see above), and revisiting it is future work, not resolved here.
- No error_type here is a validated, causal explanation -- every
  classification is descriptive and every candidate_observation is
  capped at `RESEARCH_HYPOTHESIS`, never higher, per the build
  authorization.
