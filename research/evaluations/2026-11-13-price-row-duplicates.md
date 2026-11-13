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

## 3. Model replay (2026-11-13)

**Method.** `research/price_dup_replay/replay-arm.mjs` runs the Worker's own code (`runPrediction`,
`runEthPrediction`, `runLinkPrediction`, the resolution functions, the challenger and `selectBestVariant`, taken
from the unmodified `worker.js`) in a local SQLite sandbox, once per arm:

- **Stored series:** every exported price row with its stored technical score.
- **One row per run:** the first row of each run; from each coin's first multi-row run onward the technical score is
  recomputed with the Worker's formula from the 30 prices before it.

Both arms predict at every (coin, horizon, timestamp) where production stored a prediction (4,017 cycles, no
failures), with the clock set to that timestamp. Everything else is identical: code, configuration, sentiment
context, cycle list. Each arm builds its own prediction history, calibration and selection as it goes. Both arms are
scored against the **same outcome: production's stored `realized_up`** for the cycle
(`research/price_dup_replay/compare.py`). Full figures: `2026-11-13-price-row-replay-report.json`.

**Reported window:** cycles from 2026-10-09T21:00Z (the end of the sealed period) to the export. Earlier cycles were
replayed to build each arm's history but no performance figure for them was produced or looked at.

### 3.1 Core model (`p_up`), call is UP when p_up ≥ 0.5

| Unit | Cycles compared | Changed calls | Median / 90th pct. abs. p difference | Accuracy: stored → one row | Balanced accuracy | Brier | Changed calls right: stored / one row | Non-overlapping windows |
|---|---|---|---|---|---|---|---|---|
| BTC 12h | 255 | 90 (35.3%) | 0.13 / 0.32 | 46.0% → 47.6% | 46.6% → 48.3% | 0.313 → 0.284 | 42 / 46 | 57 |
| BTC 24h | 258 | 89 (34.5%) | 0.13 / 0.33 | 49.8% → 52.6% | 51.5% → 54.6% | 0.323 → 0.291 | 39 / 46 | 30 |
| ETH 12h | 242 | 90 (37.2%) | 0.17 / 0.41 | 45.3% → 47.4% | 48.3% → 50.5% | 0.310 → 0.312 | 40 / 45 | 56 |
| ETH 24h | 253 | 91 (36.0%) | 0.14 / 0.40 | 51.9% → 41.9% | 56.6% → 45.1% | 0.293 → 0.299 | 55 / 31 | 31 |
| LINK 12h | 248 | 83 (33.5%) | 0.12 / 0.37 | 47.5% → 50.0% | 48.3% → 50.3% | 0.319 → 0.275 | 37 / 43 | 56 |
| LINK 24h | 253 | 95 (37.6%) | 0.13 / 0.33 | 45.6% → 49.0% | 46.5% → 48.5% | 0.317 → 0.287 | 40 / 48 | 31 |

All six units: 1,509 cycles compared, **538 changed calls (35.7%)**. Among the 512 changed calls with an outcome, the
stored-series arm was right 253 times and the one-row arm 259 times.

### 3.2 By direction (resolved cycles; precision / recall)

| Unit | UP, stored | UP, one row | DOWN, stored | DOWN, one row |
|---|---|---|---|---|
| BTC 12h | 42.6% / 53.1% | 44.1% / 55.8% | 50.5% / 40.0% | 52.4% / 40.7% |
| BTC 24h | 43.3% / 62.5% | 45.8% / 67.3% | 59.8% / 40.6% | 63.8% / 42.0% |
| ETH 12h | 42.9% / 73.8% | 44.3% / 75.7% | 52.6% / 22.9% | 56.9% / 25.2% |
| ETH 24h | 45.3% / 82.8% | 37.6% / 62.6% | 71.7% / 30.3% | 51.3% / 27.5% |
| LINK 12h | 44.9% / 59.5% | 46.5% / 54.1% | 51.6% / 37.2% | 54.1% / 46.5% |
| LINK 24h | 37.6% / 51.0% | 39.1% / 45.9% | 55.6% / 42.0% | 57.9% / 51.1% |

### 3.3 Other variants, calibration and selection

- `calibrated_p_up` and the three challenger outputs produce the same calls as `p_up` in both arms in every unit, so
  their changed-call counts and accuracies equal the table above.
