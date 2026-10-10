# CryptoPulseV2 research archive: Google Drive assessment

Status: **design and offline prototype only.** Nothing was sent to Google Drive and no production D1 query was
run. Branch `claude/epic-planck-uyapsw-archive` (from `main` b48fa33). Measured 2026-10-10 from git objects and
local files.

Contents: tool `archive.py` (inventory, build, verify, sync-plan), `offline_queries.py` (DuckDB),
`catalog.json` (dataset registry), `session_extracts/` (D1 extracts preserved from a session container),
`test_archive.py` (13 tests).

---

## A. Dataset inventory

Every row below was measured by `archive.py inventory`. "Pins" are the frozen hashes, recomputed with the rule
each study used. All 11 match.

| Dataset (catalog id) | Location | Format | Date range (UTC) | Rows | Size raw / Parquet | Status | Known gaps |
|---|---|---|---|---|---|---|---|
| T-A1/T-A2/T-A3 registration, addendum 1, erratum 1 (`prereg_v1_candidates`) | `claude/epic-planck-uyapsw` 916bb7d | JSON | registered 2026-10-09 16:00 | n/a | 15 KB | PREREGISTRATION; 3/3 pins OK (`66067b32…`, `c2ebdfcb…`, `5397fe70…`) | n/a |
| OI forward pre-registration (`prereg_oi_forward`) | `claude/sweet-meitner-66ntx8` 720f6fb, constant `PREREGISTRATION` in `research/risk_regime_forward_eval.py` | Python literal + MD | eval start 2026-10-10 00:00 | n/a | 30 KB | PREREGISTRATION; pin `46c0d52b…` OK (literal read with `ast`, module never executed) | n/a |
| Market-move definition and plan (`prereg_market_moves`) | `claude/epic-planck-uyapsw-market-moves` 196b87e | JSON | sealed evaluation 2026-07-22 to 2026-10-09 21:00 | n/a | 12 KB | PREREGISTRATION; 2/2 pins OK | n/a |
| Hyperliquid 1h candles BTC/ETH/LINK (`mm_candles_1h`) | `research-data/market-moves` 0eb5be0 | JSONL | open 2026-03-23 22:00 to 2026-10-09 20:00 | 14,397 (4,799 per asset) | 10.5 MB / 2.1 MB | FROZEN_SNAPSHOT, pinned by the evaluation plan | none. The revision check was only a same-hour re-fetch. From 2026-07-22 the data is sealed. |
| Market-move run records and quality report (`mm_collection_records`) | same | JSON | 2 runs, 2026-10-09 | n/a | 16 KB | RUN_RECORD | n/a |
| Risk-regime research series (`rr_raw_observations`) | `claude/sweet-meitner-66ntx8` 720f6fb | JSONL.gz | 2026-07-20 to 2026-10-08 16:40 | 63,809 in 66 series | 1.55 MB (29 MB uncompressed) / 1.56 MB | FROZEN_SNAPSHOT | Series start dates vary: DeFiLlama 07-20, Hyperliquid 08-18, GDELT events 08-24. GDELT DOC covers event windows only (no data 08-22 23:15 to 09-20). Deribit options are live-only (14 rows). Bybit and Binance live APIs are geo-blocked. |
| GDELT DOC articles (`rr_gdelt_doc_articles`) | same | JSONL.gz | first_seen 2026-08-19 to 09-28 | 14,223 | 1.40 MB / 0.83 MB | FROZEN_SNAPSHOT | same windows as above |
| Binance archive OI (5m) and funding (8h), plus all HTTP responses (`rr_oi_collection`) | same | JSONL.gz + manifest | OI 2026-01-01 to 10-07 23:55; funding to 09-30 16:00 | 162,099 obs + 295 responses | 11.9 MB (177 MB uncompressed) / 11.2 MB | FROZEN_SNAPSHOT | none inside its range. 6 refused requests are recorded (403/451/404). |
| Risk-regime forward store (`rr_forward_store`) | `research-data/risk-regime-forward` 072a64a | daily JSONL.gz partitions (normalized + raw), meta, runs, index | 2026-10-06 08:00 to 10-08 23:55 | 1,856 normalized + 721 raw | 111 KB / 110 KB | PROSPECTIVE_APPEND_ONLY (daily 10:40 collector) | Store begins 10-06. Binance OI only from 10-08. OI evaluation counts data from 10-10 only. |
| Study outputs (`rr_study_outputs`) | `claude/sweet-meitner-66ntx8` | JSON/HTML (16 files) | per study | n/a | 21.2 MB | FROZEN_SNAPSHOT (derived) | input hashes are recorded inside |
| T-A1/T-A2 progress runs (`v1_prospective_runs`) | `claude/epic-planck-uyapsw` | JSON | 2026-10-09 17:48 | n/a | 12 KB | RUN_RECORD | n/a |
| Learning exports (`learning_exports`) | `main` `data-exports/` (last export a916855, 2026-10-09 14:45) | CSV ×6 | 2026-08-01 to 10-09 | 5,455 | 1.68 MB / 0.45 MB | MUTABLE_EXPORT, overwritten daily by `export-learning-data.yml` | Earlier versions exist only in git history (21 commits since 09-20). `realized_*` values change after resolution. |
| Production D1 extracts (`d1_extracts_2026_10_08`) | **this branch**, `research/archive/session_extracts/` | JSON ×5 + MANIFEST | btc_data 08-26 to 10-06; predictions 08-27 to 10-05; V1 08-27 to 10-06; V1 archive 08-27 to 10-09 12:19; 15 research_events | 2,144 | 368 KB / 71 KB | FROZEN_SNAPSHOT; 5/5 pins OK, the 4 study inputs match `risk_regime_history.json` | Was **only in a session container**, now preserved byte for byte. Does not include the 260-row btc_data extract that B0 needs. |
| Hyperliquid page caches (`hyperliquid_page_caches`) | this branch, `.tar.gz` | 31 raw API pages | 2026-08 to 10-06 | n/a | 295 KB | FROZEN_SNAPSHOT | Was session-only. Hyperliquid serves only recent candles, so these pages may not be re-fetchable. |
| GDELT 2.0 event export zips (`gdelt_raw_cache_session`) | session container only | ZIP ×4,437 | 2026-08-19 00:00 to 10-06 06:15 | n/a | 293 MB | EPHEMERAL_LOCAL | Not in git (too large). The provider still serves the same MD5-listed files. Best candidate for the first Drive upload. |

