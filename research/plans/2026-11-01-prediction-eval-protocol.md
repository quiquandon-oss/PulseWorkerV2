# Chronological prediction evaluation protocol (PEV-1, frozen 2026-11-01)

**Frozen before the harness was run on any period.** Changes need a new versioned protocol, committed before the run
it affects. Implemented by `research/prediction_eval.py` and tested in `research/test_prediction_eval.py`.

**Purpose:** compare the legacy and corrected prediction pipelines, with and without sentiment, against naive
baselines, using one chronological rule set. This protocol does not change any frozen experiment (EXP-002/003/004/008,
EXP-023/025/027/028, EXP-009/010/011). It is an offline research evaluation and makes no production change.

## Data

| Input | Source | Use |
|---|---|---|
| Hourly candles BTC, ETH, LINK | Hyperliquid `candleSnapshot` (public). Pre-seal: `research-data/market-moves` observations. Post-seal: a fresh retrieval (`prediction_eval.py fetch`) | Prices; each close is usable from `close_ts + 1 ms` |
| V1 composite sentiment `score` | `/api/research-lab/experiment5-sentiment` archive (read-only export of V1 `history`) | Model A and C context feature |

The sha256 of every input file is recorded in the output.

## Sealed period

- **Never read:** prices with availability time in (2026-07-22T00:00Z, 2026-10-09T21:00Z] and sentiment readings in
  [2026-07-22T00:00Z, 2026-10-09T21:00Z] are dropped at load time and never used as features, labels or analogs.
  Units whose feature window or label touches the seal are therefore ineligible; they are counted.
- **Never tuned on:** the sealed holdout. Nothing in this protocol is tuned on TEST.

## Decision grid, features, label

- **Decision times:** every 3 h at 00, 03, …, 21 UTC. Each decision uses the 3-hourly closes available at or before
  the decision time.
- **`technical_score`:** `computeSimpleTechnicalScore` (ported exactly from `worker.js`) over the last 30 3-hourly
  closes, including the close at t. This is single-frequency, unlike production's mixed-frequency rows (leakage audit
  #6). If any of the 30 closes is missing, the feature is missing.
- **`regime_mag`:** (MA8 − MA21) / MA21 × 100 over 3-hourly closes, as in `computeEthRegimeMag`.
- **`sentiment`:** the V1 composite score.
  - **Corrected models:** as of t, i.e. the latest reading at or before t and no more than 4.8 h old.
  - **Legacy model:** the nearest reading within ±4.8 h, restricted to readings that existed at the decision time. That
    is how the live legacy model saw them.
- **Label:** R_h = close(t+h) / close(t) − 1, for h ∈ {12 h, 24 h}. UP if R_h > 0, otherwise DOWN. If close(t+h) is
  missing, the unit is unresolved.

## Models

All k-NN models use production's rules:
- K = min(15, max(5, ⌊n_candidates/3⌋));
- candidates are rows with ts ≤ t − (h + 0.2h) and a resolved label;
- p_up = the share of UP among the K nearest neighbours;
- at least 5 resolved neighbours, otherwise the model abstains.

| Model | Features | Join | Imputation and scaling | Notes |
|---|---|---|---|---|
| **A** legacy | technical_score, regime_mag, sentiment | symmetric nearest ±4.8 h (`fp-nearest-v0`) | mean and z-score over all rows available at t, **including the query row** | Flagged as potentially defective (leakage audit #1, #3, #4). It is an approximation of production: V1 regime_mag and bottom_score history is not available offline, so A and C share one feature set, and the comparison isolates the pipeline. |
| **B** corrected, no sentiment | technical_score, regime_mag | — | z-score on rows before t | `fp-asof-v1` |
| **C** corrected, with sentiment | technical_score, regime_mag, sentiment | as-of ≤ t, ≤ 4.8 h | mean imputation and z-score on rows before t | As in production, analogs without a reading are imputed. |

**Model D:** none. No documented reason for a fourth model exists.

### Phase 4 candidates

These are price-only, because sentiment does not exist before the seal.

- **P4-LR**, L2-regularised logistic regression on standardised [technical_score, regime_mag], refit on resolved rows
  before t. λ ∈ {0.1, 1, 10}, chosen on VAL.
  - **Hypothesis:** a smooth low-variance model calibrates better (lower Brier) than a K ≤ 15 vote, which is quantised
    to multiples of 1/K.
  - **Expected effect:** Brier improves; balanced accuracy is not expected to.
- **P4-KNN-SHRINK**, model B with K = 50 and p shrunk toward the training UP rate: p' = (K·p + m·base) / (K + m), with
  m ∈ {0, 10, 30} chosen on VAL.
  - **Hypothesis:** small-K vote probabilities are over-dispersed.
  - **Expected effect:** Brier improves.

### Baselines

- `always_up`, `always_down`: p = 1 or 0.
- `climatology`: p = UP rate of resolved training rows before t.
- `persistence`: UP if R over the preceding h > 0; p = 1 or 0.

## Periods

| Period | Decision times | Role |
|---|---|---|
| DEV | first eligible → 2026-06-15T00Z | Phase 4 fitting only |
| VAL | 2026-06-15T00Z → seal start (label ≤ 2026-07-22T00Z) | Phase 4 hyperparameter choice |
| TEST | first post-seal eligible → last resolved | One run with frozen choices. This is the only period with sentiment, so **A vs B vs C is compared on TEST only**. |

## Units and eligibility

- **Inferential units:** non-overlapping decision times, at 00:00 UTC for 24 h and at 00:00/12:00 UTC for 12 h.
- **Common eligible set:** every model is scored on the same eligible units. A unit is eligible only if every model in
  the comparison gives a prediction. For the A/B/C table that means C has a real (not imputed) as-of sentiment reading
  at t. Every exclusion is counted by reason.
- **Reporting split:** per coin (BTC, ETH, LINK) and per horizon.

## Metrics

- **Goal B (direction), all units:**
  - n and the UP share;
  - balanced accuracy (mean of UP recall and DOWN recall), with p ≥ 0.5 meaning a call of UP;
  - UP recall and DOWN recall;
  - Brier score;
  - calibration: 5 equal-width bins, ECE, and the reliability table.
- **Goal A (large moves), 24 h only:**
  - the units with |R24| > 3% (market-moves v1.0.0 threshold);
  - per-direction recall on those units;
  - AUC of the model's conviction |p − 0.5| for separating large-move units from the rest.
  - Goal A is reported separately and never pooled with Goal B.
- **Uncertainty:** percentile bootstrap 95% CI, 2000 resamples, seed 20261101, resampling inferential units. Paired
  differences (C − B, C − A, B − A, model − climatology Brier, model − best naive BA) use the same resamples.

## Decision rule

**PREDICTIVE IMPROVEMENT DEMONSTRATED** for a model, coin and horizon requires, on TEST:
- the paired 95% CI for balanced accuracy versus the best naive baseline lies entirely above 0, **and**
- the paired 95% CI for Brier versus climatology lies entirely below 0.

Otherwise the label is **NOT DEMONSTRATED**. Below 20 inferential units the label is **NOT EVALUABLE (n < 20)**. No
multiplicity relief is claimed: with 3 coins × 2 horizons × 3 models, a single success is weak evidence, and it is
reported as such.
