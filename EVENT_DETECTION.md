# Event Detection Layer (ruleset `ed-v1`)

The Event Detection Layer detects market, options, macro and corporate-filing **events**: liquidation bursts, implied-volatility shocks, options open-interest shifts, yield shocks, scheduled releases and SEC filings. Each event is evidence with a timestamp.

None of them is a V1 sentiment source, score, weight, prediction input or trading signal.

**Pipeline:** sources → adapters (`events/adapters/*`) → normalised observations → deterministic rules → deduplication and correlation → D1 ledger → API (`events/event-api.js`) → V1 Sentiment-tab panel (CryptoPulse `index.html`, display only).

## 0. Status at a glance (updated 2026-10-18)

Verification has three levels, and none of them stands in for another: the **build container** (this development environment), a **GitHub-hosted runner** (Ashburn, Cloudflare colo IAD, US; probe run [39038918636](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39038918636), 2026-10-17 14:00 UTC, no secrets), and **Cloudflare** (the Worker's own egress, which only the production run in section 12 can test).

| Detector | Code | Build container | GitHub runner (US) | Cloudflare / production |
|---|---|---|---|---|
| `deribit_options` | done | **passed** (full adapter run, 22 observations stored) | **200**; DVOL 745 candles in one page (`continuation:null`); book summary 200 | not verified |
| `okx_liquidations` | done | refused by the container's network policy | **200**; `ctVal 0.01`, `ctValCcy BTC`; one `BTC-USDT-SWAP` group; 100 records spanned about 18.4 h; the `after` page was strictly older | not verified |
| `macro_fred` | done; **UA fix on PR #94, not deployed** | refused | **200** with curl's or the descriptive User-Agent; **stream reset with no UA or a browser UA** (run 39114185612) | **HTTP 520** in run `edr_c46163d70fa2f878` (deployed code sends no User-Agent); fix pending deploy |
| `sec_edgar` | done | refused | **200** for submissions (MSTR, tickers include MSTR) with a User-Agent **without** contact details; documents weren't probed. Production must still send a contact User-Agent | not verified; needs `EVENT_SEC_USER_AGENT` |
| `binance_liquidation_sample` | done | **451** | **451** (restricted location) | disabled by default |
| GDELT geopolitical shock | research only (branch `claude/gdelt-risk-regime-shock`, 23 tests pass) | refused | not probed | **not integrated** (section 3) |

**Current verification gap:** "Operational" can only be established by the owner's controlled production run in section 12, made from Cloudflare's egress. Until that run passes, no source is called operational.

## 1. Goals: detection, early warning and prediction

The project has two goals: (1) detect significant up and down moves as early as possible, and (2) find signals that **precede** moves. This layer uses three terms precisely.

- **Detection:** the move or market stress is already happening, and the event says so with a measured latency.
- **Early warning:** an observable condition that has historically preceded moves. It is only labelled as such after the evaluation in section 10 shows that detections come before the move's onset.
- **Prediction:** a signal that precedes future moves, with **out-of-sample** value over a baseline. **Nothing in this layer has that evidence.** No event may be described as predictive until a separately approved, frozen evaluation passes.

| Detector | What it is by construction | Contribution to goal 1 | Contribution to goal 2 |
|---|---|---|---|
| `LIQUIDATION_BURST` | **Reactive.** Forced closes happen during or right after a sharp move. | Strong candidate for fast detection; the direction is the side forced out. | Unknown. Bursts can start cascades, which is a hypothesis to test, not a fact. |
| `VOL_EXPANSION` / `VOL_COMPRESSION` | Mixed. Implied volatility can rise before scheduled risk, or with a move. | Detection of stress | Compression before expansion is a classic early-warning hypothesis. Direction is unknown by construction. |
| `OPTIONS_OI_SHIFT` | Positioning build-up or unwind, observed only | Weak | Hypothesis only. No gamma or positioning is inferred. |
| `YIELD_SHOCK` | Reactive to a macro move on the same day | Context | Possible early warning for crypto with a lag; needs testing. |
| `MACRO_RELEASE_SCHEDULED` | **Known in advance**, so genuinely ahead of time | none | It says *when* risk is scheduled, not *what* will happen. Surprise detection is unavailable. |
| SEC filing events | Disclosure, sometimes ahead of market reaction | Detection of corporate flows | Treasury purchases or financing as flow early warnings: a hypothesis to test |

## 2. Architecture and invariants

### Separation from V1
- **Code:** everything lives in `events/`. In `worker.js` the change is **6 lines**: one import and the route hook in `fetch`. The `scheduled()` handler is **unchanged**, so detection never runs inside the prediction cron.
- **Data:** the layer reads and writes only its own five tables (migration 0023). It never touches `history`, `sources_json`, any predictions table, weights, the V1 composite or the sealed holdout. Static tests assert this.
- **Frontend:** the panel is a separate card. `computeComposite`, `computeCompositeBreakdown`, `COMPOSITE_SOURCES_DEFAULTS` and `COMPOSITE_WEIGHTS_VERSION` (`2026-10-12-riskregime`) are unchanged, and tests assert this.
- **Unavailable is never neutral:** missing, stale, unconfigured, blocked or immature data is an explicit status, never a zero or a 50.

### Scheduling
Workers Free limits each invocation to **50 external subrequests** and **50 D1 queries** ([Workers limits](https://developers.cloudflare.com/workers/platform/limits), [D1 limits](https://developers.cloudflare.com/d1/platform/limits)). The account's 5 Cron Triggers are all used (V1 3, this Worker 2).

Running detection inside the 3-hourly prediction tick, as the first draft of PR #94 did, would have shared those limits with the prediction tick. That was a release-blocking defect, and it stays fixed: detection never runs in the same invocation as the tick.

**Why the Worker cron was added (owner decision H3, 2026-10-31):** GitHub started only about 5 of the 24 hourly scheduled runs a day (2026-10-24 to 31), so OKX and Deribit read STALE between runs.

**How a run happens now:** each run is its own invocation.
- **Worker cron (primary):** the former `0 */3 * * *` trigger is now `0 * * * *`, so no trigger is added. In `scheduled()`, hours divisible by 3 (00, 03, ... 21 UTC) run the production tick exactly as before. The other 16 hours call `runAutomaticDetection(env, { trigger: 'cron' })` and nothing else. Expected maximum gap between runs: 2 hours.
- **GitHub schedule (fallback):** `.github/workflows/event-detection-schedule.yml` on `main` (installed 2026-10-18 from the reviewed template `events/scheduler/event-detection-schedule.yml`; non-comment content asserted identical), at `23 * * * *`, through `POST /api/events/run` with `X-Event-Trigger: schedule`.
- **Both automatic triggers** require `EVENT_DETECTION_ENABLED` = `'1'` (otherwise the POST answers 409 `DISABLED` and the cron does nothing). They run at most once per 30 minutes, whichever comes first; the other answers 200 `skipped`.
- **Manual (owner):** a one-time `POST /api/events/run`, allowed whatever `EVENT_DETECTION_ENABLED` is set to. Refused (409) if another run started less than 2 minutes earlier.
- **Stop all automatic runs:** delete the Worker secret `EVENT_DETECTION_ENABLED`.

**Per-run budget:** 40 fetches and 44 D1 queries, enforced by `makeBudget` / `countedDb`.
- An adapter that exceeds it fails alone, with `BUDGET_EXHAUSTED`.
- Queries are reserved so that every health row and the run record are always written.
- Multi-row inserts are chunked under D1's 100-bound-parameter cap, and each series' history is read once per run.
- A realistic full run in the tests (all adapters, SEC configured) stays within the budget. The live run from the build container used 10 fetches and 15 queries.

## 3. Existing sources and GDELT

These existing sources are **sentiment inputs or levels, not event detectors**. None is re-collected here; they are listed in `/api/events/health` → `existing_sources`.

| Source | What it provides | Could feed events by… |
|---|---|---|
| News (CoinTelegraph, CoinDesk, The Block, BBC World, Investing.com, SoSoValue) | Headline sentiment scores in the V1 frontend | Not without a deduplicated headline store. The ledger's `textFingerprint` is ready for syndicated-text deduplication, but no news adapter is built. |
| Hyperliquid funding (`funding`, `hypefunding`, EXP-23 hourly collector) | Funding rates | Reading the EXP-23 data branch (already collected hourly) for a funding-extreme rule. Not built. |
| `hloi` (PulseWorker `/hl-oi`) | Hourly OI snapshots | An OI-drop rule, which pairs naturally with liquidation bursts. Not built. |
| `riskregime` (rrs-v1) | Cross-asset abnormal-move breadth, 0–100 | Its stored readings could become a `CROSS_ASSET_SHOCK` event without new collection. Not built. |
| FRED proxies, gold, equities, EUR/USD, ETF flows, mempool difficulty | Daily levels | Slow; low value as events |

**GDELT is research-only.** The branch `claude/gdelt-risk-regime-shock` is Python, run offline from GDELT 2.0's 15-minute raw export files. It is not merged and not deployed; its 23 tests pass.

The minimal path to integrate it later:
1. Port its pure functions to `events/adapters/gdelt.js`: `classify`, `aggregate_batch`, `window_features`, `score_at`, `persistence_hours` and `acceleration`.
2. Fetch the latest 15-minute export listed in `lastupdate.txt` (about 4 subrequests per hourly run).
3. Unzip with `DecompressionStream('deflate-raw')` after parsing the zip header, and verify size and MD5 as the research code does.
4. Store per-batch features in `event_observations`, and emit `GEO_SHOCK` events under the research thresholds.

This is not done because `data.gdeltproject.org` can't be reached from the build container, so it couldn't be verified. It must not be presented as a live collector.

## 4. Source verification

| | OKX liquidations | Binance stream | Deribit | FRED | SEC EDGAR |
|---|---|---|---|---|---|
| **Provider / endpoint** | `GET /api/v5/public/liquidation-orders?instType=SWAP&instFamily=BTC-USDT&state=filled[&after=]`, plus `GET /api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP` | `fstream.binance.com/ws/!forceOrder@arr`; URL can be overridden with `EVENT_BINANCE_WS_URL` | `public/get_volatility_index_data` (`resolution=3600`); `public/get_book_summary_by_currency?kind=option` | `fredgraph.csv?id=DGS2/DGS10` (no key), or `fred/series/observations` and `fred/releases/dates` (with key) | `data.sec.gov/submissions/CIK##########.json`; `www.sec.gov/Archives/edgar/data/<cik>/<acc>/<doc>` |
| **Auth / config** | none | none; enabled only with `EVENT_BINANCE_SAMPLE_MS` | none | optional `FRED_API_KEY`; needed for the calendar | **`EVENT_SEC_USER_AGENT`** ("name email", SEC fair-access policy) |
| **Fields / timestamps** | `details[]`: `ts` (ms), `side`, `posSide`, `bkPx`, `sz` (contracts); `ctVal` + `ctValCcy` read live | `o.T` (ms), `o.S`, `o.z`/`o.q`, `o.ap`/`o.p`, `o.X` | `[start_ms, o, h, l, c]` + `continuation`; `open_interest` (base currency), `mark_iv`, `creation_timestamp` | daily date, value in percent; `.` = missing | `acceptanceDateTime` (ISO), `form`, `accessionNumber`, `primaryDocument`, `tickers` |
| **Limits / retention / sampling** | public, per-IP limit; codes 50011/50061 treated as rate limits; **history retention unverified**, so the baseline builds forward | **sampled**: latest order per symbol per 1000 ms; no history | public credits (code 10028 = rate limited); DVOL history ≥ 31 days (744 candles fetched live) | FRED CSV is official and key-free | ≤ 10 requests/s documented; this adapter sends ≤ 4/s, 8 submissions plus ≤ 6 new documents per run, and remembers processed accessions |
| **Freshness / baseline** | bucket must have ended ≥ 5 min earlier; 48 hourly buckets needed; source stale after 4 h | n/a (no events) | candle closed; book snapshot ≤ 15 min old; DVOL ≥ 336 prior 24 h changes; OI ≥ 30 prior changes | stale if older than 6 days; ≥ 120 prior changes | acceptance within 7 days; source stale after 2 days |
| **Failure behaviour** | fail-closed on a cursor that doesn't advance or a malformed payload; partly covered buckets never stored | 451/403 → `BLOCKED_BY_PROVIDER`; a disconnect ends the sample early | malformed candle → `PROVIDER_ERROR`; stale snapshot → `STALE` with no observation stored | malformed → `PROVIDER_ERROR`; calendar `NOT_CONFIGURED` without a key | CIK whose tickers don't match → skipped; no User-Agent → `NOT_CONFIGURED` |
| **Live verification** | **Cloudflare passed** (run `edr_c46163d70fa2f878`: 3 requests, 200 records, window covered) | 451 on both; disabled | **Cloudflare passed** (4 requests; DVOL evaluated, OI snapshot stored) | **Cloudflare failed: HTTP 520** (root cause: missing User-Agent, see below) | not configured (no `EVENT_SEC_USER_AGENT`) |

**FRED HTTP 520: root cause (2026-10-18).** `fred.stlouisfed.org` resolves to an Akamai address (23.13.165.165, `server: Apache`, no `cf-ray`), so it is not behind Cloudflare. From a GitHub runner with the adapter's exact URL (run [39114185612](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39114185612)):

| Request | Result |
|---|---|
| curl default User-Agent | 200 (4754 bytes, CSV) |
| **no User-Agent** (what a Worker `fetch` sends) | **HTTP/2 stream reset (`INTERNAL_ERROR`)**, 3 of 3 repeats |
| no User-Agent, HTTP/1.1 | no response within 25 s |
| `Mozilla/5.0 (compatible; Cloudflare-Workers)` or a browser UA | stream reset |
| `CryptoPulse-EventDetection/ed-v1 (+repository URL)` | 200 |

A Worker sees an upstream reset as HTTP 520. The failure is deterministic and caused by request headers, not transient. **Fix (PR #94, not deployed):** every FRED request sends `EVENT_DETECTION_USER_AGENT` (no contact email); failed responses now record `server`, `cf-ray` presence and a short body snippet; one failing series no longer discards the other; a failing release calendar no longer discards the yield series. **Remaining uncertainty:** the runner shares neither Cloudflare's egress IPs nor its TLS fingerprint, so the fix is verified only when a production run with the deployed fix returns 200.

**Economic consensus and surprise: `DATA_UNAVAILABLE`.** No free source with licensed consensus estimates is integrated. A calendar of release dates is not a surprise detector, and no surprise value is computed.

## 5. Rules

| Rule | Event types | Fires when | Direction | Confidence (data quality, not predictive) |
|---|---|---|---|---|
| `ed-liq-burst-v1` | `LIQUIDATION_BURST` | hourly total ≥ max($1M, median + 4·1.4826·MAD, 3 × median) over the prior ≤ 168 complete hours (≥ 48 required) | `DOWN` if liquidated longs ≥ 65%, `UP` if liquidated shorts ≥ 65%, else `MIXED` | 0.4–0.7, capped (single venue) |
| `ed-dvol-24h-v1` | `VOL_EXPANSION` / `VOL_COMPRESSION` | 24 h DVOL change ≥ max(median + 3·scale, 3 points), or the symmetric fall | `UNKNOWN` | 0.5–0.7 |
| `ed-options-oi-24h-v1` | `OPTIONS_OI_SHIFT` | \|24 h % change\| ≥ max(10%, \|median\| + 3·scale) over ≥ 30 prior changes | `UNKNOWN` | 0.4–0.6 |
| `ed-yield-shock-v1` | `YIELD_SHOCK` | \|daily bp change\| ≥ max(10 bp, median\|Δ\| + 4·scale) over ≤ 250 prior changes (≥ 120) | `UNKNOWN` | 0.6–0.8 |
| `ed-macro-calendar-v1` | `MACRO_RELEASE_SCHEDULED` | a CPI, Employment, PCE, GDP or FOMC release dated within 7 days (matched by name) | `UNKNOWN` | 0.9 (date only) |
| `ed-sec-filing-v1` | `CRYPTO_TREASURY_PURCHASE` / `_SALE`, `TREASURY_POLICY_CHANGE`, `FINANCING_CONVERTIBLE` / `_EQUITY`, `LIQUIDITY_RISK` | sentence-level rules; `COMPLETED` only with past-tense completion and no conditional language in the same sentence; intention → `PROPOSED`; mixed → `UNKNOWN` | `UNKNOWN` | 0.3 (`UNKNOWN`), 0.5 (`PROPOSED`), 0.6–0.8 (`COMPLETED`) |

**Severity** measures how far the observation exceeds its threshold (ratio < 1.4 `LOW`, < 2 `MEDIUM`, < 4 `HIGH`, otherwise `CRITICAL`). It is independent of direction and confidence.

**Thresholds** are a-priori defaults. They were **not** tuned on any data, and in particular not on the sealed holdout. Changing them means a new rule version.

## 6. Storage (migration `0023_event_detection.sql`, additive)

All five tables are new. Existing tables, columns and rows are untouched.

| Table | Contents | Write rule |
|---|---|---|
| `event_ledger` | the canonical event: `event_id` (`ev_` + 24 hex of SHA-256 of `dedup_key`), `dedup_key` (unique; names *what happened*), type, asset, market, scope, source, source URL, `source_ts` ≤ `detected_ts` (CHECK), direction, severity, confidence (CHECKs), rule id and version, observed / baseline / threshold / unit, evidence JSON, explanation, freshness, source health, status (ACTIVE / EXPIRED), `expires_ts`, `correlation_key`, `created_ts` | `ON CONFLICT(dedup_key) DO NOTHING`: the first detection is kept. CHECK violations still raise. |
| `event_observations` | (source, series, bucket) → value, unit, coverage (COMPLETE / PARTIAL / SAMPLED), `observed_ts` | first write wins; point-in-time reads use `observed_ts ≤ as-of` |
| `event_source_health` | one row per detector: status, last attempt / success / observation, last error, coverage, details | upsert |
| `event_runs` | one row per run: trigger, summary (per adapter, budget used) | upsert |
| `event_documents` | (source, doc_id) already classified (SEC accession) | insert once; later runs skip the document |

**Expiry** only sets `status = 'EXPIRED'`; evidence is never deleted.

**Correlation:** stress events on the same asset within a 6-hour window share a `correlation_key`. It links them for display only, never merges them or double-counts evidence.

The migration was validated locally on real SQLite, through the tests: it applies, enforces every CHECK, the inserts are idempotent and the chunked inserts work. It has **not** been applied to production.

## 7. API (Pulse Worker V2, `https://pulseworker-v2.quiquandon.workers.dev`)

| Route | Answer |
|---|---|
| `GET /api/events?since=<ms\|ISO>&type=&source=&asset=&status=&limit≤200` | 200 `{ok, available:true, ruleset, since_ts, count, events[], note}`; 400 for a bad `since`; **503 `{available:false}` before migration 0023** |
| `GET /api/event?id=ev_<24hex>` | 200 `{ok, event:{…, related:[…]}}`; 400 for a bad id; 404 if not found; 503 before the migration |
| `GET /api/events/health` | 200 `{ok, available, enabled, ruleset, detectors[] (status, last_success_ts, last_observation_ts, age_ms, stale, coverage, details), last_run, existing_sources, note}`. Before the migration: 503, with detectors `NEVER_RUN`. |
| `POST /api/events/run` | `Authorization: Bearer <EVENT_RUN_TOKEN or STAGE7_ADMIN_TOKEN>` (constant-time compare, tokens of at least 16 characters). Answers: 200 with a summary; 401 for a wrong token; 503 with no token configured; 409 `DISABLED` for a scheduled run while off; 409 if another run started within 2 minutes. |

**Health statuses:**
- `OK`
- `INSUFFICIENT_BASELINE`
- `STALE`
- `DATA_UNAVAILABLE`
- `NOT_CONFIGURED`
- `BLOCKED_BY_PROVIDER`
- `RATE_LIMITED`
- `PROVIDER_ERROR`
- `BUDGET_EXHAUSTED`
- `DISABLED`
- `NEVER_RUN`

**The V1 panel shows these states explicitly.** It distinguishes:
- *no detector has completed a run* from *no events detected*;
- the "no events" message names any detector that is collecting a baseline, stale or failing.

## 8. Configuration (Worker secrets; none is in `wrangler.toml`)

| Name | Effect |
|---|---|
| `EVENT_RUN_TOKEN` | least-privilege token that can only trigger `POST /api/events/run`; at least 16 characters |
| `EVENT_DETECTION_ENABLED` | `'1'` lets *scheduled* runs proceed. Unset means scheduled runs answer 409. |
| `EVENT_SEC_USER_AGENT` | required for SEC EDGAR: `"CryptoPulse research <email>"` |
| `FRED_API_KEY` | optional: FRED API for yields, and enables the release calendar |
| `EVENT_BINANCE_SAMPLE_MS`, `EVENT_BINANCE_WS_URL` | optional Binance sampler (≤ 30000 ms) |

**Use `wrangler secret put` for all of these.** `deploy.yml` runs `wrangler deploy` via `cloudflare/wrangler-action@v3` and uploads only `GEMINI_API_KEY`. Secrets set with `wrangler secret put` persist across deploys; dashboard plain vars would be replaced by the next deploy.

## 9. Cadence and cost

| Detector | Requests per run (typical / max) | Cadence | Notes |
|---|---|---|---|
| OKX liquidations | 2–4 / 9 | **hourly needed**: hourly buckets, and detection latency ≈ bucket end + run delay | 3-hourly would triple worst-case latency |
| Deribit | 4 / 10 (DVOL capped at 4 pages per currency) | hourly (DVOL candles are hourly); OI is fine at 3 h | the OI baseline counts runs, so more frequent runs mature it faster |
| FRED | 2 / 3 | daily is enough; hourly is harmless (about 48 tiny requests a day) | releases are daily data |
| SEC | 8 / 14 | hourly fine (≤ 4 req/s, about 200–330 requests a day) | processed-accession memory prevents re-reading |
| Binance | 0 (off) | — | a sample is ≤ 30 s of a snapshot stream |

The full run costs about 16–24 fetches typically and **36 in the worst case** (every provider at its page cap: 9 + 10 + 3 + 14). A test pins that count, and the 40-fetch budget leaves 10 subrequests of headroom below Cloudflare's 50. Retries count against the budget too, so a run with many retries ends the last adapter with `BUDGET_EXHAUSTED` instead of breaching the limit. Queries are 15–35 per run, against a budget of 44. D1 writes are under about 60 rows per run, well within the Free plan's daily limits.

**GitHub schedules start late and can skip slots** (the reason for the Worker cron above). On 2026-10-17 the new hourly funding workflow skipped its 06:17–09:17 slots and ran at 10:44. Expect latency of up to an hour or more until runs are proven reliable. The alternative is Workers Paid ($5/month, 250 Cron Triggers, 1000 subrequests per invocation), which is the owner's decision.

## 10. Evaluation (leakage-safe) and acceptance criteria

`events/event-eval.js`, `evaluateDetections()`:
- uses **only `detected_ts`**, never `source_ts`;
- scores against move episodes from an independent labeller (the frozen market-moves v1.0.0 labels) within an explicit window, a lead and a lag;
- excludes episodes outside detector coverage from recall.

It reports:
- recall and precision;
- false positives per 30 days;
- early warnings (detection before onset) versus detections after onset;
- latency median and minimum;
- direction agreement;
- per-type counts.

The output carries the caveat that it is not evidence of predictive value.

**Metrics, per detector:**
- detection latency from bucket end to `detected_ts`;
- latency relative to episode onset;
- precision, recall and false positives per 30 days;
- coverage (the share of the window with health `OK`);
- source availability (the share of runs `OK` or `INSUFFICIENT_BASELINE`);
- baseline maturity (time until the minimum count was met).

**Design:**
1. Collect forward only; no backfilled event counts as available earlier than its `detected_ts`.
2. Freeze the rules (`ed-v1`) before evaluation.
3. Evaluate on a window that starts after the freeze.
4. Never use the sealed holdout to choose thresholds.
5. Report every rule. Changing a threshold means a new version and a new forward window.

**Gates:**

| Gate | Requires |
|---|---|
| **Operational** | owner's production run passes (section 12); ≥ 7 days of scheduled runs with health `OK` or `INSUFFICIENT_BASELINE` ≥ 90% of the time; no `BUDGET_EXHAUSTED` |
| **Detection** | baseline mature; latency within 2 h after bucket end for ≥ 90% of runs; false positives per 30 days reported |
| **Early warning (per type)** | in a forward window, a meaningful share of matched detections precede onset, with a pre-declared minimum sample. Until then, nothing is labelled early warning. |
| **Prediction** | a separately approved, frozen out-of-sample experiment beating a baseline. Out of scope here. |

## 11. Monitoring

- `GET /api/events/health`: detector status, `age_ms`, `stale`, `last_error`, and `last_run.summary` (per-adapter status, events new, budget used).
- Workers Logs: structured lines `{"component":"event-detection","kind":"adapter_done"|"adapter_failed"|"run_skipped",…}`.
- The V1 panel's health dot: red on any provider error, block, rate limit or budget exhaustion; amber on stale or baseline collection.

## 12. Production runbook (owner only; each step is a production action)

**Configuration checked against the repository:**
- Worker `pulseworker-v2` (`wrangler.toml`), account `f58e761fbc8e62dc404d8684290af264`.
- D1 binding `DB`, database **`sentiment-history`**, id `f91ca980-b886-423a-bd6f-f3baea46d181`. This database is **shared with V1 `sentiment-ff75`**; the migration is additive.
- Deploy: a push to `main` touching `worker.js` runs `deploy.yml`, which uploads only `GEMINI_API_KEY`. Merging PR #94 touches no path watched by `deploy-research-dispatcher.yml` (`dispatcher/**`).
- The command syntax below was checked against the pinned `wrangler@4.151.1` help output. The commands were **not run** against any Cloudflare account.

Run every command from a checkout of `main` (so `wrangler.toml` resolves `sentiment-history`), logged in as the owner. Never print `EVENT_RUN_TOKEN`: read it into a shell variable, for example `read -rs EVENT_RUN_TOKEN`.

0. **Preconditions (read-only, plus one local backup file):**
   - **Database identity.** `npx wrangler d1 info sentiment-history --json` must show uuid `f91ca980-b886-423a-bd6f-f3baea46d181`. If it shows anything else, or the name resolves to more than one database, **stop**.
   - **No name clash.** `npx wrangler d1 execute sentiment-history --remote --command "SELECT name, type FROM sqlite_master WHERE name LIKE 'event%' OR name LIKE 'idx_event%'"` must return **no rows**. The migration uses `CREATE … IF NOT EXISTS`, so a pre-existing table of the same name would silently keep its old schema.
   - **Restore point.** Record a Time Travel bookmark: `npx wrangler d1 time-travel info sentiment-history --json`. Save the bookmark and the UTC time.
   - **Schema backup.** `npx wrangler d1 export sentiment-history --remote --no-data --output=pre-0023-schema.sql`. Keep this file outside the repository.
   - **V1 baseline.** Save these responses before the change:
     - `curl -s https://sentiment-ff75.quiquandon.workers.dev/risk-regime`
     - `curl -s https://pulseworker-v2.quiquandon.workers.dev/prediction-freshness`
     - a screenshot of the V1 Sentiment composite and source table.
1. **Migration** (additive, idempotent):
   `npx wrangler d1 execute sentiment-history --remote --file=.ai/migrations/0023_event_detection.sql`
   Verify: `npx wrangler d1 execute sentiment-history --remote --command "SELECT name FROM sqlite_master WHERE name LIKE 'event_%'"`. Expect 5 tables.
2. **Secrets:** `npx wrangler secret put EVENT_RUN_TOKEN` (a random value of at least 16 characters) and `npx wrangler secret put EVENT_SEC_USER_AGENT`. Optionally `npx wrangler secret put FRED_API_KEY`. **Do not set** `EVENT_DETECTION_ENABLED`.
3. **Deploy:** mark PR #94 ready and merge it into `main` (`deploy.yml`). Check the Deploy Worker run succeeds.
4. **Read checks:**
   `curl -s https://pulseworker-v2.quiquandon.workers.dev/api/events/health`
   Expect `available:true`, `enabled:false`, detectors `NEVER_RUN`, and Binance `DISABLED`. Also check that `/api/events` returns `count:0`.
5. **Schedule gate:**
   `curl -s -o /dev/null -w '%{http_code}' -X POST -H "Authorization: Bearer $EVENT_RUN_TOKEN" -H 'X-Event-Trigger: schedule' https://pulseworker-v2.quiquandon.workers.dev/api/events/run`
   Expect **409**, with nothing collected.
6. **One controlled run:**
   `curl -s -X POST -H "Authorization: Bearer $EVENT_RUN_TOKEN" https://pulseworker-v2.quiquandon.workers.dev/api/events/run`
   Expected `summary.adapters`:

   | Adapter | Expected |
   |---|---|
   | `deribit_options` | `INSUFFICIENT_BASELINE` (DVOL evaluable; OI baseline 0) |
   | `okx_liquidations` | `INSUFFICIENT_BASELINE` with observations written. `BLOCKED_BY_PROVIDER` or `PROVIDER_ERROR` would be real evidence that Cloudflare's egress is refused. |
   | `macro_fred` | `OK` or `INSUFFICIENT_BASELINE` |
   | `sec_edgar` | `OK` |
   | `binance_liquidation_sample` | `DISABLED` |

   `summary.budget.fetches` ≤ 40 and `queries` ≤ 44.
7. **Inspect:** `/api/events/health` (`last_error`, `coverage`, `details`) and `/api/events?since=0`. Any event must have evidence and a source URL.
8. **V1 unchanged:** compare against the step 0 baseline. The Sentiment composite and source table must be identical (same weights version `2026-10-12-riskregime`). `/risk-regime` must still answer. `/prediction-freshness` must keep advancing after the next prediction slot (hours divisible by 3 on the hourly `0 * * * *` trigger).
9. **Frontend:** merge CryptoPulse PR #5 (GitHub Pages). The Sentiment tab shows "Event Detection — Detected Events & Detector Health".
10. **Only with separate approval, to start recurring runs:**
    - `npx wrangler secret put EVENT_DETECTION_ENABLED` with the value `1`;
    - add the repository secret `EVENT_RUN_TOKEN` (same value);
    - install `events/scheduler/event-detection-schedule.yml` as `.github/workflows/event-detection-schedule.yml` on `main`.

**Rollback:**

| Undo | Command | Limitation |
|---|---|---|
| Stop recurring runs | `npx wrangler secret delete EVENT_DETECTION_ENABLED`, and/or delete the workflow file | Manual runs still work with the token: delete `EVENT_RUN_TOKEN` to disable all runs |
| Remove the code | revert the merge commit on `main` (`deploy.yml` redeploys `worker.js`); revert CryptoPulse PR #5 | the routes return 404, and the panel shows "not deployed" |
| Restore the database (last resort) | `npx wrangler d1 time-travel restore sentiment-history --bookmark=<step 0 bookmark>` | **Rolls back the whole shared database, V1 tables included**, losing every V1 write since the bookmark. Use it only if a V1 table was damaged. Event-only problems are undone with the `DROP TABLE` row below. Wrangler accepts timestamps within 30 days; the retention on this account's plan is unverified, so check it in the dashboard. |
| Remove the data (irreversible) | `npx wrangler d1 execute sentiment-history --remote --command "DROP TABLE IF EXISTS event_ledger; DROP TABLE IF EXISTS event_observations; DROP TABLE IF EXISTS event_source_health; DROP TABLE IF EXISTS event_runs; DROP TABLE IF EXISTS event_documents;"` | deletes all collected history and events; baselines restart from zero. No V1 table is affected. |

## 13. Production history (controlled test)

| UTC | Action | Run / identifier | Result |
|---|---|---|---|
| 2026-10-17 14:16 | Recovery check (Time Travel) | [39040059793](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39040059793) | bookmarks resolvable back 29 days |
| 2026-10-17 14:31 | Migration 0023 | [39041121783](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39041121783) | 8 objects, existing schema hash unchanged; bookmark before `00002d35-0000004e-00005107-3ac9467d…` |
| 2026-10-18 08:44 | Deploy `c19e73c` + `EVENT_RUN_TOKEN` | [39112591036](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39112591036) | version `2f229ddb-4d5b-43a0-bd29-0ef3e46a31e2`; 401/401/409 gates; V1 endpoints 200; rollback target `4f3e5069-3dee-4ed1-abc1-ec7e81ae3daf` |
| 2026-10-18 09:02 | One manual run | [39113761264](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39113761264), `edr_c46163d70fa2f878` | 10/40 fetches, 17/44 queries; 40 observations, 5 health rows, 0 events; OKX and Deribit fetched, FRED 520, SEC not configured, Binance disabled |

**Reconciliation plan (needs owner approval):**
1. Deploy the reviewed PR head through the stage workflow (gates: CI green, code identical to that head, database identity, migration objects, secret names).
2. Run one verification run.
3. Mark PR #94 ready and merge it with a **merge commit**, so the deployed commit stays reachable. `deploy.yml` then redeploys identical code, with `GIT_COMMIT_SHA` set to the merge commit.
4. Return the stage branch to `idle` and retire it.

Until step 3, do not merge PRs #83, #82 or #35, or any push touching `worker.js`, `wrangler.toml`, the package files or `deploy.yml`. Each would redeploy `main` without the event routes. Rollback means the previous Worker version (`wrangler rollback`). Restoring the database or dropping event tables is **not** a routine rollback, because a restore would erase legitimate V1 writes.

Deployment path: the stage branch `claude/epic-planck-uyapsw-event-stage1` (step file `idle`), not `deploy.yml`. **Production runs `c19e73c`; `main` is `f4c6d99`.** Until PR #94 is merged with a merge commit, a push to `main` touching `worker.js`, `wrangler.toml`, the package files or `deploy.yml` (open PRs #83, #82, #35) would redeploy without the event routes. That failure is safe (routes return 404, tables stay) but silent. New prediction rows carry `git_commit_sha = c19e73c`; research that groups by commit must treat `8b12c9a` and `c19e73c` as the same prediction code (the `worker.js` diff is the 6-line event hook).

## 14. Controlled collection plan (approved 2026-10-18)

**Approved recurring mechanism (the only one):** `.github/workflows/event-detection-schedule.yml` on `main`, hourly at `:23`, installed from the reviewed template. It needs the Worker secret `EVENT_DETECTION_ENABLED=1` (set through the stage workflow after the schedule is verified) and the repository secret `EVENT_RUN_TOKEN` (already present). Limits, overlap guard, retries, provider isolation and scrubbing are unchanged; Binance stays disabled, SEC stays `NOT_CONFIGURED`, no FRED key.

**Cost per run (measured in `edr_c46163d70fa2f878`, FRED fixed and SEC unconfigured assumed):**

| Detector | Requests | D1 queries | New rows (typical, hourly) |
|---|---|---|---|
| OKX | 2–3 (instruments + 1–2 pages; 9 at worst) | 5 | ~3 (one new hour × 3 series; overlaps are ignored) |
| Deribit | 4 | 8 | ~8 (1–2 new DVOL hours × 2 + 6 OI values) |
| FRED | 2 (CSV) | 3 | ~0–2 (daily data) |
| SEC | 0 until configured; then ≤ 14 | — | — |
| Run record, health, expiry | — | ~4 | 1 run + 5 health upserts |
| **Total** | **~9 typical; hard maximum 40 (see below)** | **~20 (budget 44)** | **~15** |

**Hard request limit (derived from the code, `44e08a4` and later).** Every logical request may make 3 attempts (2 retries), so the uncapped maximum is 108 attempts: OKX 9×3, Deribit 10×3, FRED 3×3, SEC 14×3. Two mechanisms bound it:
- `makeHttp` checks the run budget (40) **before every attempt**, so the run total can never exceed 40.
- Each detector draws from its own share (`FETCH_ALLOCATION`: OKX 10, Binance 1, Deribit 10, FRED 5, SEC 14; the shares sum to 40). Each share covers that detector's own logical maximum, and retries come out of the same share, so a failing provider cannot starve the detectors after it.

Binance's WebSocket upgrade is charged to the budget too. A detector that reaches its share ends `BUDGET_EXHAUSTED` and writes **no** observations, so partial coverage is never stored as `COMPLETE`. The D1 query budget is 44, enforced per statement with a reserve for the health rows and the run record. Tests cover all of this: all providers failing on every attempt, FRED failing on both series, exhaustion part-way through OKX paging, and the Binance charge. Error text is scrubbed of `api_key=`, `token=` and similar values before it reaches health rows or the public API.

Hourly: ~216 requests, ~480 queries and ~360 row writes a day; storage about 40 KB a day (~15 MB a year), against a 12 MB database today. **Maturity** with hourly runs: OKX after 48 complete hours of coverage (gaps up to 8 h are re-read, longer gaps are lost); Deribit options OI after ~54 h (30 prior changes, each needing a reference ±3 h of 24 h earlier); DVOL and FRED yields evaluate on provider history from the first run.

**Stop conditions (for the activation decision):** stop recurring runs if any run returns `BUDGET_EXHAUSTED`, if one detector is `PROVIDER_ERROR`/`BLOCKED` for 24 consecutive hours, if `/prediction-freshness` leaves `FRESH`, or if D1 row writes exceed 2,000 a day. Failed providers are represented by their health row (`last_error`, `requests`) and the run summary; they write no observations, so a retry or restart cannot create a misleading history (inserts are `ON CONFLICT DO NOTHING`, only complete buckets are stored).

## 15. Goals: evidence status (2026-10-18)

- **Goal A forward-evaluation protocol (declared 2026-10-18, before any forward Event Detection data exists).**
  - **Ground truth:** market-moves v1.0.0, unchanged. BTC, ETH and LINK; |R24| > 3% (strict) on Hyperliquid hourly closes; the definition's own episode, onset and resolution rules; baselines B0–B3.
  - **Window:** starts at the first hour when recurring collection is approved and running. Nothing before it, and nothing from the sealed period, is read.
  - **Data and cadence:** hourly Event Detection runs. Hyperliquid hourly candles for the labels come from the market-moves collector, collected forward.
  - **Coverage:** an episode counts as *watched* by a detector only if that detector's health was `OK` at its onset and its baseline was mature: OKX ≥ 48 complete prior buckets, DVOL ≥ 336 changes, options OI ≥ 30 changes, FRED ≥ 120. Unwatched episodes are reported, never counted as misses.
  - **Timing:** only `detected_ts` is used. A detection counts for an episode if it falls in [onset − 12 h, resolution + 6 h]. Before onset it is an *early warning*; after onset it is a *detection*. Latency is `detected_ts − onset`.
  - **Metrics, per detector and per event type:** recall over watched episodes; precision; false positives per 30 days; missed and duplicate alerts; lead time (median and minimum); coverage share; results by asset and direction where n ≥ 5.
  - **Comparison:** each detector against B0 (the 24 h return crossing itself, which by construction has zero lead) and against B1–B3 as frozen.
  - **Minimum sample:** ≥ 20 watched episodes, with ≥ 5 per direction. Below that the result is `NOT EVALUABLE`.
  - **Single evaluation:** run once at the checkpoint with the `ed-v1` rules frozen. Changing a rule means a new version and a new window.
- **Goal A (early detection): `NOT YET VALIDATED` / `NOT EVALUABLE`.** One production run, 0 events, baselines immature. Ground truth is predeclared (market-moves v1.0.0: |R24| > 3% strict, hourly Hyperliquid closes, BTC/ETH/LINK, episode/onset rules, baselines B0–B3), but its stored snapshot ends 2026-10-09 21:09Z, inside or before the sealed period, so it gives no usable post-seal episodes. The EXP-23 forward evaluator counts **2 post-seal episodes (DOWN 2, UP 0)** as of 2026-10-17. Evaluation needs ≥ 30 days of continuous forward collection with mature baselines and a predeclared minimum (proposed: ≥ 20 episodes, ≥ 5 per direction) before `evaluateDetections()` is run once with frozen `ed-v1` rules, lead 12 h and lag 6 h, against B0 (24 h return crossing) as the simple baseline.
- **Goal B (prediction): `NOT YET VALIDATED`.** The designated protocol is EXP-23 (spec v0.4.0 sealed, v0.5.0 source variants): chronological, sealed holdout, burn-in, checkpoint of 60 events (25 per direction). Latest run (2026-10-17, [39045316218](https://github.com/quiquandon-oss/PulseWorkerV2/actions/runs/39045316218)): `INSUFFICIENT_SAMPLE`, 2/60 events, F3/F4 not evaluable. No predictive claim is made; Event Detection is not an input to any prediction.

## 16. Known limitations and acceptance gaps

- No source is verified as operational in production yet. Deribit was verified from the build container. Deribit, OKX, FRED (CSV) and SEC (submissions) answered from a GitHub runner. Nothing has been verified from Cloudflare.
- OKX pagination: `after` is exclusive; the boundary millisecond is now re-read and de-duplicated by multiplicity (fix on PR #94, not deployed). A page of > 100 liquidations within one millisecond fails closed. Records first written win: if OKX publishes liquidations later than 5 minutes after the hour, the stored hour undercounts (unverified).
- OKX history retention is unverified. A GitHub runner confirmed the endpoint, the payload fields and the `after` paging; Cloudflare's egress is untested. A US runner reaching OKX does not prove that the Worker's colo will. Fail-closed runtime checks are in place.
- Liquidations cover one venue. Binance can only be sampled, and from some regions it is blocked.
- Baselines build forward: about 48 h for liquidations, and about 30 runs (around 1.5 days hourly) for options OI. DVOL and yields are evaluable immediately.
- Consensus and surprise are unavailable. The calendar is dates only and needs `FRED_API_KEY`.
- SEC covers US registrants only, 8 tracked companies, and keyword/sentence rules that can miss unusual phrasing. Quoted evidence is always kept.
- GDELT is research-only.
- The Worker cron covers 16 hours a day; the 8 prediction hours rely on the GitHub fallback, so a gap of up to 2 hours remains.
- No predictive or early-warning value has been evaluated.
