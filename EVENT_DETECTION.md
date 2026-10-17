# Event Detection Layer (ruleset `ed-v1`)

**What it is:** detects market, options, macro and corporate-filing **events** that can precede or accompany large crypto moves. It advances project goal 1, *detect significant moves as early as possible*.

**What it is not:**
- a V1 sentiment source, score, weight or prediction input;
- a trading signal;
- a claim of predictive performance.

No evaluation of predictive value exists yet.

**Pipeline:** sources → adapters (`events/adapters/*`) → normalised observations → deterministic rules → deduplication / correlation → ledger (D1) → API (`events/event-api.js`) → V1 Sentiment tab panel (CryptoPulse `index.html`).

## 1. Separation from V1

- **Separate code:** the code lives in `events/`. The only additions to `worker.js` are the import, a route hook and a flag-gated call on the existing 3-hour cron. No new Cron Trigger is added, because the account's 5 are all in use.
- **Separate data:** it reads and writes only its own four tables (migration 0023). It never touches `history`, `sources_json`, predictions, weights, the V1 composite or the sealed holdout. Tests assert this.
- **Display only in the UI:** the panel is a separate card. `computeComposite`, `COMPOSITE_SOURCES_DEFAULTS` and the weights version are unchanged, and tests assert this too.
- **Off by default:** scheduled detection runs only when `EVENT_DETECTION_ENABLED` is `'1'`. Deploying the code changes nothing until then.

## 2. Existing sources: reused, not re-collected

| Source | What it actually provides | Event detector? |
|---|---|---|
| GDELT (branch `claude/gdelt-risk-regime-shock`) | Python research with an offline archive: categories, breadth, acceleration, persistence for past episodes. Not merged, not deployed, no live collector. | No. Listed in `/api/events/health` → `existing_sources`. Not rebuilt or re-downloaded. |
| CoinTelegraph, CoinDesk, The Block, BBC World, Investing.com, SoSoValue | Headline sentiment for V1 composite sources | No |
| Hyperliquid funding (`funding`, `hypefunding`; EXP-23 collectors) | Funding rates, hourly | No |
| `hloi` | Hyperliquid BTC open-interest snapshots, hourly | No (open interest, not liquidations) |
| `riskregime` | Breadth of abnormal cross-asset 6 h moves, scored 0–100 | No (a score, not an event ledger) |
| FRED yields, oil, USD; gold; equity indices; EUR/USD | Daily levels via PulseWorker `/macro-proxy` (browser-supplied FRED key) | No |
| ETF flows (`etfflows`) | Daily net flows | No |
| mempool.space difficulty (`onchain`) | Difficulty-adjustment estimate | No |

## 3. New detectors

### A. Liquidation velocity: `okx_liquidations`, plus `binance_liquidation_sample` (off by default)

**Binance was checked first, as the brief asked.** Its official USDS-M documentation describes `!forceOrder@arr` as a WebSocket snapshot: it pushes **only the latest liquidation per symbol per 1000 ms**, and there is no public REST history. From the build container `fapi.binance.com` returns **HTTP 451**, and some Cloudflare egress regions may get the same.

**Binance adapter (secondary).** It provides a normaliser and a bounded sampler using the Workers WebSocket client. It is enabled only with `EVENT_BINANCE_SAMPLE_MS` (capped at 30 s).
- Its observations are stored as coverage `SAMPLED`.
- They are excluded from every baseline and produce **no events**.

**OKX adapter (primary).** It uses the REST endpoint `GET /api/v5/public/liquidation-orders?instType=SWAP&instFamily=BTC-USDT&state=filled`.
- **Pagination:** newest first, using the `after` cursor. A page that isn't older than the cursor fails closed.
- **Contract size:** read live from `GET /api/v5/public/instruments?instType=SWAP&instId=BTC-USDT-SWAP` (`ctVal`, which must be in `BTC`), never assumed.
- **Notional:** `sz × ctVal × bkPx`, in USD at the bankruptcy price.
- **Liquidated side:** `posSide` when it is long or short. In net mode, a forced SELL liquidates a long and a forced BUY liquidates a short.

**Verification of the OKX endpoint:**
- OKX's changelog (found by web search) confirms `instFamily`/`uly` for this endpoint.
- The OKX docs host is not reachable from the build container, so the full parameter table and the history retention are **unverified here**.
- OKX's API answered HTTP 200 from GitHub's US runners on 2026-10-17 (EXP-23 funding collector).
- The production test below verifies this endpoint live.

