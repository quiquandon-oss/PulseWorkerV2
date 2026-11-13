# Duplicate price rows per scheduled run: measurement and prepared correction

**Status:** measurement and design only. Production behaviour is unchanged. The correction on this branch
(`research/price-row-per-tick`) is behind `PRICE_ROW_PER_TICK`, unset by default, and is **not** to be merged or
deployed before an owner decision. No historical row was modified and no prediction was recomputed.

**Source:** `data-exports/{btc,eth,link}_data.csv` from export run 43372515598 (2026-11-13 05:31 UTC, `d40f52f`),
read by `research/price_row_duplicates.py`. A "run" groups rows written within 10 minutes of the group's first row.

## 1. Measured facts

| | BTC | ETH | LINK |
|---|---|---|---|
| Rows / runs | 3,050 / 1,485 | 1,412 / 1,022 | 2,718 / 1,268 |
| Runs with more than one row | 900 | 384 | 959 |
| First such run | 2026-08-03 | 2026-08-17 | 2026-07-19 |
| Rows that repeat an earlier row of the same run | 51.3% | 27.6% | 53.4% |
| Multi-row runs whose rows carry different prices | 191 | 22 | 111 |
| Multi-row runs whose rows carry different technical scores | 526 | 191 | 367 |
| Median time covered by the 14-row volatility look-back: stored / one row per run | 13.0 h / 27.0 h | 18.0 h / 25.4 h | 8.2 h / 24.3 h |
| Median time covered by the 30-row technical look-back: stored / one row per run | 29.0 h / 57.0 h | 42.0 h / 59.1 h | 17.0 h / 54.0 h |
| Trailing volatility, stored ÷ one row per run: median (10th–90th percentile) | 0.84 (0.27–1.00) | 1.00 (0.50–1.00) | 0.73 (0.29–1.00) |

**Technical score (a direct model feature), recomputed offline with the worker's own formula** from the 30 rows
before each run, on the stored series and on the one-row-per-run series, for runs since duplicates began:

| | BTC | ETH | LINK |
|---|---|---|---|
| Runs compared | 935 | 471 | 1,062 |
| Absolute difference, median / 90th percentile / maximum (points, 0–100 scale) | 9 / 28 / 65 | 7 / 18 / 45 | 8 / 21 / 49 |
| Differ by 10 points or more | 49.3% | 36.7% | 41.1% |
| On opposite sides of 50 | 29.4% | 21.0% | 25.2% |

- In November 2026 every run of every coin has exactly two rows (94 runs each).
- Runs with three or more rows (BTC up to 37) occur in August–October only.

## 2. Technical interpretations

- **Cause of the two rows.** Each scheduled run executes the 24h and the 12h prediction chain, and each chain calls
  `logBtcData` / `logEthData` / `logLinkData`, which inserts unconditionally (`worker.js`).
- **Runs with three or more rows** are consistent with additional on-demand requests inside the same 10 minutes;
  no column records what wrote a row, so this is not confirmed per row.
- **Effect on the model's inputs.** `trailingVolatility` (14 rows) and the technical score (last 30 rows) count rows,
  not time. With two rows per run they cover about half the time they would with one, and every second
  period-over-period move is zero or near zero, which lowers the measured volatility (median ratio 0.84 for BTC,
  0.73 for LINK).
- **Different technical scores inside one run** follow from the second insert computing its score from a series that
  already contains the first insert.
- The series read by the analog and neighbourhood logic (`SELECT ts, btc_price FROM btc_data ORDER BY ts ASC`)
  contains the same repeated points.

## 3. Not measured

- The effect on any prediction, variant score, selection or accuracy. That requires recomputing predictions on a
  one-row-per-run series, which was not done.
- Whether the duplicates help or hurt predictive performance. **No claim is made either way.**

## 4. Prepared correction (this branch, off)

With `PRICE_ROW_PER_TICK="1"`, the scheduled-path insert becomes
`INSERT … SELECT … WHERE NOT EXISTS (SELECT 1 FROM <table> WHERE ts > now − 10 min)`: one atomic statement, so the two
concurrent chains cannot both insert. Predictions, selection and all other writes are unchanged
(`tests/price-row-per-tick.test.js`: flag unset → two rows per run; flag set → one row, all predictions still written).

- **Forward-only.** Existing rows are never touched. From the activation time onward the row-counted look-backs cover
  about twice the time, so volatility and technical-score inputs change scale at that moment.
- **Consequence.** It is a methodology change. Rows and predictions before and after activation are not directly
  comparable on those inputs; history used for analogs and calibration would mix both regimes for weeks.
- **Alternative: collapse on read.** Keep writing as today and make every reader use one row per run. That changes the
  inputs over the whole history consistently, but changes more code paths and alters all recomputed history at once.
- **Rollback.** Unset the variable; no migration, no data change.

## 5. Evaluation design before any activation

1. Offline, on the exported series only: recompute volatility and technical score on the one-row-per-run series.
   **Done for the inputs** (section 1). Still to do: how many of the core model's calls (p_up ≥ 0.5) would differ,
   which needs the model replayed on both series. No stored value is rewritten.
2. Report accuracy of both versions on resolved history as a description, with the count of independent windows.
   It is retrospective and cannot support a claim of improvement.
3. If activation is chosen: record the effective timestamp, add a version marker to predictions written after it, and
   treat it as a registered, versioned change.

## 6. Unresolved decision (owner)

- **D1.** Activate, and when. Activation inside the EXP-002 window (from 2026-11-01), the PEV-1P window or the EXP-23
  collection would change the production model's inputs while those protocols are measuring it.
- **D2.** Write-side (forward-only) or read-side (whole history) correction.

**Recommendation:** leave the flag off and the branch unmerged; run step 1 and 2 offline first; do not activate
before the EXP-002 checkpoint unless the offline result shows the duplicates materially distort the calls.
