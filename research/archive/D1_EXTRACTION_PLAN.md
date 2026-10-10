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

---

## Step 1, refined (2026-10-10): proposed for approval, NOT executed

**Goal:**
- Recover the 260 pre-2026-07-22 `btc_data` rows needed by the market-move B0 replay.
- Recover every other `btc_data` reading not yet archived.
- Secure the V1 history that `history`'s 500-row cap keeps deleting.

**Reuse:** the preserved extracts (btc_data 759 rows, 2026-08-26 10:23 → 10-06 18:00; V1 archive 605 rows to
2026-10-09 12:19) are not re-downloaded for their own sake. Their overlap with the new extract serves only as a
revision check.

**Database:** production D1 `sentiment-history`. Read-only `SELECT`/`EXPLAIN` only. Run after a midnight-UTC
reset, one statement at a time, checking `meta.rows_read` after each. **Stop** if any statement reads more than
2× its estimate or the step exceeds **5,000 rows read**.

| # | SQL (exact) | Index / expected plan | Rows returned (expected) | Rows read (est.) |
|---|---|---|---|---|
| 1.0 | `EXPLAIN QUERY PLAN SELECT id, ts, btc_price, technical_score FROM btc_data WHERE id <= 1000000000 ORDER BY id` (and the same for 1.3–1.4) | n/a: plans only, reads no table rows | plan rows | ~0 |
| 1.1 | `SELECT type, name, tbl_name, sql FROM sqlite_master WHERE tbl_name IN ('btc_data','history','research_sentiment_archive') ORDER BY name` | `SCAN sqlite_master` | ~8 (3 tables + their indexes) | ~80 (whole schema table) |
| 1.2 | `SELECT MAX(id) AS max_id, MAX(ts) AS max_ts FROM btc_data` | `SEARCH btc_data USING INTEGER PRIMARY KEY` for `MAX(id)`; `MAX(ts)` via `idx_btc_data_ts` (last entry) | 1 | ~2 |
| 1.3 | `SELECT id, ts, btc_price, technical_score FROM btc_data WHERE id <= :max_id ORDER BY id` (`:max_id` from 1.2) | `SEARCH btc_data USING INTEGER PRIMARY KEY (rowid<?)`, no sort | ≈ 2,560 (2,506 counted 2026-10-09 14:51 + ≤ 16/day) | ≈ 2,560 |
| 1.4 | `SELECT * FROM history ORDER BY ts` (columns confirmed by 1.1 first) | `SCAN history USING INDEX idx_ts` | 500 (cap) | ~500 |
| 1.5 | `SELECT * FROM research_sentiment_archive WHERE observation_ts > 1791548387600 ORDER BY observation_ts` | `SEARCH research_sentiment_archive USING INDEX idx_research_sentiment_archive_observation_ts (observation_ts>?)` | ≤ ~30 (rows archived since 2026-10-09 12:19) | ≈ rows returned |
| **Total** | | | | **≈ 3,150 rows read: 0.06% of the 5,000,000/day free allowance** |

**Why the whole of `btc_data`, not just 260 rows:**
- The table is about 2,560 rows.
- One PK-ordered read gets the 260 B0 rows (ts < 1784678400000), the unarchived 2026-07-22 → 08-26 and
  post-10-06 readings, and the 759 overlap rows.
- The overlap is compared with the preserved extract to detect upstream revisions or backfills.
- Reading only the 260 rows (`WHERE ts < 1784678400000`, `SEARCH USING INDEX idx_btc_data_ts`) would cost about
  260 rows read. It remains an option if you prefer the minimum.

**Output schema and checks** (offline, before anything is stored):
- **Format:** each statement's `results` saved as a JSON list of row objects, exactly as returned, in
  `research/archive/session_extracts/d1_<date>/`: `btc_data.json`, `history.json`, `research_sentiment_archive_new.json`.
- **MANIFEST.json** records per file: the SQL, the database ID, extraction UTC, `meta.rows_read`, `meta.served_by`,
  row count, SHA-256 of the file bytes and SHA-256 of `json.dumps(rows, sort_keys=True)`.
- **B0 rows** (`ts < 1784678400000`) must reproduce the aggregates measured before: count **260**,
  sum(ts) **463687966800000**, sum(btc_price) **17075341**, min ts **1777939200000**, max ts **1784674800000**.
- **Overlap:** the 759 rows in 2026-08-26 10:23 → 10-06 18:00 must equal `inputs/btc_data.json` (same ts and
  price). Any difference is reported as an upstream revision and kept, never "fixed".
- **History:** `history` rows that also exist in the archive (same ts) must carry the same score and sources.
- **T-A1/T-A2:** V1 rows after 2026-10-09 16:00 are prospective data. They are archived only, and nothing is
  computed from them outside the approved progress evaluation.

**Archiving and verification in Drive:**
- **Catalog:** the files get a new catalog entry `d1_extracts_<date>` (FROZEN_SNAPSHOT, `snapshot_by: content`).
- **Upload:** the planner adds them as a new snapshot, and the mirror uploads them with SHA-256 checks and records
  them in the next state manifest. Restore verifies them like any other file.
- **Public route:** by default they pass through the public repository, as the existing extracts did.
- **Private route:** the files are small (about 0.3 MB), so they can instead be uploaded to Drive by hand and
  added to the feed as `baseline_adopt`.

Nothing in this step is run until you approve it explicitly, as written or amended.
