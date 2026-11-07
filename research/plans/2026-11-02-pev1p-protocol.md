# PEV-1P: prospective test window for the PEV-1 models (frozen 2026-11-02)

**Why.** PEV-1's TEST period (2026-10-09T21Z → 2026-11-01) had already been looked at in the 2026-11-01 sentiment
weighting audit. It cannot support a confirmatory claim. PEV-1P re-runs the **same models, features, metrics,
baselines and decision rule** on a window nobody has evaluated.

**Changes after this commit:** only by a new versioned protocol committed before the checkpoint. Nothing is tuned on
the window.

**Code:** `research/prospective_pev.py`. **Tests:** `research/test_prospective_pev.py`.

## Window and data

- **Test window:** decision times from **2026-11-01T00:00Z**. Units are the PEV-1 non-overlapping units: 00:00 UTC
  for 24 h; 00:00 and 12:00 UTC for 12 h. A unit counts once its label time has passed.
- **What had been seen before the freeze:** PEV-1's candle retrieval on 2026-11-01 included prices up to about
  03:00Z. No unit inside the window had resolved by then: the first 24 h label resolves at 2026-11-02T00:00Z and the
  first 12 h label at 2026-11-01T12:00Z. No metric, label or outcome inside the window has been computed or viewed.
- **Training and analogs:** all usable rows before each unit. That means pre-seal rows and post-seal rows, the seal
  excluded as in PEV-1. Training on the earlier, already-seen period is legitimate; only evaluation must be on unseen
  units.
- **Prices:** Hyperliquid hourly candles, as in PEV-1. Post-seal candles are fetched with pagination.
- **Sentiment:** the V1 composite archive (`/api/research-lab/experiment5-sentiment`). Server-side collection
  (PulseWorker #3), if activated, increases the number of C-eligible units; it does not change the protocol.

## Models, metrics, decision

- **Identical to PEV-1:** A, B, C, P4-LR and P4-KNN-SHRINK, and the baselines always-up, always-down, climatology and
  persistence.
- **Hyperparameters frozen** to PEV-1's VAL choices, which were identical in all 6 cells: **P4-LR λ = 10**,
  **P4-KNN-SHRINK m = 30**.
- **Common eligible set:** as in PEV-1.
- **Metrics and uncertainty:** balanced accuracy, per-direction recall, Brier score, ECE, paired bootstrap 95% CI.
  Goal A (24 h large moves) is reported separately.
- **PREDICTIVE IMPROVEMENT DEMONSTRATED,** for a model, coin and horizon, requires:
  - the paired CI for balanced accuracy versus the best naive baseline to be entirely above 0; **and**
  - the paired CI for Brier versus climatology to be entirely below 0.
- **NOT EVALUABLE** below 20 eligible units.

## Checkpoint and no peeking

- **One confirmatory analysis, at 2027-05-01T00:00Z.**
  - About 181 daily units at 24 h and 362 at 12 h per coin, before exclusions.
  - At n ≈ 181 the balanced-accuracy CI is about ±0.07, so only balanced accuracy of roughly 0.58 or more (24 h)
    or 0.555 or more (12 h) can clear the rule.
  - `final` mode refuses to run before that time.
- **Before the checkpoint,** only `counts` mode runs:
  - units elapsed, resolved, with features, with as-of sentiment, and eligible for all models;
  - no label value, metric or prediction is computed or reported;
  - a test checks that the counts are identical when every outcome in the window is reversed.
- **Multiplicity:** as in PEV-1, 3 coins × 2 horizons × 5 models are reported without correction. A single cell
  meeting the rule is reported as weak evidence.

## Frozen experiments

**This is an offline evaluation; it registers nothing and changes no production behaviour.** No frozen experiment is
modified: EXP-002/003/009/010/011/023/025/027/028.