**Coverage:** one venue, one instrument family. This is **never** total-market liquidations.
- A bucket is `COMPLETE` only when the fetched history provably covers it and it ended at least 5 minutes earlier.
- Buckets inside the covered window with no liquidations are genuine zeros.
- Partly covered buckets are never stored.

**Rule `ed-liq-burst-v1`** (hourly buckets) fires `LIQUIDATION_BURST` when:

> total ≥ max($1M, median + 4 × 1.4826·MAD, 3 × median), over the prior ≤ 168 complete buckets

- It needs at least 48 complete buckets; until then the status is `INSUFFICIENT_BASELINE` and no event is emitted.
- **Direction:** `DOWN` when liquidated longs are ≥ 65% (forced selling), `UP` when liquidated shorts are ≥ 65% (forced buying), otherwise `MIXED`.
- **Confidence (data quality only):** 0.4–0.7.

**History builds forward:** it starts accumulating from the first run, because OKX's retention is unverified. No baseline is invented.

### B. Options volatility: `deribit_options`

**Endpoints, verified live on 2026-10-17 from the build container:**
- `GET /api/v2/public/get_volatility_index_data?currency=BTC&start_timestamp&end_timestamp&resolution=3600` returns `[candle_start_ms, open, high, low, close]` rows plus `continuation`.
- `GET /api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option` returns `instrument_name`, `open_interest` (in the underlying base currency), `mark_iv`, `underlying_price` and `creation_timestamp`.

**Live result on 2026-10-17:**

| Asset | DVOL candles | Option instruments | Options OI | Put/call OI |
|---|---|---|---|---|
| BTC | 744 hourly | 938 | 390,680 BTC | 0.60 |
| ETH | 744 hourly | 810 | 1,480,346 ETH | 0.51 |

**Rule `ed-dvol-24h-v1`** (closed candles only) compares the 24 h DVOL change with a robust baseline of prior 24 h changes.
- It needs at least 336 prior changes (14 days). The first run fetches 31 days, so it is evaluable immediately.
- `VOL_EXPANSION` fires when Δ ≥ max(median + 3·scale, 3 points). `VOL_COMPRESSION` fires symmetrically.
- **Direction is always `UNKNOWN`**, because implied volatility isn't directional.

**Rule `ed-options-oi-24h-v1`** compares the % change in total options OI with the snapshot closest to 24 h earlier (±3 h), against a baseline of at least 30 prior such changes.
- `OPTIONS_OI_SHIFT` fires when |Δ| ≥ max(10%, |median| + 3·scale).
- The snapshot history builds forward from the first run.
- A book snapshot older than 15 minutes is `STALE`.

**Not claimed:** dealer gamma, options flow or directional positioning.

### C. Macro calendar and surprises: `macro_fred`

**Yields** (FRED `DGS2`, `DGS10`, daily percent):
- With `FRED_API_KEY` set, it uses the FRED API.
- Without a key, it uses FRED's official key-free CSV (`fred.stlouisfed.org/graph/fredgraph.csv`).
- Missing (`.`) values are skipped, never filled.
- A series whose latest value is more than 5 days old is `STALE`.

**Rule `ed-yield-shock-v1`** fires `YIELD_SHOCK` when |daily bp change| ≥ max(10 bp, median|Δ| + 4·scale), over the prior 250 changes (at least 120). Direction is `UNKNOWN`.

**Calendar:** an `EconomicCalendarProvider` interface. The FRED provider (`fred/releases/dates`) needs `FRED_API_KEY`.
- It matches CPI, Employment Situation, PCE, GDP and FOMC by release **name**, so a renamed release fails visibly.
- It gives **dates only**, not release times.
- Each match is a `MACRO_RELEASE_SCHEDULED` event.
- With no key it is `NOT_CONFIGURED`.

**Consensus and surprise: `DATA_UNAVAILABLE`.** No free provider with licensed consensus estimates is integrated. FRED has actual values only, so a surprise is never computed or invented.

**Point-in-time:** each event's `detected_ts` is when this Worker first saw the observation. `source_ts` is the observation date.

### D. SEC EDGAR filings: `sec_edgar`

**Endpoints:**
- `https://data.sec.gov/submissions/CIK##########.json` (recent filings);
- the primary document under `https://www.sec.gov/Archives/edgar/data/<cik>/<accession>/<doc>`.

