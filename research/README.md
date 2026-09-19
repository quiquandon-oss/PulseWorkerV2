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

### Interpretation caveats (required reading before using these counts)

Two clarifications added after independent review, both documentation-
only (no classification logic changed):

> Classification labels are mutually exclusive winner labels determined
> by documented precedence. `contributing_signals` preserves additional
> matched categories. Winner-label frequencies must not be interpreted
> as independent estimates of causal prevalence.

> `empirical_staleness_threshold()` and `empirical_magnitude_threshold()`
> are descriptive batch statistics calculated from the analysis window.
> They must not be interpreted as information available to the original
> prediction at prediction time.

The first exists because `classify_prediction()` reports exactly one
`error_type` per prediction (the highest-ranked match in the fixed
precedence below), even when a prediction's window genuinely satisfies
multiple candidate causes at once -- confirmed happening in real
production data (see "Real production findings" below) and now proven
with constructive tests (`test_large_move_outranks_regime_reversal_when_both_present`
and five siblings in `test_error_classification.py`) rather than only
observed from the aggregate output. The suppressed categories are never
discarded -- they remain visible per-row via `contributing_signals`'s
own counts -- but they are invisible to any code that reads only the
`error_counts` totals. A queued, NOT-yet-built follow-up ("PR5d-
followup: overlap analysis") would report every matched category
per prediction (not only the winner) plus a co-occurrence matrix, to
let the two views -- "what does the current deterministic taxonomy
assign" vs. "what candidate explanations were actually simultaneously
present" -- be compared directly. This PR deliberately does not build
that; the priority chain itself is not being redesigned until that
overlap data exists.

The second exists because the PROPOSED staleness/magnitude cuts are
computed once over the *entire* analysis window (including rows
chronologically after the specific prediction being classified) -- a
valid batch descriptive statistic (the same kind PR5b's
`movement_distribution` module already computes), but never something
an online, real-time process could have known before the window
finished. Only the per-row inputs actually compared against these
thresholds (a row's own staleness gap, a row's own realized_return) are
prediction-time-safe in the Section 4 sense.

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

## PR5d-followup: error taxonomy overlap / suppression analysis

Scope: analysis-only. Measures the effect of PR5d's existing, UNCHANGED
precedence chain -- does not redesign it, does not "fix" the taxonomy.
No V1/V2/Worker change, no PR3 change, no priority-chain change, no new
threshold, no schema migration, no production write (this module has
no persistence function at all).

### Why this analysis exists

`classify_prediction()` reports exactly one `error_type` per prediction
(the highest-ranked match under a fixed precedence). PR5d's own review
found predictions that genuinely satisfy multiple candidate causes at
once, with only the winner visible in `error_counts`. This follow-up
answers, empirically: which categories actually matched simultaneously,
and how much does the precedence change what the aggregate numbers show?

### winner_label vs. all_matched_categories -- two different questions

- **winner_label** (`research/error_classification.py`'s own
  `error_type`, copied verbatim, never recomputed): "what does the
  current deterministic taxonomy assign?"
- **all_matched_categories** (this module, `research/error_overlap_analysis.py`):
  "what candidate explanations were actually simultaneously present?",
  derived by reading `contributing_signals`' own six fields directly --
  `matched_categories_for_classification()` never re-runs an event-
  window overlap query or recomputes an empirical threshold; it only
  interprets values PR5d's classifier already computed.

These are reported as two separate distributions (Section 6A/6B below)
and must never be combined into one statistic.

### Scope: the six categories in PR5d's reviewed precedence chain

`UNEXPECTED_SHOCK`, `REGIME_CHANGE`, `MISSING_EVENT`,
`MISLEADING_SENTIMENT`, `TECHNICAL_SENTIMENT_CONFLICT`,
`STALE_SENTIMENT` each have their own independent match condition in
`contributing_signals`. `WRONG_DIRECTION` / `CORRECT_DIRECTION_WRONG_MAGNITUDE`
/ `INSUFFICIENT_INFORMATION` are PR5d's residual outcomes (not
independently-matchable causes) and are excluded from co-occurrence/
suppression, matching the follow-up's own worked example. PR3's own
event category names map one-to-one to four of these six
(`LARGE_MOVE`->`UNEXPECTED_SHOCK`, `REGIME_REVERSAL`->`REGIME_CHANGE`,
`VOLATILITY_EXPANSION`->`MISSING_EVENT`,
`V1_BTC_DIVERGENCE`->`MISLEADING_SENTIMENT`) by construction of
`classify_prediction()` itself -- "LARGE_MOVE + REGIME_CHANGE" and
"UNEXPECTED_SHOCK + REGIME_CHANGE" are the literal same measurement
here, reported once.

Rows with no V1 context (or UNRESOLVED) are `NOT_EVALUABLE` for all six
categories, never assumed "no match" -- `classify_prediction()` itself
never computes these signals for that population.

### Production snapshot / window

Same production D1 (`sentiment-history`) pulled fresh, read-only, for
this follow-up: 1070 `predictions` rows (unchanged since the PR5d
review round), 500 `history` rows (unchanged), 2095 `btc_data` rows.
Window: ts 1785582508231 .. 1789797630549 (~49 days). Data had not
changed since PR5d's own review session -- confirmed by comparing row
counts and min/max timestamps before pulling full data, and confirmed
after by an exact match against PR5d's previously reported winner
counts (see regression check below).

### Winner distribution (6A) vs. matched distribution (6B)

12h (n_evaluable=211 of 391 resolved -- the rest lack V1 context):

| Category | Winner count (6A) | Matched count (6B) |
|---|---|---|
| UNEXPECTED_SHOCK | 24 | 36 |
| REGIME_CHANGE | 0 | 15 |
| MISSING_EVENT | 3 | 29 |
| MISLEADING_SENTIMENT | 0 | 15 |
| TECHNICAL_SENTIMENT_CONFLICT | 24 | 91 |
| STALE_SENTIMENT | 7 | 21 |

24h (n_evaluable=311 of 673 resolved):

| Category | Winner count (6A) | Matched count (6B) |
|---|---|---|
| UNEXPECTED_SHOCK | 68 | 116 |
| REGIME_CHANGE | 0 | 37 |
| MISSING_EVENT | 48 | 117 |
| MISLEADING_SENTIMENT | 0 | 37 |
| TECHNICAL_SENTIMENT_CONFLICT | 13 | 152 |
| STALE_SENTIMENT | 3 | 31 |

The gap between the two columns is the suppression effect: every
category's matched count is >= its winner count, often by a large
margin (`TECHNICAL_SENTIMENT_CONFLICT` matches 91-152 times but wins
only 13-24 of them).

### Suppression analysis (reconciles exactly: matched = winner + suppressed)

24h, all six categories (12h shows the same pattern, smaller n):

