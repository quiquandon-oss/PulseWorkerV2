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

## Reproduction and conformance review (2026-11-01)

1. **Re-run:** the same command on the same inputs, with all 7 input sha256 verified, gives a report identical in
   every metric. Only `generated_at` differs.
2. **Pinned inputs:**
   - **Committed:** the post-seal candles and the post-seal sentiment archive are now committed in `pev1-inputs/`,
     with `MANIFEST.md` and `test_pev1_inputs.py`, which checks the hashes and that no sealed-period row was committed.
   - **Result:** a re-run on the committed copies is identical in every metric.
3. **Independent implementation:** `research/pev1_independent_check.py` re-implements the VAL computation for model
   B and climatology from the protocol text, in NumPy, sharing no code with the harness. It matches the reported
   `n`, B Brier, climatology Brier, B balanced accuracy and UP share **exactly in all 6 coin × horizon cells**.
4. **Conformance with the protocol, checked against the code:**
   - **Seal:** a price is usable iff available ≤ seal start or > seal end; boundary tests are in place.
   - **Periods:** DEV < 2026-06-15, VAL [2026-06-15, seal start), TEST after the seal.
   - **Units:** 00:00 UTC (24 h) and 00:00/12:00 UTC (12 h), so labels never overlap between units.
   - **Analogs:** an analog's label time is ≤ t − 0.2h.
   - **Common eligible set:** a unit counts only if every model in the block predicted; exclusions are counted by reason.
   - **Baselines and metrics:** as specified; Goal A is reported at 24 h only and separately.
   - **No deviation found.**
5. **Sample-size limits** (unchanged conclusions):
   - **TEST 12 h:** n = 21 per coin. A balanced-accuracy CI is about ±0.2 wide, so only effects above roughly 0.2
     could be detected.
   - **TEST 24 h:** n = 5 per coin; NOT EVALUABLE.
   - **Goal A:** 0–1 large-move units; NOT EVALUABLE.
   - **VAL:** the finding that k-NN B is worse than climatology on Brier in 4 of 6 cells rests on 74 units (12 h)
     and 37 units (24 h).

## Why nothing beats climatology: feature information (pre-seal only, 2026-11-01)

**Setup:** `research/feature_information.py`, output in `2026-11-01-feature-information.json`.
- **Data:** DEV + VAL only; TEST and the seal are untouched.
- **Units:** non-overlapping; n = 232 at 12 h and 116 at 24 h per coin.
- **Nature:** pre-declared and descriptive; nothing is tuned or selected.

**Findings:**
- **No detectable directional information.** All **36** Spearman correlations of `technical_score` and `regime_mag`
  with the forward return have 95% CIs that include 0. The 36 are 3 coins × 2 horizons × 2 features × (all,
  low-volatility, high-volatility).
  - Overall |ρ| ≤ 0.09.
  - The largest sub-group estimate, ETH 24 h `technical_score` in low volatility, is −0.25 [−0.48, 0.004]. It is not
    significant, and it is one of 36 tests.
  - At n = 232 the CIs exclude |ρ| above about 0.13.
- **Class balance:** the UP share is 0.50–0.54, so imbalance does not explain the results.
- **Feature drift:** the mean of `technical_score` moved from about 48 (DEV) to about 53 (VAL), with similar spread.
  `regime_mag` is centred near 0 in both.

**Conclusion.** With these price-derived features, a k-NN or a regularised linear model has no signal to learn at
12–24 h. That is consistent with every model landing at or above climatology's Brier score.

**Implications:**
- **Do not add model complexity** on this feature set.
- **The only untested candidate is sentiment.** It needs the server-side collection (PulseWorker#3) and a prospective
  sample.
- **Status: PREDICTIVE IMPROVEMENT NOT DEMONSTRATED**, now with the limiting evidence identified.