- `p_up_experimental`: 71–106 changed calls per unit (31–43%); accuracy stored → one row: BTC 12h 44.7% → 50.8%,
  BTC 24h 50.6% → 52.6%, ETH 12h 50.0% → 48.6%, ETH 24h 49.0% → 43.6%, LINK 12h 48.2% → 47.8%, LINK 24h 44.0% → 49.0%.
- `calibrated_conditional_p_up` (BTC only): 46 (12h) and 64 (24h) changed calls; 47.6% → 50.0% and 47.8% → 50.2%.
- **Selection.** The chosen variant differs in 1–6 cycles per unit for BTC and LINK and in 20 (12h) and 2 (24h) for
  ETH; `original` is chosen in at least 93% of cycles in both arms. The significance gate clears a different number
  of times: BTC 24h 1 → 15, BTC 12h 3 → 1, ETH 12h 15 → 30, ETH 24h 21 → 8, LINK 12h 4 → 14, LINK 24h 6 → 19.
- Stored `chosen_p_up` direction differs in 51–95 cycles per unit; its accuracy against the cycle outcome, stored →
  one row: BTC 12h 45.0% → 47.1%, BTC 24h 54.6% → 49.3%, ETH 12h 43.5% → 48.0%, ETH 24h 44.6% → 42.7%,
  LINK 12h 46.8% → 50.2%, LINK 24h 53.9% → 46.6%. (`chosen_p_up` carries the latest-resolved semantics.)

### 3.4 What this supports

- **Established:** the duplicate rows change the model's output materially. About one call in three would be
  different on a one-row-per-run series, in every asset and horizon, and the selection gate behaves differently.
- **Not established:** that either series predicts better. Accuracy is higher for one row per run in five units by
  1.6–3.3 points and lower by 10.0 points in ETH 24h; on changed calls the two arms are right equally often (253 vs
  259). Every accuracy in the table is within a few points of 50%, on 30–57 non-overlapping windows per unit. This is
  retrospective and overlapping; **no claim of predictive improvement is made.**

### 3.5 Limitations

- **Fidelity.** The stored-series arm reproduces production's stored `p_up` exactly in 55–87% of cycles and the same
  call in 90.5–95.7%. It is a close replay, not an exact reconstruction.
- **Sentiment context.** `history` (V1 sentiment) keeps only its latest 500 rows (from 2026-09-24). The replay uses
  those rows as of each cycle; what the table contained on a past date is not recoverable. Same in both arms.
- **Unavailable inputs.** The digest input of the challenger and the Gemini-bias resolution were not replayed
  (external calls are blocked in the sandbox). Same in both arms.
- **Concurrency.** Production runs the 24h and 12h chains concurrently; the replay runs cycles in timestamp order.
- **Rows from on-demand requests** inside a run are dropped with the scheduled duplicate in the one-row arm; they
  cannot be told apart.
- **Overlapping windows.** Predictions every 3 hours over 12h/24h horizons are not independent.
- The one-row arm drops 5 ETH and 3–4 BTC/LINK 12h cycles that had too little data at that moment.

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

## 5. Evaluation status

1. Inputs recomputed offline (section 1): done.
2. Model replayed on both series with the Worker's code (section 3): done.
3. Prospective evidence: not available; it requires both versions to run forward in time.

## 6. Recommendation and unresolved decision (owner)

**Recommendation.**
- Keep `PRICE_ROW_PER_TICK` off and this branch unmerged. The replay gives no performance reason to change the
  production model inside the EXP-002, PEV-1P and EXP-23 windows, and a change there would alter what those
  protocols measure.
- The correction is justified on correctness (look-backs meant as time are halved), not on accuracy.
- **Do not use the forward-only write flag as the correction.** It would leave about half of the stored history
  duplicated while new rows are single, so look-backs and stored technical scores would mix two regimes for months.
- **Preferred form: deduplicated reads with the technical score recomputed at read time**, as a new model version
  logged in shadow beside the production model (as the challenger is), with its own registered, prospective
  comparison. That keeps history untouched, is consistent over the whole series, and produces out-of-sample evidence
  before any switch. This is new work and a registered experiment; it is not started.

**D1 (owner).** Whether to register and build that shadow version now, or leave the issue documented until the
EXP-002 checkpoint.