**SEC policy:**
- It requires a declared User-Agent with contact details; this adapter is `NOT_CONFIGURED` until `EVENT_SEC_USER_AGENT="<name> <email>"` is set.
- The documented limit is ≤ 10 requests/s. This adapter sends ≤ 4/s, sequentially, with at most 12 documents per run.

**Tracked registrants:** MSTR, MARA, RIOT, COIN, TSLA, XYZ (Block), GME, SMLR.
- Each CIK is checked at runtime against the `tickers` the SEC returns.
- A mismatch is skipped as `SKIPPED_IDENTITY_MISMATCH`, so a filing is never attributed to the wrong company.

**Forms:** 8-K, 8-K/A, 10-Q, 10-K, 424B2/3/5/7, S-3, S-3ASR and 6-K accepted in the last 7 days.

**Rule `ed-sec-filing-v1`** (sentence-level and deterministic) can produce:
- `CRYPTO_TREASURY_PURCHASE`
- `CRYPTO_TREASURY_SALE`
- `TREASURY_POLICY_CHANGE`
- `FINANCING_CONVERTIBLE`
- `FINANCING_EQUITY`
- `LIQUIDITY_RISK`

**Transaction status:**
- **`COMPLETED`:** past-tense completion language with no conditional language in the same sentence.
- **`PROPOSED`:** intention or proposal language only.
- **`UNKNOWN`:** a sentence that mixes both, or sentences that disagree with each other.

**Evidence kept:** URL, accession number, company, form, acceptance time, and up to 3 quoted sentences.

**Direction:** `UNKNOWN`. An actual purchase or sale is never inferred from an intention.

**Jurisdiction:** US SEC only. Non-US issuers, such as Metaplanet, are not covered.

## 4. Canonical event record (`event_ledger`)

**Identifiers:**
- `event_id`: `ev_` + 24 hex characters of SHA-256 of the `dedup_key`.
- `dedup_key`: type | asset | source | window or document. It names *what happened*, never when it was noticed, so re-detection maps to the same row (`ON CONFLICT DO NOTHING`, first detection kept).

**Fields:**
- **Scope:** `event_type`, `asset`, `market`, `scope`.
- **Source and times:** `source`, `source_url`, `source_ts` ≤ `detected_ts` (enforced by a CHECK constraint and by the code), `freshness_ms`.
- **Classification:** `direction` (UP / DOWN / MIXED / UNKNOWN), `severity` (LOW / MEDIUM / HIGH / CRITICAL), `confidence` (0–1). These three are independent fields.
- **Rule and values:** `rule_id`, `rule_version`, `observed_value`, `baseline_value`, `threshold_value`, `unit`.
- **Explanation:** `evidence_json`, `explanation`.
- **Lifecycle:** `source_health`, `status` (ACTIVE / EXPIRED; evidence is never deleted), `expires_ts`.
- **Correlation:** `correlation_key`. Stress events on the same asset within the same 6 h window are linked for display only, never merged or double-counted.

**Supporting tables:**
- `event_observations` (first write wins; `observed_ts` is used for point-in-time reads);
- `event_source_health` (one row per detector);
- `event_runs` (audit log).

## 5. API (Pulse Worker V2)

| Route | Purpose |
|---|---|
| `GET /api/events?since=<ms\|ISO>&type=&source=&asset=&status=&limit=≤200` | Recent events with evidence |
| `GET /api/event?id=ev_…` | One event, plus correlated events |
| `GET /api/events/health` | Per detector: status, last success, last observation and age, staleness, coverage, details. Also `enabled`, `last_run` and `existing_sources`. |
| `POST /api/events/run` | Runs every detector once. Requires `Authorization: Bearer <STAGE7_ADMIN_TOKEN>` (the existing admin token check). |

Before migration 0023 is applied, every GET returns 503 with `available:false` and detectors `NEVER_RUN`, never an empty "OK".

**Health statuses:**
- `OK`
- `INSUFFICIENT_BASELINE`
- `STALE`
- `DATA_UNAVAILABLE`
- `NOT_CONFIGURED`
- `BLOCKED_BY_PROVIDER`
- `RATE_LIMITED`
- `PROVIDER_ERROR`
- `DISABLED`
- `NEVER_RUN`

## 6. Reliability

- **Per-request HTTP:** 10 s timeout, and at most 2 retries with exponential backoff for network errors, 429 and 5xx (honouring `Retry-After`, capped at 10 s).
- **Blocks:** 403 and 451 become `BLOCKED_BY_PROVIDER` with no retry.
- **Payloads:** malformed JSON or payloads become `PROVIDER_ERROR`.
- **Isolation:** each adapter has its own 45 s timeout and its own try/catch, so one failing provider never stops the others. Failures are written to health.
- **Logs:** structured lines (`component: event-detection`) go to Workers Logs.