| Category | Matched | Winner | Suppressed | Rate | Suppressed by |
|---|---|---|---|---|---|
| UNEXPECTED_SHOCK | 116 | 68 | 48 | 41% | CORRECT_DIRECTION_WRONG_MAGNITUDE (43), no-error (5) |
| REGIME_CHANGE | 37 | 0 | 37 | 100% | UNEXPECTED_SHOCK (21), CORRECT_DIRECTION_WRONG_MAGNITUDE (16) |
| MISSING_EVENT | 117 | 48 | 69 | 59% | CORRECT_DIRECTION_WRONG_MAGNITUDE (34), UNEXPECTED_SHOCK (24), no-error (11) |
| MISLEADING_SENTIMENT | 37 | 0 | 37 | 100% | UNEXPECTED_SHOCK (21), CORRECT_DIRECTION_WRONG_MAGNITUDE (16) |
| TECHNICAL_SENTIMENT_CONFLICT | 152 | 13 | 139 | 91% | UNEXPECTED_SHOCK (47), CORRECT_DIRECTION_WRONG_MAGNITUDE (42), MISSING_EVENT (37), no-error (13) |
| STALE_SENTIMENT | 31 | 3 | 28 | 90% | UNEXPECTED_SHOCK (9), CORRECT_DIRECTION_WRONG_MAGNITUDE (6), TECHNICAL_SENTIMENT_CONFLICT (4), MISSING_EVENT (1), no-error (8) |

Note `suppressed_by_winner` includes `CORRECT_DIRECTION_WRONG_MAGNITUDE`
and "no-error" (`None`) as winners in several rows: a row's V1/technical
conflict or staleness gap can be present even when the *direction was
correct* (these signals are computed regardless of direction
correctness) -- in that case there is no "wrong direction" branch to
compete in at all, so the row's true winner is the direction-correct
outcome, and the matched-but-unused category is suppressed by that
outcome, not by another wrong-direction category. This is reported
exactly as computed, not smoothed into the wrong-direction-only story.

### Specific overlap findings (Section 5, both horizons show the same qualitative pattern; 24h shown)

