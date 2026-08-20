# CryptoPulseV2 — Leakage Audit

Date: 2026-08-18
Scope: `predictions`, `link_predictions`, `eth_predictions`, `challenger_predictions`,
calibration (core + challenger), `selectBestVariant`, the new daily learning engine,
and the new catalyst layer.

Per `.ai/EXPERIMENT_PROTOCOL.md`, and per the explicit instruction attached to this
work: *"The existing audit indicates calibration and candidate-cutoff logic appear
leak-safe. Do not 'fix' these areas unless a reproducible defect is found. Document
the evidence supporting the conclusion that they are safe."* No prediction-path code
was modified as part of this leakage audit — findings only.

---

## 1. Look-ahead bias

**Safe.** All three core models (`runPrediction`, `runLinkPrediction`, `runEthPrediction`)
build their k-NN candidate pool with:

```
candidates = complete.slice(0, -1).filter(r => r.ts <= today.ts - (lagMs + tolMs))
```

This excludes any historical point too recent to have a resolved forward outcome —
confirmed in memory as a previously-found-and-fixed bug (a live run once returned 15
neighbors, 0 resolved, before this filter existed). Present and correct in all three
core prediction functions as of this audit.

## 2. Feature leakage

**Safe.** `today = complete[complete.length - 1]` — the feature vector for the
prediction being made is always the most recent row in an ascending-`ts` series, never
a row with a later timestamp. Feature values (`score`, `technical_score`,
`regime_mag`, `bottom_score` / LINK's / ETH's self-contained equivalents) are read
directly from `btc_data`/`link_data`/`eth_data`/`history`, all populated by the
existing hourly/3-hourly logging cron, never backfilled from a point later than the
prediction's own `ts`.

## 3. Calibration leakage

**Safe — verified, not re-derived.** Both calibration layers query strictly-prior
data at prediction time:

- Decile calibration: `getLatestCalibrationCurve` reads the most recently *computed*
  curve (built by the daily 07:00 UTC cron from `realized_up IS NOT NULL` rows only —
  i.e. rows that have already resolved, which by definition happened before "now").
  A resolved historical prediction's outcome informing today's *different* prediction
  is calibration working as intended, not leakage.
- Conditional calibration: `computeConditionalCalibration`'s history query is
  `WHERE horizon_hours=? AND realized_up IS NOT NULL AND ts < today.ts` — explicit
  `ts < today.ts` bound, can't include same-or-future rows.

**Previously flagged, now resolved:** a hypothesized "midnight rollover" edge case
(a same-day-resolved prediction feeding a same-day new prediction before the daily
curve refresh) was reviewed and is **not** a leak — it only means calibration can be
a few hours stale, never that a prediction's own unresolved outcome touches its own
calibration.

## 4. Neighbor leakage

**Safe.** Neighbors are selected from `candidates` (see §1), and each neighbor's own
forward return (`nearestRow(btcRows, n.row.ts + lagMs, tolMs)`) is itself required to
already exist in the historical series — i.e., the neighbor's outcome is in *our*
past even though it was that neighbor's *own* future. This is the entire mechanism of
analog-matching, not leakage: today's prediction never sees its own future, only
already-realized outcomes of earlier analogous days.

## 5. Selection leakage

**Safe — reviewed this pass** (flagged as not-yet-traced in the Phase 1 audit,
now traced end-to-end). `selectBestVariant`'s meta-neighborhood query:

```
WHERE horizon_hours=? AND realized_up IS NOT NULL AND features_json IS NOT NULL
  AND ts < ? ORDER BY ts DESC LIMIT 300   -- bound to latestCore.ts
```

Both the meta-neighborhood (which historical moments count as "similar") and each
variant's LCA scoring (`WHERE ... realized_up IS NOT NULL ...`) are restricted to
already-resolved, strictly-prior rows. Today's query features come from the most
recent core prediction's already-computed `features_json` — not recomputed with any
forward-looking data.

## 6. Timestamp leakage

**Safe.** Every table's `ts` (prediction time) and `resolved_ts` (outcome time) are
set by `Date.now()` at distinct, sequential points in the code — `ts` at prediction
creation, `resolved_ts` only later inside `backfillPredictions` /
`backfillEthPredictions` / `backfillChallengerPredictions`, gated by
`WHERE realized_up IS NULL AND target_ts <= ?`. `resolved_ts >= ts` holds structurally:
resolution can only run after `target_ts` (itself `ts + lagMs`) has passed.

## 7. Outcome leakage

**Safe.** Confirmed directly by reading the three backfill functions: each `UPDATE`
statement's `SET` clause touches only `realized_*_price`, `realized_return`,
`realized_up`, `resolved_ts` — never `features_json`, `p_up`, `calibrated_p_up`, or
any other prediction-time field. This is now enforced by an automated test
(`tests/learning-engine.test.js`, "Immutability" suite) that parses the actual
`UPDATE` statements and fails if any non-outcome column is ever added to one.

## 8. Post-publication catalyst leakage

**Structurally prevented, not yet load-bearing** (no automatic catalyst ingestion
exists yet — Phase 6 is schema/contract only, per `.ai/MARKET_CATALYST.md`).
`classifyCatalystTiming(catalystEventTs, predictionTs)` is the single function
allowed to answer `available_before_prediction`, and it is deliberately **not**
precomputed and stored on the catalyst row itself — a catalyst's availability is a
property of *which prediction* it's being compared against, so hardcoding it on the
row would let staleness silently misclassify it later. `recordCatalyst` stores raw
timestamps only.

---

## Open items for future review (not defects — scope boundaries)

- Catalyst layer has zero real usage yet (2 pre-existing, unrelated V1 rows only) —
  the leak-safety above is a design guarantee, not yet evidence from real data. Revisit
  once catalysts are actually being logged.
- `selectBestVariant`'s 6-hour tolerance (`TOL_MS_META`) for matching a variant's own
  prediction timestamp to a neighborhood timestamp was not specifically stress-tested
  for edge cases near horizon boundaries in this pass — no evidence of a problem, just
  not exhaustively fuzzed.

## Conclusion

No leakage defect was found in any of the 8 required categories. Two categories
flagged as open-but-unreviewed in the Phase 1 audit (selection-layer meta-neighborhood,
calibration timing) were traced this pass and confirmed safe. No prediction-path code
required a "fix" — consistent with the instruction not to alter leak-safe areas
absent a reproducible defect.
