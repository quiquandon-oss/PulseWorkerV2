# Missing historical production data: per-table extraction plan (NOT executed)

Status: **plan only. No production D1 query was run to prepare it, and none may run until the owner approves a
specific step below.** It was built from the repository and from figures already measured on 2026-10-08/09:
- **Code read:** migrations `.ai/migrations/*.sql`, the writes in `worker.js`, the experiment code and workflows.
- **Figures already measured:** read-only query metadata, `data-exports/STATUS.md` and the extract manifests.

## Ground rules for any approved extraction

1. **Timing:** run after the midnight-UTC reset, sequentially, one statement at a time, at most one step per day.
2. **Query shape:** single-table `SELECT` only. No JOIN, no correlated subquery, no `json_each`/`json_extract`,
   no `LIKE`, and no `COUNT(*)` over a whole table.
3. **Large rows:** paginate on the primary key (`WHERE id > :last ORDER BY id LIMIT 500`), so rows read equals rows
   returned.
4. **Stop rule:** check `meta.rows_read` after every statement, and stop the whole step at once if it exceeds
   **2 × the estimate**.
5. **Step budget:** a hard cap of **50,000 rows read** per step, 1% of the 5,000,000/day free allowance.
6. **Store results** byte for byte in `research/archive/session_extracts/` style: SQL, time, `rows_read`,
   sha256. Never write to D1.
7. **Production's own reads:** the Worker already reads unbounded full tables on every 3-hour chain. Migration
   0001 estimates "~76,000 rows/day". The owner can check the day's usage in the Cloudflare dashboard (D1 →
   Metrics) before a step; a step is skipped if usage already looks abnormal.

## Why not a full-database export

`wrangler d1 export` reads every row of every table. That includes large text tables research does not need
(`gemini_*`, `daily_report_cache`, `analyst_relay_log`, `backtest_results` …) whose sizes are unknown. Its
row-read cost therefore cannot be estimated in advance. Cloudflare documents that an export can block other
requests to the database while it runs; confirm in the D1 docs before ever considering it. All research inputs
fit in about **21,000 rows read** with the bounded statements below, so a full export is **not necessary**.

## Step 0: schema and size probes (P0, about 110 rows read)

These settle the two open questions: which migrations are applied, and the exact columns of the core tables
(which have no `CREATE TABLE` in the repo).

| SQL | Rows read (est.) | Basis |
|---|---|---|
| `SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name` | ~80 | one row per table, index and view: about 35 tables + 40 indexes from code and migrations |
| for each research table T: `SELECT MAX(rowid) AS max_rowid FROM T` | 1 each, ~25 | rowid max is a B-tree seek, not a scan; an upper bound on the row count for append-only tables |

Do **not** use `SELECT COUNT(*) FROM T` for sizing: it scans the table.

## Steps 1–3: the tables

Units: rows read = rows the statement scans (D1 bills scans, not results). Archived = rows already preserved
offline (git or Drive package).