- **UNEXPECTED_SHOCK + REGIME_CHANGE**: count_both=37 of 37 REGIME_CHANGE
  matches (100% of REGIME_CHANGE's matches also have UNEXPECTED_SHOCK).
- **UNEXPECTED_SHOCK + MISLEADING_SENTIMENT**: count_both=37 of 37 (100%,
  identical pattern).
- **REGIME_CHANGE + MISSING_EVENT**: count_both=0 -- these two NEVER
  co-occur in this sample (a `REGIME_REVERSAL` event and a
  `VOLATILITY_EXPANSION` event never overlapped the same prediction's
  window).
- **MISLEADING_SENTIMENT + TECHNICAL_SENTIMENT_CONFLICT**: count_both=25
  of 37 MISLEADING_SENTIMENT matches (68%).
- **STALE_SENTIMENT + TECHNICAL_SENTIMENT_CONFLICT**: count_both=15 of 31
  STALE_SENTIMENT matches (48%).
- A striking, unrequested-but-observed finding: **REGIME_CHANGE and
  MISLEADING_SENTIMENT co-occur with each other 100% of the time in
  both directions** (`count_both` == `count_a` == `count_b` == 37 at
  24h, 15 at 12h) -- every `REGIME_REVERSAL` event in this window's
  detected set coincides with a `V1_BTC_DIVERGENCE` event and vice
  versa. Reported as an observation only; the underlying cause (only 3
  distinct `REGIME_REVERSAL` timestamps and 9 `V1_BTC_DIVERGENCE`
  events sharing one single daily-resampled timestamp, per PR3's own
  frozen resampling logic) is a data-shape fact, not a claim about why.

### Is REGIME_CHANGE/MISLEADING_SENTIMENT's absence explained by overlap?

Yes, directly and completely, at both horizons: `fully_suppressed=True`
for both categories at both 12h and 24h -- every single one of their
15-37 matches was outranked by a higher-priority winner
(`UNEXPECTED_SHOCK` and, for direction-correct rows,
`CORRECT_DIRECTION_WRONG_MAGNITUDE`/no-error), never by an absence of
the underlying signal itself. `winner=0` in both cases is fully
accounted for by `suppressed=matched` -- not a detection gap.

### Regression check (Section: PR5d results unchanged)

`report["per_horizon"][h]["winner_distribution"]` was compared field-
for-field against `ec.build_error_classification_report()`'s own
`overall` breakdown for the same window, for both horizons -- exact
match. PR5d's own classification results are provably unaffected by
this module's existence.

### Whether suppression is substantial or limited (measurement, not a recommendation)

Substantial, by the numbers above: `TECHNICAL_SENTIMENT_CONFLICT` is
suppressed 74-91% of the time it matches; `REGIME_CHANGE` and
`MISLEADING_SENTIMENT` are suppressed 100% of the time. This is stated
as a measurement only. **This PR does not recommend changing the
priority chain** -- per the build authorization, any such change is a
separate, explicitly human-reviewed decision. If a taxonomy redesign is
warranted, it is a FOLLOW-UP HYPOTHESIS for a human to evaluate, not an
action this PR takes.

### Limitations

- n_evaluable (211/395 at 12h, 311/675 at 24h) is smaller than PR5d's
  own n_resolved, because rows with no V1 context are additionally
  excluded here (see Scope above) -- roughly half the resolved
  population is outside this measurement's reach, not just the ~1/3
  PR5d itself flagged for `INSUFFICIENT_INFORMATION`.
- The 100% co-occurrence of REGIME_CHANGE/MISLEADING_SENTIMENT is
  observed in a small-n, single ~49-day window with only 3 distinct
  REGIME_REVERSAL timestamps -- whether this is a lasting structural
  property or an artifact of this specific sample is not established
  by this PR.
- Percentages in the co-occurrence matrix (`pct_of_a_containing_b` etc.)
  are descriptive ratios over a small evaluable sample, not
  statistical estimates with a confidence interval -- no significance
  claim is made or implied anywhere in this module.

## PR5e: research hypothesis & evidence gate

Scope: turns the already-computed, already-reviewed outputs of PR5c,
PR5d, and PR5d-followup into explicit, persistent, auditable
hypothesis records, gated through a fixed, deterministic pipeline. No
V1/V2/Worker/PR3/PR5d-classifier change, no new production table, no
production write of any kind (`persist_hypothesis()` is exercised only
against an in-memory sqlite3 fixture in this session). PR5e is an
**evidence gate, not a discovery generator**: it never asserts a
hypothesis is true, only how far it has advanced through the gates.

### Lifecycle (reused verbatim, not reinvented)

`OBSERVATION -> MONITOR -> RESEARCH_HYPOTHESIS -> VALIDATION_READY ->
BUILD_REQUEST -> AWAITING_APPROVAL -> IMPLEMENTED -> VALIDATED ->
REJECTED -> ROLLED_BACK`, exactly PR5a's own migration-0008 documented
lifecycle. This PR structurally never assigns past `BUILD_REQUEST` --
`AWAITING_APPROVAL`/`IMPLEMENTED`/`VALIDATED`/`ROLLED_BACK` all require
a real human decision and/or deployment, outside this PR's scope
(enforced by `test_never_assigns_beyond_build_request`).

### Evidence gates 0-6

| Gate | Question | Source candidates | Taxonomy candidates |
|---|---|---|---|
| 0 Observation | Does a measurable sample exist? | `level2.status=="OK"` and `n>0` | `n_resolved>0` |
| 1 Repeatability | Does the pattern reappear independently? | `>=2` horizons for the same source at STATISTICALLY_SIGNIFICANT/STABLE | PR5d's own recurring-across-dimensions flag |
| 2 Association | Is there a measurable relationship? | PR5c's `classify_evidence()` label (SIGNIFICANT or STABLE) | concentrated share exceeds baseline by >=`STABILITY_EPSILON` |
| 3 Incremental value (related to Gate 2, NOT independent -- see below) | Beyond the V1 composite / baseline? | discovery-half **partial correlation** \|r\|>=`STABILITY_EPSILON` | effect exceeds baseline by >=`TAXONOMY_INCREMENTAL_MULTIPLIER * STABILITY_EPSILON` (2x Gate 2's bar) |
| 4 Out-of-sample validation (the principal held-out gate) | Does it hold on unseen data, with a MEANINGFUL, REPLICATING effect? | validation-half rmse_reduction_pct >= 5% AND the SAME direction replicates across two chronological sub-windows of that validation half | chronological holdout requiring BOTH halves to clear `STABILITY_EPSILON` above baseline (already combines magnitude + stability, unchanged in v2) |
| 5 Significance eligibility (a STRICTER RE-READ of Gate 2, not independent evidence) | Is the association specifically STATISTICALLY_SIGNIFICANT (not merely STABLE)? | `evidence_status == "STATISTICALLY_SIGNIFICANT"` | same |
| 6 Build-request eligible | Gates 0-5 passed, evidence_type != EXPLANATORY, AND redundancy status != `REDUNDANCY_UNRESOLVED` | | |

Every threshold above is reused from PR5c's own already-justified
constants (`MIN_SAMPLE_FOR_LEVEL2`, `STABILITY_EPSILON`,
`OOS_SPLIT_FRACTION`, `MIN_SAMPLE_FOR_CONTRADICTED`) or is that same
constant doubled/rescaled with the rationale stated inline
(`TAXONOMY_INCREMENTAL_MULTIPLIER = 2`, `MEANINGFUL_OOS_IMPROVEMENT_PCT
= STABILITY_EPSILON * 100`) -- no new arbitrary number is introduced.
Where the available data cannot support a gate, the gate returns
`passed: False` with an explicit `INSUFFICIENT_DATA`/
`INSUFFICIENT_DATA_FOR_HOLDOUT` reason rather than forcing a decision.

**On reusing `STABILITY_EPSILON`'s numeral, precisely stated**:
`STABILITY_EPSILON` (0.05) was originally defined in
`source_analysis.py` (Section 12) as *"|r| below this is treated as 'no
material effect' when checking whether a non-significant or reversed-
sign result is 'stable'/'contradicted' vs. merely noise"* -- a
**correlation-scale evidence-classification floor**, used to decide
whether a result is labeled stable/contradicted vs. inconclusive. It
was never an economically- or predictively-calibrated minimum effect
size. This PR reuses its *numeral* three further times: on a
correlation scale again (Gate 3's partial correlation), on a share-
difference scale (taxonomy Gates 2/3), and on a **percentage RMSE-
reduction scale** (Gate 4's `MEANINGFUL_OOS_IMPROVEMENT_PCT = 5%`). Each
reuse is a deliberate, documented **convention** to avoid inventing an
unjustified new number -- **not a claim that these are statistically
equivalent uses**, and no formal transformation connects a 0.05
correlation-stability floor to a 5%-RMSE-reduction economic-effect
floor. A future PR could replace Gate 4's floor with a genuinely
economically-derived one (e.g. tied to realized trading costs/slippage)
if such a figure becomes available; until then, this is the most
defensible reuse available from already-approved constants, not
evidence that 5% is itself a statistically optimal cutoff.

### v1 gate-independence bug, and the deeper v2 finding that followed

Before PR #53 was first opened, an earlier draft was run against the
real production snapshot and produced **38 of 105 source candidates
reaching BUILD_REQUEST**. Root cause: Gates 3 and 4 both tested the
identical field (`level3.oos.status=="IMPROVED"`), and taxonomy's Gate
3 duplicated Gate 2. Fixed pre-review by making Gate 3 read the
discovery-half partial correlation instead, and adding a Gate 5
significance requirement -- re-running the snapshot produced 23/105.

**A second, independent review went further** and asked whether the
"fixed" gates 2-5 were actually measuring different things, or just
different representations of the same PR5c statistic. Re-running the
diagnostics on the reviewed code found:

- `corr(Gate 2's r_full, Gate 3's partial_r) = 0.944` across the 105
  candidates -- Gate 3 is NOT materially independent of Gate 2 in this
  dataset (the discovery half is 70% of the same series as the full
  sample, and the V1 composite rarely explains much of the outcome at
  these horizons, so partialling it out removes little).
- The original Gate 4 accepted ANY positive `rmse_reduction_pct`,
  observed as low as **0.0086%** -- plausibly noise on a ~135-160 row
  validation half.
- The old Gate 5 re-read Gate 2's own significance flag and called it
  "build-request eligible," implying independent confirmation it did
  not have.
- `NOT_REDUNDANT_OBSERVED` (the old name) could be misread as
  "independence established" -- concretely, `global` and `gold`
  correlate at **r=-0.624** in the snapshot, comfortably under the 0.7
  pairwise-redundancy threshold, so both carried that label without it
  ruling out their shared information.

**v2 response** (architecture unchanged; gates 3-6 strengthened and
relabeled -- see `hypothesis_gate.py`'s module docstring, section "v2
REVISION," for the complete write-up):
- Gate 3 now explicitly carries `independent_of_gate2: False` and a
  note on every result -- reported for completeness, never treated as
  corroborating Gate 2.
- Gate 4 now requires BOTH a >=5% validation-half RMSE reduction
  (`MEANINGFUL_OOS_IMPROVEMENT_PCT`, PR5c's own `STABILITY_EPSILON`
  reused on a percentage scale) AND replication of the same improvement
  direction across two non-overlapping chronological sub-windows of the
  validation half (`source_subsplit_stability()`, reusing the same
  "sign_stable" pattern PR5c's own `level2_association_for_source()`
  already uses one level up). A positive-but-insufficient point
  estimate is now `NOT_REPLICATED` -- not evidence against the
  hypothesis, just not enough to confirm it; it does not demote to
  REJECTED, but it cannot reach BUILD_REQUEST either. A clearly negative
  point estimate is still `FAILED_HOLDOUT` -> REJECTED, unchanged.
- Gate 5 is split out and renamed `gate5_significance_eligibility()` --
  explicitly documented as a stricter re-read of Gate 2's own metric,
  never independent evidence.
- Gate 6 (new) is the actual BUILD_REQUEST gate: gates 0-5 + evidence
  type + **redundancy status**. A candidate whose `source_redundancy_note`
  is `REDUNDANCY_UNRESOLVED` can no longer reach BUILD_REQUEST even if
  every other gate passes.
- Redundancy labels renamed: `UNKNOWN_STRONG_REDUNDANCY_PRESENT` ->
  `REDUNDANCY_UNRESOLVED`; `NOT_REDUNDANT_OBSERVED` ->
  `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED` (states precisely and only
  what was checked -- no single pairwise/vs-composite \|r\| >= 0.7 --
  never a claim of independence).
- New `signal_family_summary` and `gate_funnel` report fields (see
  "Production snapshot" below) so candidate count is never mistaken for
  independent-discovery count, and no candidate's fate is hidden.

Re-running the same snapshot after the v2 changes: **7 of 105 source
candidates reach BUILD_REQUEST**, across **3 distinct source families**
(down from 23 candidates / effectively the same underlying signals
under v1's looser gates). See "Production snapshot" below for the full
funnel, OOS distribution, and exact candidates.

### Temporal safety

Gates 0-3 and 5-6 consume only already-computed, already-reviewed
report fields (PR5c's as-of joins, PR5d's event-window-overlap checks,
PR5d-followup's `contributing_signals` reads) -- no new query against
predictions/history/btc_data beyond what source_analysis.py's own
report already issues for the same source/horizon. Gate 4's v2
`source_subsplit_stability()` and taxonomy's
`chronological_holdout_for_taxonomy_candidate()` are the two genuinely
new computations in this module; both are built entirely from PR5c's
own public primitives (`extract_source_matrix`,
`outcome_engine.compute_forward_returns_from_history`,
`stats_utils.ols_1var`/`ols_2var`/`rmse` for the former;
`error_classification`'s own `breakdown_by_*()` for the latter) over a
chronologically-sorted (never shuffled) split of the SAME already-
fetched rows -- no new statistical technique, no reshuffled data.
Post-event-only evidence (PR3's `MISLEADING_SENTIMENT`/
`V1_BTC_DIVERGENCE`, always `is_post_event_analysis=1`) is forced
`evidence_type="EXPLANATORY"` and Gate 6 hard-requires `evidence_type
!= "EXPLANATORY"` -- it can reach RESEARCH_HYPOTHESIS/VALIDATION_READY
but never BUILD_REQUEST.

### Six-part decomposition, never collapsed

Every evaluated candidate carries `observation`, `association`,
`incremental_information`, `hypothesis_statement`, `evidence_status`,
and `validation_status` as distinct fields (plus `lifecycle_status`,
`evidence_type`, `source_redundancy_note`, and the full `gate_results`
for audit). `winner_label`/`all_matched_categories`/`suppressed_categories`
from PR5d-followup are preserved verbatim inside
`observation.detail`, never collapsed into a single flag.

### Two different "validation" concepts

`lifecycle_status` (the ten-stage column) includes a stage literally
named `VALIDATED`, but that means "validated in production after being
implemented." This PR's own `validation_status`
(`NOT_YET_TESTED`/`PASSED_HOLDOUT`/`NOT_REPLICATED`/`FAILED_HOLDOUT`/`INSUFFICIENT_DATA_FOR_HOLDOUT`)
is a separate, pre-implementation concept, stored in the reused
`out_of_sample_status` column, and is never a synonym for the
lifecycle's own later `VALIDATED` stage. `NOT_REPLICATED` (v2 addition)
means a positive point estimate that did not clear the meaningful-
effect floor and/or did not replicate across both validation sub-
windows -- not evidence against the hypothesis, so it does NOT demote
to REJECTED, it simply cannot reach BUILD_REQUEST. A `FAILED_HOLDOUT`
candidate (a genuinely negative point estimate) is still demoted
straight to `lifecycle_status="REJECTED"`.

### Source redundancy caveat (terminology corrected in v2)

`source_redundancy_note()` never reports a source candidate as cleared
"beyond its correlated group" -- only `REDUNDANCY_UNRESOLVED` (a
\|r\|>=0.7 partner exists per PR5c's own pairwise/vs-composite
redundancy output; renamed from `UNKNOWN_STRONG_REDUNDANCY_PRESENT` --
this is a specific, confirmed unresolved overlap, not merely
"unknown") or `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED` (renamed from
`NOT_REDUNDANT_OBSERVED`; states precisely and only that no single
pairwise/vs-composite Pearson \|r\| crossed 0.7 -- **never** a claim of
independence). Concrete counter-example kept in both the module and
here so it is never re-forgotten: `global` and `gold` correlate at
**r=-0.624** in the production snapshot, well below the 0.7 threshold,
so both could carry `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED`
simultaneously without that ruling out their shared information. As of
v2, `REDUNDANCY_UNRESOLVED` also **blocks Gate 6 (BUILD_REQUEST)
outright** -- a candidate with strong association, incremental value,
and a validated OOS effect still caps at VALIDATION_READY if its
redundancy is unresolved (this removed `etfflows` from the final
BUILD_REQUEST list in the production snapshot). Every BUILD_REQUEST
candidate still carries this note inside `known_confounders`, visible,
never hidden.

### Horizon overlap: candidate count != independent-signal-family count

Adjacent horizons for the SAME source share overlapping forward-return
windows and are not independent confirmations of each other. v2 adds
`signal_family_summary` to `build_hypothesis_report()`'s output,
grouping `SOURCE_INCREMENTAL_INFO` candidates by `source_key` (the
natural, already-existing grouping unit -- no new clustering algorithm)
so a reader never mistakes "N BUILD_REQUEST candidates" for "N
independent discoveries." In the production snapshot: 7 BUILD_REQUEST
candidates reduce to 3 distinct source families (`fng`, `global`,
`onchain`).

**This grouping is an audit/reporting dimension only -- it does not
gate anything and must not be read as "3 independent opportunities."**
`signal_family_summary` is informational, exactly like
`source_redundancy_note`: it tells a human reviewer how to weigh the
BUILD_REQUEST list (3 underlying signals worth investigating, not 7),
it does not filter, deduplicate, merge, or otherwise act on the
candidates themselves. No downstream code treats "3 families" as a
smaller or different set of BUILD_REQUEST candidates than the 7 that
were actually produced -- `build_requests` still contains all 7, each
independently auditable and independently subject to whatever human
review follows. Collapsing horizons into "one candidate per family"
would itself require a modeling choice (which horizon to keep, how to
combine evidence across them) this PR deliberately does not make.

### Schema reuse -- no migration proposed

`research_hypotheses` (PR5a, migration 0008, still not deployed to
production -- confirmed by direct read-only `sqlite_master` check this
session, same as every prior PR5 round) already has every column this
PR needs: `subject`/`statement` (hypothesis identity), `status` (the
ten-stage lifecycle), `out_of_sample_status` (this PR's
`validation_status`), `source_analysis_ids` (traceability -- honestly
empty for this snapshot, since production `research_analyses` has zero
rows), and `evidence_summary_json` (the full six-part decomposition +
gate results, never an opaque free-text blurb). No schema change is
proposed; per the build authorization, this PR does not apply the
migration.

### BUILD_REQUEST semantics -- human review candidate, not "validated"

`build_build_request_candidate()` only ever runs on a
`lifecycle_status=="BUILD_REQUEST"` record and assembles the exact
hypothesis, affected component (always `"research_only"` -- this PR
never proposes a V1/V2 change directly), association evidence,
incremental evidence, OOS effect and OOS stability evidence,
significance eligibility, redundancy status, sample size, time window,
validation method, known confounders, overlap/redundancy
considerations, expected measurable effect, an explicit
`why_it_passed_the_promotion_gate` narrative, a `known_limitations`
list (repeating the Gate 3 non-independence and redundancy-terminology
caveats on every single BUILD_REQUEST record, not just here), a
hardcoded `proposed_change_description` of `"NONE"`, and
`human_approval_required: True`. **BUILD_REQUEST means "candidate
worthy of human review for a possible future implementation" -- it
does NOT mean "validated."** A BUILD_REQUEST is not permission to
modify production; this PR implements no V1/V2 change.

### Production snapshot

Same production D1 (`sentiment-history`), pulled fresh, read-only:
1070 `predictions` rows (unchanged since the PR5d-followup review),
499 `history` rows, 2095 `btc_data` rows. Prediction window: ts
1785582508231 .. 1789797630549 (~49 days, unchanged). **Difference from
the earlier PR5e snapshot**: `history`'s ts range shifted to
1786866679664 .. 1789816132898 and its row count moved from 500 to 499
-- rolling retention continues to age out the earliest rows and append
new ones between pulls. This does not change the analysis window
(`start_ts`/`end_ts` are still derived from `predictions`, untouched by
`history`'s retention), and is recorded here per the build
authorization's "do not silently change the analysis window" /
"identify any difference from the previous snapshot."

Results (`source_horizons=(1,3,6,12,24)`, `horizons=(12,24)`):

- 105 source candidates (21 sources x 5 horizons), 4 taxonomy
  candidates -- 109 total.
- **Gate funnel** (candidates PASSING each gate, out of 109):

  | Gate | 0 Obs. | 1 Repeat. | 2 Assoc. | 3 Increm. | 4 OOS (strengthened) | 5 Signif. elig. | 6 Redundancy/type | BUILD_REQUEST |
  |---|---|---|---|---|---|---|---|---|
  | Count | 109 | 80 | 56 | 51 | 8 | 8 | 7 | 7 |

  Gate 4 (the strengthened OOS check) is where almost all of the
  filtering now happens: 51 candidates had a positive-and-large-enough-
  looking association at Gate 3, but only 8 replicate a meaningful
  effect across both chronological validation sub-windows. Gate 6
  (redundancy) removes exactly 1 more (`etfflows`, `REDUNDANCY_UNRESOLVED`
  against the V1 composite at r=0.75).
- `lifecycle_status_counts`: `OBSERVATION=5`, `MONITOR=1`,
  `RESEARCH_HYPOTHESIS=6`, `VALIDATION_READY=35`, `BUILD_REQUEST=7`,
  `REJECTED=55`.
- **OOS distribution** (`oos_distribution`, all 105 source candidates
  with an OK Level 3 result): min=-27.57%, q1=-2.51%, median=-0.08%,
  q3=1.01%, max=16.21%; 55 candidates <=0%, 50 candidates >0%, 10 clear
  the 5% meaningful-effect floor, and of those, 8 also replicate across
  both validation sub-windows (`count_passing_stability_and_magnitude`).
  The 40 positive-but-`NOT_REPLICATED` point estimates range from
  0.0086% to 9.04% -- exactly the sub-5%-or-unstable population the v2
  Gate 4 change was designed to exclude from BUILD_REQUEST while still
  reporting honestly (none of these are claimed as evidence against the
  hypothesis).
- **7 BUILD_REQUEST candidates**, all `SOURCE_INCREMENTAL_INFO` (0
  taxonomy candidates reach BUILD_REQUEST -- all 4 taxonomy candidates
  show `evidence_status=INCONCLUSIVE`, which Gate 5 excludes by
  construction): `fng` at 6h/12h/24h, `global` at 6h/12h/24h, `onchain`
  at 24h. All 7 carry `redundancy_status=NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED`
  (never a claim of independence -- see caveat above).
- **`signal_family_summary`**: 7 BUILD_REQUEST candidates reduce to
  **3 distinct source families** (`fng`, `global`, `onchain`) --
  candidate count is explicitly not independent-discovery count.
- Re-running `build_hypothesis_report()` twice against the identical
  snapshot produced an identical report (`report_1 == report_2`),
  confirmed both in `test_deterministic_rerun_identical_report` and
  against this real snapshot.

### Known limitations

- **Horizon overlap is not corrected for.** All 7 BUILD_REQUEST
  candidates are 3 sources appearing at 2-3 adjacent horizons each
  (`fng` at 6h/12h/24h, `global` at 6h/12h/24h). Forward-return windows
  across adjacent horizons overlap substantially and are highly
  autocorrelated, so a pattern at one horizon is likely to reappear at
  the next for that reason alone. Neither Gate 1 nor PR5c's own
  Benjamini-Hochberg correction models this -- an inherited property of
  PR5c's already-merged methodology, not a new bug in this PR, and this
  PR does not invent a new correction for it (see
  `signal_family_summary` above for the corrected framing: **3
  independent-ish signal families, not 7**).
- **Gate 3 is reported, not treated as confirmatory.** `corr(Gate 2's
  r_full, Gate 3's partial_r) = 0.944` across the 105 candidates --
  passing Gate 3 must never be read as independent corroboration of
  Gate 2 (see `independent_of_gate2: False` on every Gate 3 result).
- **Gate 4's sub-split stability check only verifies SIGN, not
  magnitude, in each sub-window.** `onchain:24h`'s second validation
  sub-window shows only a 0.24% RMSE reduction (barely positive) while
  its first sub-window shows 34.56% -- the candidate still passes
  because both are positive, even though the two sub-windows disagree
  sharply on magnitude. A future PR could tighten this further (e.g.
  requiring both sub-windows to individually clear some fraction of the
  meaningful-effect floor); this PR does not invent that additional
  threshold without further data-driven justification, per the "do not
  invent a threshold merely to shrink the count" instruction.
- `chronological_holdout_for_taxonomy_candidate()` only supports 3 of
  PR5d's 5 breakdown dimensions (`v1_composite_bucket`, `regime`,
  `model_version`); `chronological_period` and `confidence_band`
  candidates return `INSUFFICIENT_DATA_FOR_HOLDOUT` with an explicit
  reason rather than a hastily-added breakdown.
- A taxonomy candidate can reach `lifecycle_status="VALIDATION_READY"`
  while its own `evidence_status` is `INCONCLUSIVE` (this snapshot's 4
  taxonomy candidates all do): Gate 1 (recurrence across breakdown
  dimensions) drives `evidence_status`, while Gates 3/4 (which do not
  depend on Gate 1) drive `lifecycle_status`. This is intentional --
  the two fields answer different questions and are never collapsed --
  but `lifecycle_status` alone should never be read as a stand-in for
  `evidence_status`; Gate 6 already requires both before BUILD_REQUEST.
- A candidate whose `first_failed_gate` is an early gate (e.g. gate1)
  can still end up `lifecycle_status="REJECTED"` rather than
  `"OBSERVATION"`, because Gate 4 is always evaluated regardless of
  earlier failures and a clearly negative OOS point estimate overrides
  the ladder position (explicit negative evidence is a stronger verdict
  than "insufficient repeatability" alone) -- `first_failed_gate` still
  correctly names the first thing that went wrong; `lifecycle_status`
  separately reports the strongest available verdict. See
  `assign_lifecycle_status()`'s docstring.
- `research_hypotheses.source_analysis_ids` is honestly empty for
  every hypothesis persisted from this snapshot, because production
  `research_analyses` (PR1's own table) has zero rows to reference --
  not a defect in this PR, a fact about what has and has not been
  persisted upstream so far.

## PR5f: source-family discrimination

Scope: for each of PR5e's surviving BUILD_REQUEST source families
(`fng`, `global`, `onchain`), determines whether it carries incremental
information BEYOND the V1 composite **AND** the other surviving
families jointly -- the exact question PR5e's own `source_
redundancy_note()` could only flag as unresolved. Entirely read-only,
no persistence function exists in this module, no V1/V2/Worker/PR5e
change of any kind. PR5e's own hypothesis records are read and reported
side by side, never modified or re-persisted.

### Why this is not circular with PR5e

PR5e's Gate 3/4 condition on a control set of size 1 (the V1 composite
only). This PR conditions on a STRICTLY LARGER set (composite + the
other 2 surviving families) for the same outcome. Adding controls to a
nested-OLS comparison can only reduce or leave unchanged a variable's
apparent incremental contribution, never inflate it -- so a "survives"
verdict here is strictly stronger evidence than PR5e's own Gate 4
verdict, not a re-run with the same possible outcome. The production
snapshot below demonstrates this concretely: two of the three families
that passed PR5e's composite-only test do NOT survive this stricter one.

### Methodology

1. Reads a FRESH `hypothesis_gate.build_hypothesis_report()` (PR5e's
   own, completely unmodified) to find the CURRENT surviving BUILD_REQUEST
   families -- never hardcoded, so this stays correct if a future
   snapshot's surviving set differs.
2. Selects exactly ONE primary horizon per family: the horizon (among
   that family's own PR5e BUILD_REQUEST horizons) with the largest
   `oos_validation.rmse_reduction_pct` -- a deterministic criterion
   from PR5e's own already-computed numbers, never cherry-picked. The
   family's other horizons are reported separately as explicit,
   non-gating CONTEXT ONLY (never independent confirmations -- they
   overlap the primary horizon's forward-return window).
3. Reports (never gates on) the exact pairwise Pearson correlation
   between every pair of surviving families (PR5c's own `pairwise_
   source_redundancy()`, unmodified).
4. Reports a higher-order PARTIAL correlation of (family, outcome)
   controlling for {composite, other family A, other family B} --
   computed via the standard recursive partial-correlation identity
   using ONLY pairwise Pearson correlations and PR5c's own single-
   control `partial_correlation()`, applied repeatedly. Verified to
   match the equivalent multiple-regression-residual computation to
   machine precision (`test_higher_order_partial_correlation_matches_
   regression_residual_method`). Reported for completeness, exactly
   like PR5e's own Gate 3 -- NOT independent confirmation of the OOS
   result below.
5. **The principal held-out test**: a nested OLS comparison generalized
   from PR5e's own v2 Gate 4 to a multi-predictor baseline. Baseline =
   composite + other family A + other family B; full = baseline +
   target family. Both fit on the discovery half (chronological, never
   shuffled, `OOS_SPLIT_FRACTION=0.7`), RMSE-compared on the untouched
   validation half. Uses a new general `stats_utils.ols_nvar()`
   (validated against `ols_2var` for the 2-predictor case, and against
   an exact hyperplane recovery for 4 predictors). **Reuses PR5e's own
   v2 promotion criterion verbatim, no new threshold**: validation-half
   RMSE reduction >= `MEANINGFUL_OOS_IMPROVEMENT_PCT` (5%) AND the same
   direction replicates across two chronological sub-windows of the
   validation half (`family_subsplit_stability()`, the direct
   multi-predictor generalization of PR5e's own `source_subsplit_
   stability()`).

### Discrimination status -- reuses PR5e's vocabulary, adds one rollup

`validation_status` is `hypothesis_gate.VALIDATION_STATUSES`, imported
and reused verbatim. This PR adds exactly one new, narrow rollup:
`discrimination_status` = `DISCRIMINATED_INCREMENTAL` (validation_status
== `PASSED_HOLDOUT`), `NOT_DISCRIMINATED` (`NOT_REPLICATED` or
`FAILED_HOLDOUT`), or `INSUFFICIENT_DATA` (`INSUFFICIENT_DATA_FOR_HOLDOUT`).
**`DISCRIMINATED_INCREMENTAL` is NOT a promotion and NOT `hypothesis_
gate`'s "VALIDATED" lifecycle stage** (which means validated in
production after implementation, per PR5a's own schema, entirely
outside this PR's scope) -- it answers a narrower question than PR5e's
own `lifecycle_status` ("worthy of human review") and is reported
side by side with it, never merging or overriding PR5e's own values.

### Production snapshot

Extracted 2026-09-19 (extraction_ts recorded per run). Same production
D1 (`sentiment-history`), same window as PR5e's own (`predictions`
table itself is byte-for-byte unchanged: 1070 rows, identical min/max
ts -- the window derived from it is therefore identical, not silently
changed). **Differences from PR5e's recorded snapshot**: `history`
499->500 raw rows, `btc_data` 2095->2099 rows -- both tables' rolling
retention/accumulation continuing to advance past the fixed prediction
window, exactly the kind of drift PR5e's own README already documented
between its own two snapshots. `surviving_families` unchanged: `fng`,
`global`, `onchain`. Note: `history`'s raw table count (500) is not the
same as the number of `history` rows that fall INSIDE the analysis
window bounded by `predictions`' own min/max ts -- only 495 do; the
`extract_source_matrix()`/`pairwise_family_correlations()`/complete-case
counts below all use this windowed 495, not the raw 500.

Primary horizons selected (by PR5e's own largest `rmse_reduction_pct`):
`fng`->24h, `global`->12h, `onchain`->24h.

Exact discovery/validation periods for the 257-row complete-case
sample (identical timestamps across all three families' evaluations,
confirmed by direct set comparison): discovery n=179,
2026-08-16T08:06:47Z .. 2026-09-08T04:50:16Z; validation n=78,
2026-09-08T07:51:24Z .. 2026-09-19T02:48:33Z (sub-window 1: n=39,
through 2026-09-14T06:01:19Z; sub-window 2: n=39, from
2026-09-14T08:55:05Z).

Pairwise family correlations (n~257-408, none anywhere near the 0.7
strong-redundancy threshold, yet see the joint-model results below):

| Pair | r | n |
|---|---|---|
| fng / global | 0.187 | 286 |
| fng / onchain | -0.125 | 408 |
| global / onchain | 0.166 | 286 |

**Joint-control discrimination results** (n=257 for all three, same
combined sample since all three families' values are required
simultaneously):

| Family | Controls | rmse_reduction_pct | Sub1 / Sub2 | Higher-order partial r | validation_status | discrimination_status |
|---|---|---|---|---|---|---|
| `fng` (24h) | composite, global, onchain | **+10.99%** | +5.92% / +13.04% (stable) | -0.262 | PASSED_HOLDOUT | **DISCRIMINATED_INCREMENTAL** |
| `global` (12h) | composite, fng, onchain | **-4.88%** | -15.72% / -2.28% (unstable, both negative) | -0.373 | FAILED_HOLDOUT | NOT_DISCRIMINATED |
| `onchain` (24h) | composite, fng, global | +4.93% | +7.63% / +3.63% (stable, but < 5% floor) | -0.190 | NOT_REPLICATED | NOT_DISCRIMINATED |

**Only 1 of 3 surviving PR5e families (`fng`) is `DISCRIMINATED_INCREMENTAL`.**
`global`'s marginal contribution turns NEGATIVE once `fng` and `onchain`
are already in the model -- despite a merely moderate pairwise
correlation with `fng` (r=0.187, far below the 0.7 "strong redundancy"
threshold PR5e's own `source_redundancy_note()` checks). `onchain`'s
effect is directionally consistent (both sub-windows positive) but
falls just under the 5% meaningful-effect floor once the other two
families are controlled for. This is the concrete demonstration of
this PR's whole premise: PR5e's `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED`
label (all three pairs here carry it) never meant "no shared
information" -- even modest, sub-threshold correlations can fully
explain away another candidate's OOS improvement once tested jointly.

Determinism: re-running `build_family_discrimination_report()` twice
against the identical snapshot produced an identical report.

### Independent audit findings (post-open review of PR #54)

A line-by-line + numerical audit cross-checked every piece of new
statistical machinery against independently-coded reference methods,
run on the real production data (not just synthetic test fixtures):
`ols_nvar()`'s coefficients matched both a cofactor-expansion Cramer's
rule implementation and a Frisch-Waugh-Lovell sequential-residualization
computation (via repeated `ols_1var`) to ~1e-12/1e-15 precision;
`higher_order_partial_correlation()` matched the equivalent multiple-
regression-residual-correlation method to 1e-16 precision; the reported
`rmse_reduction_pct` for `fng` was reproduced from scratch outside the
module's own code path with a diff of exactly 0.0. **No logic defect
was found.** The audit did surface two things worth knowing before
trusting a result at face value:

- **`fng`'s DISCRIMINATED_INCREMENTAL status is horizon-specific, not a
  horizon-independent property of the family.** Re-running the SAME
  joint-control test at `fng`'s other PR5e horizons (not just the
  selected primary one) found: 6h=+1.55% (NOT_DISCRIMINATED),
  12h=+4.40% (NOT_DISCRIMINATED), 24h=+10.99% (DISCRIMINATED_INCREMENTAL
  -- the one `select_primary_horizon_per_family()` picked, because it
  also had PR5e's own largest composite-only effect). Because the
  selection criterion and the confirmation test are not statistically
  independent of each other, "fng carries incremental information" is
  a narrower, horizon-qualified claim than it might first appear --
  though the roughly monotonic 1.55%->4.40%->10.99% trend with
  increasing horizon length is at least consistent with a genuine,
  horizon-dependent effect rather than pure noise. By contrast,
  `global`'s NOT_DISCRIMINATED verdict is robust to this concern: it is
  negative at ALL THREE of its own PR5e horizons (6h=-0.50%,
  12h=-4.88%, 24h=-9.31%), so it would have been reached regardless of
  which horizon PR5e had preferred. `onchain` has only one PR5e horizon
  (24h), so selection could not have been a factor either way.
- **The negative higher-order partial correlation and the positive OOS
  RMSE reduction for `fng` are NOT in tension** -- the audit verified
  fng's full-model OLS coefficient is ALSO negative (-0.0788, matching
  the partial correlation's sign exactly, confirmed via an independent
  Frisch-Waugh-Lovell computation) and a NEGATIVE marginal relationship
  can still produce a POSITIVE out-of-sample RMSE reduction: partial
  correlation measures association direction/strength, RMSE reduction
  measures whether including the variable (with whatever sign its
  optimal coefficient turns out to be) improves held-out prediction
  accuracy. These are different questions with no reason to share a
  sign, and here they do not conflict at all -- the earlier draft of
  this section's wording (implying a "sign difference between the
  partial correlation and the OLS coefficient") was imprecise and has
  been corrected.

### Known limitations

- **Reduced sample size, and NOT symmetric across families.** Requiring
  all three families' values simultaneously present drops the usable
  sample from PR5e's own per-family n (445-495) to n=257. This is
  overwhelmingly driven by ONE variable: of 495 `history` rows in the
  analysis window (the windowed count -- see snapshot section below for
  why this differs from the raw `history` table's own row count),
  `fng` is present in 448 (90.5%) and `onchain` in 453 (91.5%), but
  `global` in only 317 (64.0%). Missingness across the three appears
  close to independent (495*0.905*0.640*0.915 ~ 262, close to the
  actual 257, i.e. not concentrated in the same rows). The 257-row
  complete-case sample spans 33.8 of the full window's 33.9 days (not
  clustered into a narrow sub-period), though `global`'s own coverage
  density does rise somewhat over the window (~35-43% in the first
  third of the window, ~58-69% in the final third) -- a real, mild
  temporal trend, disclosed here rather than smoothed over. All three
  families' evaluations were confirmed (by direct set comparison) to
  operate on the EXACT SAME 257 timestamps, so the fng/global/onchain
  results are directly comparable to each other, not confounded by
  differing sample composition.
- **Higher-order partial correlation is reported, not gated, and is
  NOT independent of the OOS result** -- both are functions of the same
  discovery-half data and the same variable set, and are never treated
  as two independent pieces of evidence. See the audit-findings note
  above for the precise (non-conflicting) relationship between the two
  for `fng` specifically.
- **Horizon selection is not statistically independent of the
  confirmation test** -- see the audit-findings note above. This is a
  materially different (weaker) claim than "the joint-control test and
  the horizon-selection criterion are unrelated," and readers should
  weigh `fng`'s result accordingly.
- **Only one primary horizon per family was tested jointly**; the other
  PR5e horizons are reported as context only, never independently
  re-run through the joint-control test in the module's own report
  output (the audit above DID re-run them, off-report, specifically to
  characterize the horizon-selection concern -- those numbers are
  recorded here and in the module docstring, not silently discarded).
  A future PR could formally add all horizons to the report, but that
  would reintroduce horizon-overlap pseudo-replication as a permanent
  feature of the output rather than a one-time audit check.
- **This result is specific to the trio {fng, global, onchain} and this
  ~49-day snapshot.** A different surviving-family set (a future
  snapshot) would need this analysis re-run, not extrapolated from
  these numbers.
- **`DISCRIMINATED_INCREMENTAL` is still not "validated."** `fng`'s
  survival here is stronger evidence than PR5e's own BUILD_REQUEST
  status, but it remains research evidence a human must review --
  never a basis for automatically implementing a V1/V2 change.
- The redundancy correction here is exact for THIS trio (3 families +
  composite) but is not a general n-family solution; a future surviving
  set with more members would need the same recursive/`ols_nvar`
  approach extended, not a new method invented ad hoc.

## PR5g: FNG-24h temporal robustness / validation study

Scope: a research-only follow-up to PR5f's own audit (PR #54), which
found PR5f's `fng`-at-24h `DISCRIMINATED_INCREMENTAL` result was
horizon-specific and asked whether it might also be specific to the
one ~34-day sample PR5f evaluated. PR5g answers exactly that question
and no other. It does not implement FNG, does not touch V1/V2/Worker,
and performs no production write. Full predefined methodology (written
BEFORE any new FNG number was computed) lives in
`research/fng_24h_robustness.py`'s own module docstring; this section
summarizes it and reports the result.

### Primary hypothesis (fixed before computing anything new)

H0: the PR5f FNG-24h incremental effect does not persist outside the
original evaluation period. H1: it continues to provide incremental
information beyond composite+global+onchain on genuinely new
chronological evidence. H1 was never assumed.

### Anti-cherry-picking rule

Horizon = 24h only, inherited unchanged from PR5f -- never re-selected.
6h/12h appear only as fixed context, copied verbatim from PR5f's own
already-published audit numbers (1.55%/4.40%), never recomputed here.

### Step 0: data availability (established before any FNG-specific
result existed)

Checked, read-only: `predictions`' own max ts was byte-for-byte
unchanged from PR5f's recorded window end. Only 6 `history` rows and 8
`btc_data` rows exist past that window end, and `btc_data` extends only
a few hours beyond it -- far short of the 24 hours of future price data
a new row needs to resolve a forward return at this horizon.
**Conclusion: zero new resolvable observations exist.** This is an
empirical fact checked before any FNG number was computed, not a
post-hoc excuse. `check_new_chronological_data()` is written generically
(never hardcodes "insufficient") so a future re-run with genuinely
advanced production data would correctly detect that and take the
primary design (Branch A) instead.

### Methodology actually used: Branch B (predefined walk-forward on the
existing sample)

Because Step 0 found no new external data, PR5g falls back to its own
predefined secondary design: split the SAME 257-row PR5f complete-case
sample (identical rows, identical model, identical horizon) into 4
equal chronological quartiles Q1-Q4, and run an expanding-window
walk-forward: fold 1 trains on Q1/tests on Q2, fold 2 trains on
Q1+Q2/tests on Q3, fold 3 trains on Q1+Q2+Q3/tests on Q4. Model spec is
IDENTICAL to PR5f (composite+global+onchain baseline vs. +fng full,
fit via `stats_utils.ols_nvar()`, RMSE-compared, chronological only).
Branch A (freeze PR5f's own discovery-half model, apply it forward to a
genuinely new period) is implemented and unit-tested against synthetic
new-period data, ready for whenever real new data exists, but was not
exercised on production today.

**This is stated explicitly, per the build authorization's own
required wording: "PR5g provides robustness evidence, not independent
confirmation."** Branch B re-examines rows PR5e and PR5f already used,
from a third chronological angle -- useful, but not the same
evidentiary weight as a fresh sample.

### Production snapshot

Extracted 2026-09-19 (a fresh pull, read-only). `predictions`: 1070
rows, ts range identical to PR5e/PR5f's own recorded window (byte-for-
byte, confirming no new predictions exist). `history`: 500 rows,
`btc_data`: 2099 rows -- both a handful of rows further along than
PR5f's own snapshot, but (per Step 0) none of that handful can resolve
a 24h forward return. The 257-row complete-case sample used here is
IDENTICAL to PR5f's own (same window, same target/control set).

### Results (predefined, not adjusted after seeing them)

| Fold | Train period | Test period | n train / test | Baseline RMSE | Full RMSE | RMSE reduction |
|---|---|---|---|---|---|---|
| 1 | Q1 | Q2 | 64 / 64 | 2.7165 | 3.0969 | **-14.00%** |
| 2 | Q1+Q2 | Q3 | 128 / 64 | 2.0238 | 2.0459 | **-1.09%** |
| 3 | Q1+Q2+Q3 | Q4 | 192 / 65 | 2.3748 | 2.0749 | **+12.63%** |

Only 1 of 3 folds is positive; the other two are negative (fold 1
substantially so). Fold 3's test period (2026-09-14 through
2026-09-19) is the portion of the sample most similar to PR5f's own
original validation tail -- which is exactly where PR5f's own
positive result came from. The earlier ~3/4 of the sample (folds 1-2)
does **not** show a consistent positive FNG-24h effect under this
predefined re-partitioning.

**Overall classification: `walk_forward_classification = NOT_REPLICATED`
(primary_classification = `INSUFFICIENT_DATA`, mechanically, because
no new external data exists to test the actual H0/H1 question).**

Both classifications were determined by predefined, mechanical rules
fixed before any fold was computed (see the module docstring) -- not
chosen after seeing which label looked best.

### Interpretation (per Section 15's required wording)

This does **not** mean "FNG is invalid" -- the underlying `ols_nvar`
mathematics were independently re-verified in PR #54's audit and
produce the identical numbers here. It also does **not** mean "FNG is
validated," under any classification, ever. What it means: FNG-24h's
positive effect, as PR5f measured it, appears concentrated in the tail
of the sample rather than holding consistently across earlier chronological
re-estimations of the same data -- a real, honestly-reported temporal-
stability concern that materially tempers how much weight PR5f's
original finding should carry, without new data to say more.

### Limitations

- **Not independent confirmation.** This is the third successive PR
  (PR5e, PR5f, PR5g) analyzing the same underlying ~34-day sample.
  Repeated looking at the same historical rows is not equivalent to a
  fresh sample, however the partition is sliced.
- **No genuinely new chronological data exists today.** The primary
  hypothesis this study was designed to test (does the effect persist
  OUTSIDE the original window) could not be tested at all.
  `INSUFFICIENT_DATA` is the honest, predefined answer to that
  specific question.
- **Smaller per-fold samples than PR5f's own 70/30 split.** Each
  quartile is ~64 rows; PR5f's own validation half was 78. Individual
  fold RMSE-reduction point estimates carry more sampling noise than
  PR5f's headline number.
- **6h/12h context is not re-verified here** -- copied verbatim from
  PR5f's own audit, per the fixed-horizon rule; a future study could
  re-run the SAME walk-forward at those horizons but that is out of
  PR5g's own predefined scope.
- **No economic/trading-cost calibration.** This study reports RMSE
  reduction only (a statistical/predictive-fit measure); no realized
  P&L, slippage, or trading-cost data was available or used, and the
  5% figure referenced for descriptive labeling (inherited from
  PR5e/PR5f) is explicitly NOT re-asserted here as an economic
  threshold -- see the module docstring's Section-11-compliance note.
- **No significance test / confidence interval on the RMSE-reduction
  point estimates.** Consistent with PR5e/PR5f's own established
  convention (magnitude + directional stability, not p-values), this
  study inherits the same limitation rather than inventing a new
  statistical test.

### Tests / production safety

25 new tests in `test_fng_24h_robustness.py` (chronological ordering,
no future-data leakage, fixed 24h horizon, no horizon re-selection,
insufficient post-period data at multiple thresholds, minimum-sample
handling, quartile construction, positive/negative/mixed replication,
deterministic reruns, missing-source complete-case handling, no writes,
no network, no V1/V2/Worker references). 419/419 research tests,
523/523 Vitest. No `persist_*` function exists in this module. No
migration applied. All production access read-only.

## FNG-24h data-readiness check (`fng_24h_readiness.py`)

A small, read-only follow-up to PR5g. It is **not** a new FNG analysis
and computes no FNG performance number. It answers exactly one
question: is there now enough genuinely new, resolvable 24h data after
PR5g's own frozen reference window (`fng_24h_robustness.PR5F_REFERENCE
["window_end_ts"]`, never re-hardcoded here) to run PR5g's
already-defined Branch A?

It reuses `check_new_chronological_data()` unmodified for the raw
candidate-row count and the existing `MIN_SAMPLE_FOR_LEVEL3=40`
threshold, and `source_analysis.extract_source_matrix()` unmodified to
read each candidate row's parsed source values. A row counts toward
readiness only when it has fng + all controls present **and** passes
the same strict per-row rule `check_new_chronological_data()` already
uses (`ts + 24h <= max(btc_data.ts)`) -- deliberately NOT
`outcome_engine`'s own `outcome_status == "RESOLVED"` flag, which is a
looser as-of proxy elsewhere in this project (it marks a row resolved
as soon as any later price point exists at all, however close, which
would understate the 24h horizon this check exists to enforce).

`build_readiness_record(conn)` returns `status` of either
`WAITING_FOR_NEW_24H_DATA` or `READY_FOR_PR5G_BRANCH_A`. Reaching
`READY_FOR_PR5G_BRANCH_A` does not run Branch A and does not by itself
authorize running it -- a separate, explicit authorization is still
required before any new FNG analysis.

Verified against a fresh, independent read-only production pull
(2026-09-19): 6 candidate rows exist after PR5g's window, `btc_data`
extends only ~1.1h past the newest of them (need 24h) ->
`new_resolvable_rows = 0`, `status = WAITING_FOR_NEW_24H_DATA`,
`data_gap_ms ≈ 82.5M ms (~22.9h)` -- consistent with PR5g's own Step 0
finding.

16 new tests in `test_fng_24h_readiness.py` (zero/below/exactly-at/
above the 40-row threshold, partial and mixed future-data resolution,
missing fng, missing a control family, the existing PR5f/PR5g sample
never counted as new even as `btc_data` advances past the window,
deterministic reruns, no writes, no network, no V1/V2/Worker
references, no FNG performance computation). 435/435 research tests,
523/523 Vitest. No `persist_*` function. No migration. No scheduling --
this is a utility to be invoked manually or wired into scheduling in a
separate, later, explicitly-authorized step.
