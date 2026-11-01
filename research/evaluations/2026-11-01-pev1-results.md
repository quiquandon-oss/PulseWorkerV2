# PEV-1 results: chronological evaluation of models A, B, C and Phase 4 candidates (2026-11-01)

**Protocol:** `research/plans/2026-11-01-prediction-eval-protocol.md`, frozen before the run. **Full output:**
`2026-11-01-pev1-report.json`, which includes every metric, CI, calibration table and input sha256.

**Run:** `python3 research/prediction_eval.py run --candles <market-moves hourly> <post-seal fetch> --sentiment <archive>`.
It ran once on TEST. A first invocation crashed on a missing-feature query row before producing any output; the
fix makes the model abstain, as the protocol's "missing feature" rule implies, and a test was added. No choice was
changed after any result was seen.

**Data:**
- Hourly Hyperliquid candles 2026-03-23 → 2026-11-01, sealed closes dropped.
- 206 usable V1 composite readings, all after the seal.
- **Sealed period:** nothing read.

## Headline

| Question | Answer | Label |
|---|---|---|
| Does any model beat naive baselines on TEST? | No. 12 h: all models NOT DEMONSTRATED (n = 21 per coin). 24 h: n = 5 per coin. | **PREDICTIVE IMPROVEMENT NOT DEMONSTRATED** |
| Does sentiment (C vs B) help? | No evidence either way. The paired CIs include 0 everywhere except ETH 12 h, where C has a **worse** Brier (+0.040 [0.008, 0.069]). That is one cell of 6 and not conclusive. | NOT DEMONSTRATED |
| Does the leakage fix (B or C vs A) change accuracy? | No detectable change at this n. Two cells show B better than A on Brier (ETH 12 h −0.067 [−0.121, −0.014]; LINK 24 h n = 5). Not conclusive. | NOT DEMONSTRATED |
| Is the production k-NN rule (B) calibrated? | **No.** On pre-seal VAL its Brier is worse than climatology in every cell, with the CI excluding 0 in 4 of 6 (table below). Balanced accuracy is not above the baselines. | Finding (pre-seal VAL) |

## Pre-seal VAL (2026-06-15 → 2026-07-22): B vs climatology

This is the largest clean sample (74 units at 12 h, 37 at 24 h).

| Coin | h | ΔBrier B − climatology [95% CI] | ΔBA [95% CI] |
|---|---|---|---|
| BTC | 12 h | +0.038 [0.000, 0.076] | −0.074 [−0.195, 0.054] |
| BTC | 24 h | +0.037 [−0.023, 0.104] | +0.044 [−0.117, 0.203] |
| ETH | 12 h | +0.056 [0.009, 0.106] | −0.076 [−0.268, 0.106] |
| ETH | 24 h | +0.069 [−0.002, 0.149] | −0.069 [−0.233, 0.095] |
| LINK | 12 h | +0.043 [0.009, 0.077] | −0.093 [−0.285, 0.095] |
| LINK | 24 h | +0.075 [0.020, 0.129] | −0.099 [−0.302, 0.125] |

**Interpretation:** a K ≤ 15 neighbour vote produces over-dispersed probabilities (multiples of 1/K). On price-only
features it carries no detectable directional information, so its Brier is worse than always forecasting the base
rate. This is a model-quality finding, independent of the leakage fix.

## Phase 4 candidates

Hyperparameters were chosen on VAL by Brier: P4-LR λ = 10, and P4-KNN-SHRINK m = 30 for every coin and horizon.

- **P4-LR:**
  - **Hypothesis H4.1** (better Brier than k-NN): **supported on VAL.** Brier ≈ 0.251–0.263 against B's 0.288–0.326.
  - Brier is not better than climatology, and balanced accuracy is ≈ 0.5.
  - It learns ≈ the base rate, which means the price features carry little signal.
- **P4-KNN-SHRINK:**
  - **Hypothesis H4.2** (shrinkage repairs over-dispersion): **partly supported.** Brier is lower than B but not
    below climatology.
  - On TEST ETH 12 h it reached the best BA (0.68 [0.47, 0.86]); the CI includes 0.5, so it is not demonstrated.
- **Release:** neither candidate is recommended for release. No candidate beats climatology.

## Why C cannot be evaluated yet: sentiment availability

The V1 composite is computed in the browser and written to `history` only while someone has V1 open: no write below
50% completeness, and at most one per 15 min.

**Coverage after the seal:**
- **Gaps:** 23 gaps longer than 4.8 h, the longest 18.7 h.
- **Daily units:** of 19 post-warm-up daily units (00:00 UTC), **6** had an as-of reading. C therefore abstained on
  most units.

**What is needed:** a server-side composite collected on a fixed schedule (Phase 2, PR-C). Until it exists, any
sentiment evaluation is sample-limited by browser activity, not by calendar time.

## Goal A (large moves, |R24| > 3%)

**Status: NOT EVALUABLE.** TEST holds 0 large-move units for BTC and ETH and 1 for LINK.

## Limitations

- **Model A approximates production.** V1 `regime_mag` and `bottom_score` history is not available offline, so A and
  C share one feature set. The A–C comparison isolates the pipeline, not the full production model.
- **TEST is small.** It is 22 days, about 4 of which are feature warm-up after the seal gap. 12 h units: 21; 24 h units: 5 per coin.
- **Multiplicity.** 3 coins × 2 horizons × 5 models are reported without multiplicity correction. Single-cell CIs
  that exclude 0 are reported as observations, not findings.
- **TEST had been seen before.** The post-seal period was already used in the 2026-11-01 sentiment weighting audit
  (composite accuracy). It is therefore not a pristine holdout for sentiment questions. The protocol's next TEST
  window should start 2026-11-01.