| # | Table → columns | Range | Needed rows (basis) | Archived | SQL (all `ORDER BY` on PK or indexed ts) | Index used | Est. rows read | Incremental? | Recoverable without D1? | Priority / why |
|---|---|---|---|---|---|---|---|---|---|---|
| 1a | `btc_data` → ts, btc_price, technical_score | all (2026-05-05 → now) | ~2,520 (2,506 measured 2026-10-09 14:51; +16/day) | 759 (08-26..10-06) | `SELECT id, ts, btc_price, technical_score FROM btc_data ORDER BY id` | PK | ~2,550 | yes: `WHERE id > :max_id` | no: production readings, including backfilled rows, are what B0 replays | **P1**: market-move B0 (260 pre-2026-07-22 rows), EXP-004, EXP-005/009/010 and outcome replays. Covers the 260-row B0 extract. |
| 1b | `history` → id, ts, score, btc_price, sources_json, technical_score, gold_regime, regime_mag, bottom_score, global_mcap | whole table (capped at 500 newest rows) | 500 (measured) | 474 of the frozen-window timestamps (via the archive), without regime_mag/bottom_score | `SELECT * FROM history ORDER BY ts` | idx_ts | ~500 | yes: `WHERE ts > :max_ts` | partly: score/sources survive in `research_sentiment_archive`; regime_mag, bottom_score, global_mcap do not | **P1**: EXP-005/009/010 inputs. **Rows older than the cap are gone for good**, so each later extract preserves what the cap will delete. |
| 1c | `research_sentiment_archive` → all 12 columns | observation_ts > 1791548387600 (after the 605 archived) | ~5–10/day since 10-09 12:19 | 605 (08-27..10-09 12:19) | `SELECT * FROM research_sentiment_archive WHERE observation_ts > 1791548387600 ORDER BY observation_ts` | unique observation_ts | ≈ rows returned (~20) | yes | no | **P1**: the only V1 history that survives the 500-row cap. ⚠ Rows after 2026-10-09 16:00 are T-A1/T-A2 prospective data: archive them only, never compute interim results outside the approved progress mechanism. |
| 1d | `research_events` → all | all | 17 (smoke test, 2026-10-09) | 15 | `SELECT * FROM research_events ORDER BY event_id` | PK | ~17 | yes: `WHERE event_id > 15` | no | **P1**: EXP-009, Stage 7, learning loop, B0 comparison |
| 1e | `research_event_evidence` → all 11 columns | all | unknown; RSS articles per event (estimate ≤ 2,000; probe Step 0) | 0 | `SELECT * FROM research_event_evidence WHERE evidence_id > :last ORDER BY evidence_id LIMIT 500` (repeat) | PK | = rows (≤ 2,000) | yes | no | **P1**: EXP-009 evidence joins and Stage 7; nothing archived |
| 1f | `selection_decisions_anomaly` → all | all | ≤ ~100 (0–2/day, BTC only, since EXP-002 start) | 0 (not exported) | `SELECT * FROM selection_decisions_anomaly ORDER BY rowid` | rowid | ≤ ~100 | yes: `WHERE rowid > :max` | no | **P1**: EXP-002's only output table |
| 1g | `research_experiment_registry` → all | all | 7 (EXP-004..010) | 0 | `SELECT * FROM research_experiment_registry ORDER BY experiment_id` | PK | 7 | n/a | partly (seed SQL in migrations 0010–0014) | **P1**: authoritative EXP status |
| 2a | `predictions` → id, features_json (the only column missing from the CSV) | all | 1,191 (STATUS.md) | 1,191 rows minus features_json | `SELECT id, features_json FROM predictions WHERE id > :last ORDER BY id LIMIT 500` (×3) | PK | ~1,200 | yes | no | **P2**: replaying EXP-002/003 selection neighbourhoods |
| 2b | `challenger_predictions` → id, trend_strength, foufi_digest_video_id | all | 1,797 | 1,797 rows minus these 2 columns | `SELECT id, trend_strength, foufi_digest_video_id FROM challenger_predictions ORDER BY id` | PK | ~1,800 | yes | no | **P2**: EXP-001 reads trend_strength |
| 2c | `eth_data` → ts, eth_price, technical_score | all | 910 (measured 2026-10-09) | 0 | `SELECT id, ts, eth_price, technical_score FROM eth_data ORDER BY id` | PK | ~920 | yes | no | **P2**: EXP-001/003 for ETH |
| 2d | `link_data` → ts, link_price, technical_score, funding_adj | all | 2,212 (measured 2026-10-09) | 0 | `SELECT id, ts, link_price, technical_score, funding_adj FROM link_data ORDER BY id` | PK | ~2,230 | yes | no | **P2**: EXP-001/003 for LINK |
| 2e | `experiment_4_timesfm` → all | all | ~140 (2/day since Aug) | 0 | `SELECT * FROM experiment_4_timesfm ORDER BY id` | PK | ~140 | yes | no | **P2**: EXP-004 results |
| 2f | `research_analyses` → listing, then rows | all | < 60 rows, but metric_json is ~220 KB each | 0 | list: `SELECT analysis_id, analysis_ts, subject, sample_size, validation_status, length(metric_json) AS n FROM research_analyses ORDER BY analysis_id`; then per row `SELECT * FROM research_analyses WHERE analysis_id = :id` | PK | ~60 + 1 per row | yes | no | **P2**: historical EXP-005/009/010 outputs. One row per call keeps responses small. |
| 3a | `link_predictions` → id, features_json | all | 997 | 997 rows minus features_json | as 2a | PK | ~1,000 | yes | no | P3 |
| 3b | `selection_decisions` → id, neighborhood_json | all | 1,150 | 1,150 rows minus neighborhood_json | as 2a | PK | ~1,150 | yes | no | P3 |
| 3c | `research_hypotheses` → all | all | < 100 (estimate) | 0 | `SELECT * FROM research_hypotheses ORDER BY hypothesis_id` | PK | < 100 | yes | no | P3: Experiment 5 hypotheses |
| 3d | `calibration_curve`, `challenger_calibration_curve` → all | all | ≤ 60/day each since Aug (≤ ~3,600 each, probe first) | 0 | `SELECT * FROM calibration_curve ORDER BY rowid` (and challenger) | rowid | ≤ ~7,200 total | yes: `WHERE computed_ts > :max` (no index: use rowid) | partly (recomputable from predictions) | P3 |
| 3e | `stage7_*`, `learning_candidates`, `v1_methodology_versions` | all | small; tables may not exist in production (0021 says staging only; Step 0 tells) | 0 | `SELECT * FROM <table> ORDER BY <pk>` | PK | < 100 each | n/a | no | P3, **read-only**. Reading Candidate #1's row never changes its state. |

**Totals (estimates):**
- Step 0: ~110 rows read.
- P1 (1a–1g): ~5,200, about 0.10% of the daily allowance.
- P2 (2a–2f): ~6,400.
- P3: ~9,600.

**All steps: about 21,000 rows read, about 0.4% of one day's allowance.** For comparison, the audit query that
exhausted the quota read 8,184,077 rows.

## Not recoverable from D1 at all

- **V1 history before 2026-08-27.** `history` keeps only the newest 500 rows, and the append-only archive starts
  on 08-27. The older rows were deleted by V1's own cap. Only an outside copy could restore them; there is none
  in these repositories.
- **Earlier states of `realized_*` and other mutable columns.** They exist only in the git history of
  `data-exports/*.csv`, about one version a day since 2026-09-20. The Drive package's `learning_exports` keeps
  the current version; earlier versions can be added from git without D1.

## Recommended order (each needs its own approval)

1. **Step 0 + P1 together:** under 5,500 rows read. This unblocks the market-move B0 baseline and secures the
   cap-limited V1 history.
2. **P2**, on a later day.
3. **P3**, only if a study needs it.

After that, re-extract only incrementally (`WHERE id > :max` / `ts > :max`), roughly weekly, so every later
extraction reads only new rows.
