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
| `fp-asof-v2` (`knn-core-v2-asof` / `knn-core-v2-selfcontained`) | BTC, LINK, ETH | Fixes 1–5 below: as-of join, train-only fitting, backfill restamp (`bf-restamp-v1`). |

## Findings

| # | Feature or step | Model | Defect (before) | Severity | Status |
|---|---|---|---|---|---|
| 1 | V1 context `score`, `regime_mag`, `bottom_score` | BTC | Joined with `nearestRow`, symmetric within ±`TOL_MS` (4.8 h at 24 h). A historical analog could carry a V1 reading taken up to 4.8 h **after** its own timestamp, inside the outcome window it is labelled with. (The 48 h `HISTORY_FRESHNESS_MS` check never bound, because the 4.8 h window applied first.) | High: direct look-ahead into the label window | **Fixed:** as-of join (`asOfRow`): the latest reading at or before the row, no older than 4.8 h, else missing |
| 2 | V1 context `btc_regime_mag`, `sentiment_score` | LINK | Same symmetric join. | High | **Fixed:** same rule as BTC |
| 3 | Imputation means for missing context | BTC, LINK | Means computed over every row, including the predicted row. | Low: the query row contributes 1/n | **Fixed:** means from the rows before the predicted row |
| 4 | z-score mean and std | BTC, LINK, ETH | Statistics computed over every row, including the predicted (query) row. | Low | **Fixed:** fitted on the rows before the predicted row |
| 5 | `btc_price`, `technical_score` on backfilled rows | BTC, LINK, ETH | `backfillCoinHistory` stamped each Hyperliquid candle at its **open** time `c.t` but stored its **close** price, so price and RSI were available one interval (1 h or 24 h) after the row's timestamp. It also stored the still-open candle. | High: in analogs built on daily backfill rows the price is 24 h ahead of its timestamp | **Fixed.** New backfills are stamped at close + 1 ms. Stored rows are corrected **at read time** (`bf-restamp-v1`, pipeline `fp-asof-v2`); see "Scope of #5". Nothing in D1 is rewritten. |
| 6 | Mixed frequency in `technical_score` | BTC, LINK, ETH | RSI over the last 30 stored rows mixes daily backfill rows, hourly backfill rows and live ~3-hourly rows. Its meaning therefore changes over time. | Medium: a non-stationary feature, not look-ahead | **Not changed** (a model-definition change, not leakage). The offline harness computes RSI from a single-frequency candle series. |
| 7 | Label resolution `nearestRow(rows, ts + h, tol)` | all | The label price is the stored price nearest to t+h within ±0.2h. It can be up to 0.2h **before** t+h. | Label noise, not feature leakage. Analogs are restricted to `ts ≤ today − (h + tol)`, so no label uses data after the prediction time. | **Not changed.** The frozen prospective plans (H2) use their own resolution rule. |
| 8 | Calibration curve (`refreshCalibrationCurve`) | all | Built from all resolved predictions, i.e. only predictions whose target time has passed. It mixes model versions. | No look-ahead. After this change it will mix `v1` and `v2` predictions. | **Not changed.** Flagged for the deployment decision. |
| 9 | Conditional calibration (`predictions.ts < today.ts`, resolved only) | BTC | None found. | — | — |
| 10 | `computeEthRegimeMag`, `trendStrength`, `trailingVolatility` | ETH, all | Trailing windows ending at the row (inclusive). | None (subject to #5 for backfilled rows) | — |
| 11 | Selection LCA (`computeLcaScore`) | selection | It matches a variant's past prediction to a neighborhood timestamp in either direction within the tolerance. It uses resolved past predictions only. | Low: alignment noise, not look-ahead relative to now | **Not changed** (outside the prediction cores). Listed for follow-up. |

## Scope of #5 (read-only investigation, 2026-11-01)

**Method.** I read `ts` and price from the public production read endpoints `/chart-data`, `/eth-chart-data` and
`/link-chart-data`, which serve the same rows the models read.
- **Seal protection:** prices and outcomes of rows inside the sealed period were discarded at parse time. Only their
  timestamps were kept, plus one equality check on duplicate pairs.
- **Reference data:** unsealed rows were compared with independently retrieved Hyperliquid candles
  (`research-data/market-moves`).

**Affected rows by table:**

| Table | Rows | Exact-hour timestamps (backfill) | Distinct | Range | Last mis-stamped row (cutoff) |
|---|---|---|---|---|---|
| `btc_data` | 2,866 | 550 (91 at midnight) | 550 | 2026-05-05 → 2026-08-03 | 1785718800000 |
| `eth_data` | 1,228 | 551 (91 at midnight) | 551 | 2026-05-19 → 2026-08-17 | 1786924800000 |
| `link_data` | 2,534 | 861 (92 at midnight) | 540 | 2026-05-04 → 2026-08-03 | 1785726000000 |

**Proof of the shift.** Every unsealed exact-hour row equals the close of the candle that **opens** at its `ts`:
**638 / 638** (BTC 260, ETH 64, LINK 314).
- **Midnight rows** are daily candles, available 24 h later. The daily backfill ran first, so the hourly backfill
  skipped midnight even inside its window.
- **Other exact-hour rows** are hourly candles, available 1 h later.
- **Rejected rule:** a timestamp-adjacency rule mis-classified 14 midnight rows. The midnight rule reproduces all 638.

**Why the identification is safe.** Live rows carry millisecond jitter: no row after the cutoffs falls exactly on the
hour. New backfill rows are stamped close + 1 ms, so they are never exactly on the hour either.

**Duplicates.** `link_data` holds 321 duplicated backfill timestamps: 320 pairs have identical prices and 1 pair
differs. The correction keeps one deterministic row per timestamp.

**Correction.** `restampBackfillRows`, applied only inside `runPrediction`, `runLinkPrediction` and `runEthPrediction`:
- **Selection:** exact-hour rows at or before the table's cutoff.
- **Shift:** midnight rows move +24 h; other exact-hour rows move +1 h.
- **Deduplication and order:** one row per corrected timestamp is kept, and rows are re-sorted.

**Tests.**
- A behavioural test shows that mis-stamped daily rows give exactly the prediction that correctly stamped rows give.
  It fails if the correction is removed (mutation-checked).
- Unit tests cover the boundaries, the cutoff and order-independent deduplication.

**Not corrected, and why.**
- **Charts, `backfillPredictions`, the Gemini history and calibration-history readers** still read the stored stamps.
  They are display or analysis paths, not model features.
- **The challenger** (`regime-trend-v1`) reads only the latest 200 rows, which are all live rows today.
- **A permanent fix:** a reviewed D1 migration (`UPDATE … SET ts = ts + interval`) would remove the need for the
  read-time correction. It rewrites stored history, so it is left to an owner decision.

## Missing and stale values

- **What counts as missing or stale:** a context value is missing when no V1 reading exists at or before the row,
  and stale when the latest one is older than `CONTEXT_ASOF_MAX_AGE_MS` (4.8 h, the same width as the old window).
  Both are treated identically; a test checks that a stale reading gives exactly the same result as no reading.
- **How they are filled:** they are imputed with the mean of earlier rows' real values, the existing technique and
  not a neutral constant. If no earlier real value exists, the existing fallbacks (50 / 0 / 25) apply, as before.
- **Disclosure:** the per-prediction note keeps disclosing how many analogs used an averaged reading.

## Residual risk

1. **Existing backfilled rows (#5).** These are now corrected at read time in the three model paths (see "Scope of
   #5"). A permanent stored-data correction is an owner decision.
2. **The calibration curve mixes model versions (#8)** until enough `v2` predictions resolve.
3. **Selection and experiment comparisons.**
   - **Problem:** EXP-002 and EXP-003 compare against production choices. Deploying `v2` changes the production model
     mid-collection. Every stored row carries `model_version`, so this can be stratified, but it is a confound to
     acknowledge before deploying.
   - **Status:** frozen plans are not modified here.
