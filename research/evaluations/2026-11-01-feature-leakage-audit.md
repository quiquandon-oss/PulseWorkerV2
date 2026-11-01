# Feature leakage audit: BTC, LINK and ETH k-NN cores (2026-11-01)

**Scope:** every feature, imputation, scaling and label step in `runPrediction` (BTC), `runLinkPrediction` (LINK) and
`runEthPrediction` (ETH), plus the backfill writers that create the rows those models read.

**Method:** code reading, with mutation-checked regression tests in `tests/prediction-asof-leakage.test.js`. If the
join is made symmetric again (`nearestRow`), 2 of those tests fail.

**Not in scope:** no stored data was read or rewritten. The sealed period was not touched.

## Pipeline versions

| Version | Models | Change |
|---|---|---|
| `fp-nearest-v0` (implicit, tagged `knn-core-v1` / `knn-core-v1-selfcontained`) | BTC, LINK, ETH | Legacy behaviour, kept in git history. Model A of the offline evaluation reproduces it. |
| `fp-asof-v1` (`knn-core-v2-asof` / `knn-core-v2-selfcontained`) | BTC, LINK, ETH | Fixes 1–4 below. |

## Findings

| # | Feature or step | Model | Defect (before) | Severity | Status |
|---|---|---|---|---|---|
| 1 | V1 context `score`, `regime_mag`, `bottom_score` | BTC | Joined with `nearestRow`, symmetric within ±`TOL_MS` (4.8 h at 24 h). A historical analog could carry a V1 reading taken up to 4.8 h **after** its own timestamp, inside the outcome window it is labelled with. (The 48 h `HISTORY_FRESHNESS_MS` check never bound, because the 4.8 h window applied first.) | High: direct look-ahead into the label window | **Fixed:** as-of join (`asOfRow`): the latest reading at or before the row, no older than 4.8 h, else missing |
| 2 | V1 context `btc_regime_mag`, `sentiment_score` | LINK | Same symmetric join. | High | **Fixed:** same rule as BTC |
| 3 | Imputation means for missing context | BTC, LINK | Means computed over every row, including the predicted row. | Low: the query row contributes 1/n | **Fixed:** means from the rows before the predicted row |
| 4 | z-score mean and std | BTC, LINK, ETH | Statistics computed over every row, including the predicted (query) row. | Low | **Fixed:** fitted on the rows before the predicted row |
| 5 | `btc_price`, `technical_score` on backfilled rows | BTC, LINK, ETH | `backfillCoinHistory` stamped each Hyperliquid candle at its **open** time `c.t` but stored its **close** price. Price and RSI were therefore available one interval (1 h or 24 h) after the row's timestamp, and the still-open last candle was stored with a moving "close". | High for analogs built on daily backfill rows: the price is 24 h ahead of its timestamp, and the context join (now as-of) pairs it with an older reading | **Fixed for future backfills:** rows are stamped at close time + 1 ms and the open candle is skipped. **Existing stored rows are not rewritten** (instruction: do not rewrite historical observations). They stay mis-stamped; see "Residual risk". |
| 6 | Mixed frequency in `technical_score` | BTC, LINK, ETH | RSI over the last 30 stored rows mixes daily backfill rows, hourly backfill rows and live ~3-hourly rows. Its meaning therefore changes over time. | Medium: a non-stationary feature, not look-ahead | **Not changed** (a model-definition change, not leakage). The offline harness computes RSI from a single-frequency candle series. |
| 7 | Label resolution `nearestRow(rows, ts + h, tol)` | all | The label price is the stored price nearest to t+h within ±0.2h. It can be up to 0.2h **before** t+h. | Label noise, not feature leakage. Analogs are restricted to `ts ≤ today − (h + tol)`, so no label uses data after the prediction time. | **Not changed.** The frozen prospective plans (H2) use their own resolution rule. |
| 8 | Calibration curve (`refreshCalibrationCurve`) | all | Built from all resolved predictions, i.e. only predictions whose target time has passed. It mixes model versions. | No look-ahead. After this change it will mix `v1` and `v2` predictions. | **Not changed.** Flagged for the deployment decision. |
| 9 | Conditional calibration (`predictions.ts < today.ts`, resolved only) | BTC | None found. | — | — |
| 10 | `computeEthRegimeMag`, `trendStrength`, `trailingVolatility` | ETH, all | Trailing windows ending at the row (inclusive). | None (subject to #5 for backfilled rows) | — |
| 11 | Selection LCA (`computeLcaScore`) | selection | It matches a variant's past prediction to a neighborhood timestamp in either direction within the tolerance. It uses resolved past predictions only. | Low: alignment noise, not look-ahead relative to now | **Not changed** (outside the prediction cores). Listed for follow-up. |

## Missing and stale values

- **What counts as missing or stale:** a context value is missing when no V1 reading exists at or before the row,
  and stale when the latest one is older than `CONTEXT_ASOF_MAX_AGE_MS` (4.8 h, the same width as the old window).
  Both are treated identically; a test checks that a stale reading gives exactly the same result as no reading.
- **How they are filled:** they are imputed with the mean of earlier rows' real values, the existing technique and
  not a neutral constant. If no earlier real value exists, the existing fallbacks (50 / 0 / 25) apply, as before.
- **Disclosure:** the per-prediction note keeps disclosing how many analogs used an averaged reading.

## Residual risk

1. **Existing backfilled rows (#5).**
   - **Problem:** `btc_data`, `link_data` and `eth_data` still hold rows stamped at candle open with the close price.
     The live models keep reading them as analogs.
   - **Why it is not fixed here:** a read-time correction is possible only if backfilled rows can be told apart from
     live rows reliably. No source column exists, and exact-hour timestamps are a heuristic. It is not attempted here.
   - **Options for the owner:**
     - (a) add a `source` column for new rows and a one-off, reviewed relabelling migration;
     - (b) shift backfilled rows at read time, using the heuristic, behind a version tag.

   Each needs approval because it alters what production reads.
2. **The calibration curve mixes model versions (#8)** until enough `v2` predictions resolve.
3. **Selection and experiment comparisons.**
   - **Problem:** EXP-002 and EXP-003 compare against production choices. Deploying `v2` changes the production model
     mid-collection. Every stored row carries `model_version`, so this can be stratified, but it is a confound to
     acknowledge before deploying.
   - **Status:** frozen plans are not modified here.