Not archived and out of scope: production D1 itself (9.55 MB on 2026-10-09 21:12, the live source). The four
front-end repositories hold no datasets; their only data files are web `manifest.json` files.

## 3. What can be reused without querying D1

- **Fully offline** (in archive form; see the GDELT index gap in section 9): all three pre-registrations; market-move candles (development period only until an
  authorised evaluation run); all risk-regime series, OI and forward partitions; study outputs; the learning
  exports at their latest daily commit; and the preserved D1 extracts (btc_data 08-26 to 10-06, predictions,
  research_events 1–15, V1 scores 08-27 to 10-09).
- **Still needs D1, once and with approval:**
  - the market-move B0 replay input (260 btc_data rows before 2026-07-22);
  - any production rows newer than the extracts (V1 after 10-09 12:19, research_events after #15);
  - tables never extracted (`history`, `eth_data`, `link_data`, `research_hypotheses`,
    `research_event_evidence`, …).

  The cheapest route is a single approved `wrangler d1 export` dump after a row estimate (section F4). From then
  on, history comes from the archive.

## B. Storage and growth

**Now:** 4,562 files and 358.8 MB staged. 292.7 MB of that is the GDELT zips; without them the total is about
66 MB. The Parquet copies add 17.4 MB.

Daily and annual growth. Each figure is measured, and the assumption behind it is stated.

| Stream | Measured basis | Per day | Per year |
|---|---|---|---|
| Forward store (running) | 65,563 B for 2026-10-08, the first full day with all three sources, plus about 1 KB per run record | ~66 KB | ~24 MB |
| Learning exports, kept as daily raw snapshots | 1,673,823 B on 10-09, growing 14.2 KB/day (1,404,631 B on 09-20 → 10-09) | 1.67 MB, rising | ~1.56 GB (≈ 365 × 1.67 MB + 14.2 KB × 365²/2) |
| ↳ same, gzip-compressed (5.06× measured) | | ~0.33 MB | ~0.31 GB |
| ↳ same, weekly snapshots | | | ~0.22 GB raw |
| Market-move candles, *if* collection becomes prospective (not authorised yet) | 732 B/row JSONL, 147 B/row Parquet, 72 rows/day | 53 KB | ~19 MB raw, ~4 MB Parquet |
| GDELT raw zips, *if* collection continues (not running now) | 293 MB over 46.8 days | ~6.3 MB | ~2.3 GB |
| Ad-hoc D1 extracts | KB-scale per approved extract | | < 10 MB |
| Production D1, for reference (not archived) | 9,465,856 → 9,551,872 B over 32.1 h (4 KB page granularity) | ~64 KB | ~23 MB |

Year-one projection:
- **Running streams only:** about 0.35 GB (forward store, gzipped daily exports, candles).
- **Worst case:** about 4.3 GB (raw daily exports, GDELT, candles).

That is at most about 1.3% of the 320 GB. The 320 GB is shared with Gmail and Photos, so the plan should alert
at 80% of a budget the owner sets, rather than relying on the nominal figure.

## C. Parquet schema and DuckDB workflow

**Principle:** Parquet is a query copy. The archived raw file stays the source of truth. Every Parquet file keeps
the original fields with their exact names and values, and can rebuild its source rows exactly (`verify` checks
this).

- **Original columns:** exact copies of the source fields. Strings stay strings: prices and rates stay decimal
  text such as `"86002.10"` and `"1.25e-05"`, so classification can use `Decimal`. Columns with mixed or nested
  values are stored as canonical JSON text, and their names are listed in the file metadata.
- **Helper columns** (prefixed `_`):
  - `_ts_ms`, `_available_at_ms`, `_retrieved_at_ms` (int64 epoch ms);
  - `_value_f64` / `_c_f64` … (float64, convenience only);
  - `_absent_<col>` when a field is missing in some rows;
  - `_line` (row order in the source file).
- **File metadata:** `dataset_id`, `source_path`, `source_sha256`, `rows`, `rows_digest` (sha256 over the
  ordered row hashes), `original_columns`, `json_columns`, `tool`.

Tables:

| Table | Grain | Key columns (originals) | Partition path |
|---|---|---|---|
| `observations` | one value of one series | `source, provider, instrument, metric, interval, timestamp, available_at, retrieved_at, value, unit, coverage_status, historical_or_live, source_url, raw` | `dataset=<id>/snapshot=<id>` or, for append-only stores, `dataset=<id>/partitions/` |
| `candles_1h` | one closed candle | `asset, market, open_ts, close_ts, available_at, o,h,l,c,v, n, request, retrieved_at, raw, raw_sha256, collector_version` | as above |
| `articles` | one GDELT DOC article | `topic, url, title, domain, first_seen, available_at, retrieved_at, query_url` | as above |
| `d1_extract` | one row of a read-only D1 extract | the table's own columns | `dataset=<id>/snapshot=<id>` |
| `d1_export` | one row of a learning export CSV | the CSV header (text) | one snapshot per export version |

Point-in-time rule: queries filter on `_available_at_ms ≤ t` (or use an `ASOF` join), never on `_ts_ms` alone.

**Can DuckDB replace the historical D1 queries? Yes, for research. Not for the live Worker.**

| Historical query run against D1 | Offline replacement (`offline_queries.py`) | Result |
|---|---|---|
| Monthly gap/coverage query (142,396 D1 rows read) | candle coverage per asset and month; development rows only, no returns | 0 missing hours |
| B0 `not_evaluable` check (btc_data readings per UTC day) | day histogram over the preserved extract | 42 days, 6–31 readings/day, 0 empty |
| Repeated `COUNT` / resolution checks on prediction tables | aggregate over learning-export Parquet | e.g. btc 1,185 of 1,191 resolved |
| "Latest value available at V1 time" joins | DuckDB `ASOF JOIN` on `_available_at_ms` | 575/575 V1 rows matched, max age 59.9 min |
| Production event detectors (`research/event_detector.py`, unchanged) | Parquet → in-memory SQLite `btc_data` → `detect_large_moves`, `detect_regime_reversals`, `detect_volatility_expansion` | **8/8 production events in the window reproduced exactly**, plus 1 extra (below) |

- Python research modules already take a `sqlite3` connection, so they run unchanged on an in-memory database
  built from Parquet.
- The correlated-subquery audit that exhausted the D1 quota (8.18 M rows) would cost nothing offline.
- The live Worker still reads D1. That stays as is.

**Observation, not a finding:** the unchanged production algorithm, run offline on the btc_data extract, also
fires `LARGE_MOVE|1788480028536|UP` (2026-09-04 00:00 UTC). It is not in production `research_events`. This was
not investigated further.
- No metric was computed from it.
- Nothing was compared with reference episodes.
- The sealed market-move evaluation was not touched: candle queries read only rows available before 2026-07-22,
  and a test enforces that.

## D. Google Drive integration design

**Folder layout** (one private folder in the owner's My Drive, link sharing off):

```
CryptoPulseV2-Research-Archive/
  00_preregistrations/<dataset>/snapshot=<date>_git-<commit>/...     byte copies of frozen rules
  10_raw/frozen/<dataset>/snapshot=<id>/...                          study inputs and outputs, byte copies
  10_raw/prospective/<dataset>/partitions/...                        append-only partitions, each stored once
  10_raw/prospective/<dataset>/snapshot=<id>/...                     mutable files (index) per sync
  10_raw/exports/<dataset>/snapshot=<id>/...                         one snapshot per export version
  10_raw/session_extracts/<dataset>/snapshot=local-<sha>/...         files that existed only in a container
  20_parquet/<table>/dataset=<id>/(snapshot=<id>|partitions)/*.parquet
  30_run_records/<dataset>/snapshot=<id>/...
  90_manifests/<dataset>__<snapshot>.json, catalog.json
  SHA256SUMS                                                         uploaded last
```

The local staging tree built by `archive.py build` has exactly this layout. Snapshot ids carry the git commit,
so where every file came from stays traceable.

**Safe initial synchronisation**
1. Build locally: `archive.py build`.
2. `verify` must return `ok` (checksums, pinned hashes, exact Parquet round trips, credential scan).
3. `sync-plan` lists the uploads (4,562 files, 358.8 MB now).
4. Upload the tree as is, with file conversion off (CSV must not become Sheets).
5. List the remote tree with its checksums. Drive returns `sha256Checksum`/`md5Checksum` for uploaded files.
   Re-run `sync-plan` against that listing: it must show 0 uploads and 0 conflicts.
6. Download the folder once, then run `verify` on the download.

Rules:
- Never overwrite: a remote file with different bytes is a conflict, reported for a human to decide.
- Never delete remotely.
- `SHA256SUMS` goes last, so a partial upload is recognisable.

**Manual or automated**

| | Manual (Drive web or Drive for desktop) | Automated (Drive API v3) |
|---|---|---|
| Credentials | none beyond the owner's own login | OAuth client + refresh token (below) |
| Effort per sync | build, verify, drag the folder, compare checksum listing | none after setup |
| Risks | human error (wrong folder, partial upload); no checksum comparison unless done | token leakage; a bug could upload to the wrong place (limited by the scope); needs code review |
| Duplicates | Drive allows two files with the same name; desktop sync may add "(1)" copies | uploader looks files up by parent and name first; idempotent |
| Verification | download + `verify` | per-file `sha256Checksum` after upload |
| Suits | first upload, the 293 MB GDELT cache, rare syncs | daily or weekly prospective partitions |

Recommendation: do the first sync manually. Add automation only once the prospective volume justifies it.

**Minimum credentials for automation**
- **Scope:** only `https://www.googleapis.com/auth/drive.file`. The app then sees only the files and folders it
  created. It cannot read or delete anything else in the account.
- **Not a service account.** Service accounts have no storage quota in a personal My Drive, so uploads would not
  count against the owner's 320 GB and fail. Shared drives need Google Workspace.
- **Setup:**
  - A Google Cloud project (free) with the Drive API enabled.
  - An OAuth client of type Desktop app.
  - The consent screen set to In production, so the refresh token does not expire after 7 days as it does in
    Testing mode. Confirm this when setting it up.
  - Consent given once by the owner on their own machine.
- **Secrets (GitHub):** `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN`, plus the archive
  folder id (not secret).
  - Only a dedicated workflow, triggered manually at first, reads them.
  - It needs no Cloudflare secret, never touches D1, and has `contents: read` only.
- **Revocation:** Google Account → Security → Third-party access → remove the app.

## E. Security and recovery plan

**What is preserved, and how**
- **Pre-registrations** (T-A1–T-A3 `66067b32…`, addendum `c2ebdfcb…`, erratum `5397fe70…`, OI `46c0d52b…`,
  market-move definition and plan): archived as byte copies. Their hashes are pinned in `catalog.json` and
  re-checked by `verify`, by a test, and in every manifest.
- **Prospective boundaries:** unchanged. The archive never computes evaluation metrics. The market-move sealed
  period (from 2026-07-22) is excluded from offline candle queries, and a test checks the code.
- **Source timestamps and provenance:** `timestamp`, `available_at` and `retrieved_at` are kept verbatim (raw
  bytes plus the original Parquet columns). Source URL, request, raw response and the git commit are recorded per
  snapshot.
- **Immutable snapshots:**
  - An archive path is written once. Rewriting it with different bytes is refused (tested).
  - Append-only partitions are stored once, and a rewritten past partition stops the build (tested).
  - Drive's own revision history and 30-day trash are a second net, not the primary one.

**Threats and controls**

| Threat | Control |
|---|---|
| Accidental deletion or overwrite in Drive | Never-delete, never-overwrite uploader. Git research-data branches stay as the second independent copy. Manifests + `SHA256SUMS` exist in both copies. |
| Silent corruption | `SHA256SUMS`, per-file `sha256Checksum` comparison, `verify` after any download |
| Credentials leaking into the archive | `verify` scans every text and gzip file for token patterns (Bearer, Google `ya29.`/`1//`, GitHub, AWS, private keys, `api_key=`). Current archive: 0 findings. |
| Drive token misuse | `drive.file` scope only; dedicated workflow; revocable; no D1 or Cloudflare secrets in that job |
| Exposure of private production rows (predictions, V1 scores) | folder private, link sharing off; no public links; same visibility as the private repo |
| Account loss | Git copy remains. For 3-2-1, optionally an occasional offline download kept by the owner. |
| Quota surprise | `sync-plan` reports upload bytes; stop at a budget the owner sets |

**Recovery:**
1. Download `CryptoPulseV2-Research-Archive/`, or check out the research-data and study branches.
2. Run `archive.py verify --stage <dir>` (about 30 s for the current 4,562 files).
3. Point the study scripts or `offline_queries.py` at it.

No D1 credential is needed at any step. Recovery point: the latest sync. Data newer than that is still in git
(forward store, exports), or still in D1.

## 9. Offline verification (done)

All of the following ran with `env -i` (empty environment, so no Cloudflare or Google variables), local git
objects only, and, for the query step, Python sockets disabled (any connection attempt raises):

- `inventory`, `build`, `verify`: `ok`, 4,561 files checked, 24 Parquet files rebuilding 263,983 source rows
  exactly, 11 pinned hashes.
- `offline_queries.py`: all five analyses above, including the exact reproduction of the 8 production events.
- `sync-plan`: 4,562 uploads, 0 conflicts, 0 deletes. It is computed only; nothing was sent.
- Full `pytest research/`: 924 passed, 3 skipped. The 3 skips are the Parquet tests when pyarrow is absent, as
  in CI.

**Gap found: one existing study is not yet fully offline.** The network block stopped
`research/risk_regime_reconstruction.py` (branch `claude/sweet-meitner-66ntx8`), run without `--live` on the
preserved extracts and the GDELT cache. It had tried to fetch `http://data.gdeltproject.org/gdeltv2/masterfilelist.txt`.
- **Why:** the GDELT step (`gdelt_research_run.py`) downloads that index to check the size and MD5 of each cached
  zip, even when every zip is already cached.
- **Fix** (step F2a): archive the index tail once, alongside the zips, and add an `--offline` switch that reads it
  from the archive.
- **Study code:** not changed here. It belongs to that study's branch and needs your go-ahead.
- **Not affected:** the archive-based analyses above and the production detectors run fully offline today.

## F. Minimal implementation plan with tests

| Step | What | Needs approval | Tests |
|---|---|---|---|
| F1 (done, this branch) | catalog, inventory, Parquet build, verify (checksums, pins, round trip, credential scan), sync plan, DuckDB queries, preserved session extracts | no | 13 in `test_archive.py`: pins and tamper detection; `ast`-only constant hashing; write-once; append-only partitions stored once and rewrite refused; exact round trip (strings, mixed numbers, nested, missing, Unicode); corrupt file reported not crashed; sync never deletes or overwrites; network refused; sealed rows excluded; credential scan |
| F2a | make the risk-regime/GDELT study offline-capable: archive `masterfilelist.txt` (one public GDELT fetch) with the zips; `--offline` reads it from the archive | yes (touches study code on its branch) | the study re-run with sockets disabled reproduces `risk_regime_history.json` V1 rows, outcomes and failure index |
| F2 | first **manual** sync (D steps 1–6) by the owner; record the remote checksum listing in `90_manifests/remote_listing_<date>.json` | owner does it | `sync-plan` against the listing → 0 uploads, 0 conflicts; `verify` on a download |
| F3 | one-time approved D1 dump: estimate rows from known counts first, after the daily reset, read-only, then archive the dump as `d1_snapshot_<date>` | **yes** (D1 quota) | row counts match the dump; preserved extracts are a subset (hash-checked); no write statements in the dump job |
| F4 | `drive_sync.py` (Drive API v3 over HTTPS, `drive.file`, resumable upload, lookup by parent and name, compare `sha256Checksum`, never delete, conflicts reported) plus a `workflow_dispatch`-only workflow | **yes** (new credentials, external integration) | against a fake Drive HTTP server: idempotent re-run, conflict refusal, no delete call ever issued, resumable upload after interruption, quota error stops cleanly, token never logged |
| F5 | schedule F4 weekly for prospective partitions and export snapshots | **yes** | the same tests plus a dry-run diff in the job summary |

Not done here: no Drive connection, no upload or delete, no D1 query, no change to V1 scoring, experiment rules,
Candidate #1, production code, workflows or secrets. Nothing merged or deployed.