## 7. Configuration (Pulse Worker V2)

| Name | Type | Effect |
|---|---|---|
| `EVENT_DETECTION_ENABLED` | secret or var, `'1'` | Runs detection on the existing `0 */3 * * *` cron. Unset means off. |
| `EVENT_SEC_USER_AGENT` | secret | Required for SEC EDGAR, e.g. `CryptoPulse research you@example.com` |
| `FRED_API_KEY` | secret, optional | FRED API for yields, and enables the release calendar |
| `EVENT_BINANCE_SAMPLE_MS` | var, optional | Enables the Binance sampled stream (≤ 30000) |
| `EVENT_BINANCE_WS_URL` | var, optional | Overrides the stream URL; the default is `https://fstream.binance.com/ws/!forceOrder@arr` |

`wrangler deploy` replaces dashboard-set vars that are missing from `wrangler.toml`, so use `wrangler secret put` for the switches. Secrets persist across deploys.

## 8. Migration and rollback

- **Apply** (production D1 is shared with V1; additive only):
  `npx wrangler d1 execute sentiment-history --remote --file=.ai/migrations/0023_event_detection.sql`
- **Stop collection:** `npx wrangler secret delete EVENT_DETECTION_ENABLED`. The routes stay read-only.
- **Remove the code:** revert the PR on `main`. `deploy.yml` redeploys `worker.js`. Revert the CryptoPulse PR for the panel; GitHub Pages redeploys.
- **Drop the data** (optional, irreversible):
  `DROP TABLE IF EXISTS event_ledger; DROP TABLE IF EXISTS event_observations; DROP TABLE IF EXISTS event_source_health; DROP TABLE IF EXISTS event_runs;`

## 9. Single controlled production test (owner only)

1. Apply migration 0023 (section 8).
2. `npx wrangler secret put EVENT_SEC_USER_AGENT`. Optionally `npx wrangler secret put FRED_API_KEY`. Leave `EVENT_DETECTION_ENABLED` unset.
3. Deploy: merge the Pulse Worker V2 PR into `main` (`deploy.yml` deploys `worker.js`).
4. `curl -s https://pulseworker-v2.quiquandon.workers.dev/api/events/health`. Expect `available:true`, `enabled:false`, every detector `NEVER_RUN`, and Binance `DISABLED`.
5. One run: `curl -s -X POST -H "Authorization: Bearer $STAGE7_ADMIN_TOKEN" https://pulseworker-v2.quiquandon.workers.dev/api/events/run`. Expected per-adapter results:

   | Adapter | Expected status |
   |---|---|
   | `okx_liquidations` | `INSUFFICIENT_BASELINE`, with observations written. `BLOCKED_BY_PROVIDER` or `PROVIDER_ERROR` would mean OKX refused the Cloudflare egress. |
   | `deribit_options` | `INSUFFICIENT_BASELINE` (DVOL `OK`, options OI baseline 0) |
   | `macro_fred` | `OK`, or `INSUFFICIENT_BASELINE` |
   | `sec_edgar` | `OK` |

6. `curl -s '…/api/events/health'` and `curl -s '…/api/events?since=0'`. Check each `last_error`, the coverage, and that any event has evidence and a source URL.
7. V1 unchanged: the Sentiment composite and source table look as before. `/api/learning/*` and the predictions still update on their own schedule.
8. Merge the CryptoPulse PR. On the Sentiment tab, the new card "Event Detection — Detected Events & Detector Health" shows each detector's status and the events.
9. Only after reviewing steps 5–8: `npx wrangler secret put EVENT_DETECTION_ENABLED` with the value `1`, to start 3-hourly collection.

## 10. Known limitations

- Liquidations cover one venue. Binance can only be sampled, and has no history. OKX retention is unverified, so the baseline needs about 2 days of forward collection (48 buckets).
- The options OI rule needs about 4 days of snapshots at the 3 h cadence.
- Macro: consensus and surprise are `DATA_UNAVAILABLE`. The calendar has dates only and needs `FRED_API_KEY`.
- SEC: US registrants only, 8 tracked companies, and a 7-day window. The rules are keyword- and sentence-based, so they can miss unusual phrasing; quoted evidence is always kept for review.
- Cadence: 3-hourly, because there are no free Cron Triggers. A burst is detected up to about 3 h after its hour closes.
