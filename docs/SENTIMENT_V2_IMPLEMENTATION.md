# Sentiment V2 — implementation, assumptions and traceability

Authoritative product specification: `docs/SENTIMENT_V2_BUILD_SPEC.md` (owner's `CryptoPulse_Sentiment_V2_Build_Spec.md`,
copied verbatim; sha256 prefix `4b24b2a9f3bd3c85`). Current-system map: `docs/SENTIMENT_V2_ARCHITECTURE_MAP.md`.
This document records how the specification is implemented, every technical or methodological detail the
specification left open (marked **H** = hypothesis or assumption), measurements, and remaining limitations.

## 1. Architecture

```
original PulseWorker (V1, unchanged) ──writes──> history, btc_data ─┐
PulseWorkerV2 archive job (unchanged) ──writes──> research_sentiment_archive (V1 score + 21 source readings)
Event Detection (unchanged) ─────────────writes──> event_ledger
                                                                    │ read only
worker.js runSentimentV2Cycle (hourly non-tick hours, after Event Detection)
   ├─ pending decisions = V1 archive observations newer than the last V2 result (first run: newest one only)
   ├─ point-in-time inputs: archive rows <= t, btc_data prices <= t, events detected <= t
   ├─ sv2Compute: families, staleness, redundancy, events, regime, analogues, score, confidence
   ├─ sv2RunAi: optional Workers AI annotation (disabled until verified), schema-validated, never sets numbers
   └─ INSERT OR IGNORE sentiment_v2_results (unique asset, calc_version, decision_ts)
GET /api/sentiment-v2/comparison?period=24h|7d|30d|90d|all
   └─ V1 + BTC (aligned at or before each V1 timestamp) + V2 (same timestamp) ──> CryptoPulseV2 "Sentiment V2" tab
```

Why these choices:
- **Decision timestamps = V1 observation timestamps.** V1, V2 and BTC are compared at exactly the same instants
  (spec §6), with no interpolation.
- **Hourly non-tick hours, after Event Detection.** The account is on Workers Free with all five Cron Triggers in use
  (V1 uses three). V2 never shares an invocation with the prediction tick, and detection's writes finish before V2
  starts.
- **No historical V2.** The first cycle evaluates only the newest V1 observation; later cycles catch up at most three
  observations newer than the last result (spec §1).

## 2. Method (spec §4), with hypotheses

| Component | Live implementation | Status |
|---|---|---|
| Source intelligence (§4.1) | Each of V1's archived sources is **fresh**, **stale** (value unchanged for ≥ 10 consecutive observations, the EXP-005 rule in `research/source_intelligence.py`) or **missing** (seen in the 31-day window, absent now). Stale and missing sources are excluded, never treated as neutral. Unmapped sources are listed. | implemented |
| Information families (§4.2) | `sv2Config().families` maps every source V1 actually archives to the nine spec families. `market_structure_breadth` has only `global`, which V1 lists but no longer observes, so that family is reported as uncovered. **H2**: mapping by source meaning (`foufi` digest → sentiment/news; `macrogeo` → macro; `strc` → corporate treasury). | implemented |
| Redundancy (§4.1) | Pearson r over the last 120 observations (n ≥ 30). Pairs with r ≥ 0.9 **within a family** are merged into one group, so the family is not counted twice; pairs across families are reported as overlap, not merged. **H3**: only positive correlation is treated as duplication. | implemented |
| Family score | Mean of independent groups' fresh readings (V1 readings are 0–100, 50 neutral, **H4**). | implemented |
| Composite | Equal weight per family with at least one fresh source (**H1**: no family is proven more predictive; equal weights until incremental-value testing says otherwise). | implemented |
| Event intelligence (§4.3) | `event_ledger` rows detected ≤ t within 72 h, not expired at t, BTC or market-wide. Grouped by `correlation_key`/`dedup_key` (dedup; persistence = group size). Severity weight × novelty (half-life 24 h from first detection) × confidence. Family from event type/rule; overlap with a fresh family halves its effect (**H6**). Role: price-derived events, or a ≥ 2 % same-direction BTC move in the 6 h before detection → `consequence`; ≥ 1 % → `coincident`; else `cause_candidate`. Intensity = 1 − Π(1 − w). Adjustment = ±10 × intensity × direction balance (**H7**). | implemented |
| Event reaction / priced-in (§4.3, §4.5) | Realised 24 h BTC return after earlier detections of the same type, only where observable at t; stored as `reaction_prior` per event, not used in the score. | implemented (informational) |
| Macro and cross-market (§4.4) | Macro and equity sources form their own families; direction via the family score, persistence via staleness and redundancy, regime via §4.5. | implemented through families |
| Regime (§4.5) | Transparent rules on point-in-time BTC prices: `stress_high_volatility` (7-day vol > 1.5 × 30-day vol, or |24 h return| ≥ 5 %), `bullish` (30-day ≥ +5 % and 7-day ≥ 0), `bearish` (mirror), else `sideways`; `insufficient_history` under 7 days. **H5**: an HMM is a research challenger, not live. | implemented |
| Historical analogues / predictive (§4.5, §5) | `predictive_json`: share of the 20 nearest earlier family profiles whose BTC rose over 24 h, outcomes observable at t only; `validation: candidate_unvalidated`; `insufficient_history` under 10 analogues. Kept apart from sentiment. | implemented (candidate) |
| Forecasting research (§4.6) | TimesFM (EXP-004) and Chronos-2 (EXP-008) already run as GitHub Actions research challengers; their results stay research inputs. TiRex-2, LightGBM/XGBoost and an HMM are listed as challengers in §6; none runs in the Worker (no paid compute, Workers CPU limits). PEV-1P is untouched and protected (PR #130). | research only |
| AI reasoning (§4.7) | `sv2RunAi`: Workers AI binding only, compact package with identifiers, strict JSON schema (`summary`, `cited_ids`, `contradictions`, `event_relevance`, `uncertainty`), cited ids must exist in the package, any extra field (e.g. a score) rejected. It annotates; it never sets a number. | implemented, **disabled** (§4) |

Confidence = 0.6 × evidence coverage + 0.4 × family agreement (1 − sd/25) (**H8**). Sentiment, confidence, evidence
coverage, event intensity and the predictive signal are separate columns (spec §5).

## 3. Persistence and API

- Migration `.ai/migrations/0027_sentiment_v2_results.sql`: one additive table plus index; nothing existing touched.
- Idempotency: `UNIQUE (asset, calc_version, decision_ts)` (also the index every V2 query uses; SQLite query plans checked) and `INSERT OR IGNORE`.
- V1 immutability: V2 code contains SQL writes only to `sentiment_v2_results` (tested statically and with a recording
  fake database).
- `GET /api/sentiment-v2/comparison?period=24h|7d|30d|90d|all` (default 7d): `points` [{ts, v1, v2, btc, btc_ts,
  v2_status}] at most 500 (downsampling keeps actual observations), `series_status` (first/last timestamps from indexed single-row lookups, `stale` after
  6 h, V2 `available`, `insufficient_history`), `v2_latest`, `v2_error`. Archive and V2 queries read at most 2,000 rows each and the btc_data price query at most 6,000 (newest first; `truncated` and `btc_truncated` say when a cap was reached), so one uncached request reads at most about 10,000 rows plus two single-row lookups. Cache API plus in-isolate cache, 5 min per period,
  `Cache-Control: public, max-age=300`. V1 values are passed through exactly as stored.

## 4. Zero-cost controls

- **Workers AI not verified, so AI is off.** No Cloudflare credentials are available in the build environment (no
  wrangler login, no API token, repository secrets not readable), so the account's model catalogue, eligibility,
  licence and free quota could not be verified (spec §3). The adapter ships with `SENTIMENT_V2_AI_MODEL = ""` and
  `SENTIMENT_V2_AI_DAILY_CALLS = "0"`; every result records `ai_status: disabled`.
- To enable after verification: set a verified model id and a daily ceiling below the free allowance minus a reserve
  for production. The guard counts the day's calls in D1 and stops at the ceiling (`quota_guard`); errors and invalid
  output degrade the cycle without retrying or switching provider.
- V2 code has no `fetch`, no API keys, no other provider (tested).

## 5. Measured resource use (real V1 archive, 210 observations; real btc_data, 473 rows in 31 days)

| Item | Per cycle | Per day (16 cycles) |
|---|---|---|
| D1 rows read | ≈ 212 archive + 473 btc_data + events (≤ 2000) ≈ 700 | ≈ 11k (≈ 0.2 % of the free 5M) |
| D1 rows written | 1–3 | ≈ 13 (one per V1 observation) |
| CPU | ≈ 5 ms per decision (Node), ≤ 3 decisions | — |
| API (per uncached request) | archive rows in period + btc_data rows in period | cached 5 min |

Production measurement after deploy: `d1_usage_daily` already records every scheduled run.

btc_data is V1-owned and this repository cannot see whether it has an index on `ts`. With the optional migration
`0028_btc_data_ts_index_optional.sql` (owner decision; additive, no data change) the capped newest-first price reads
scan only the rows they return (SQLite plan: `SEARCH btc_data USING INDEX idx_btc_data_ts`). Without it each
btc_data read scans the whole table: about 3,000 rows on 2026-11-14, growing about 16 a day, so about 48k rows a day
for 16 cycles plus uncached API requests (about 1% of the free 5M).

## 6. Requirement traceability

| Spec | Requirement | Where | Test (`tests/sentiment-v2.test.js`) |
|---|---|---|---|
| §1 | No invented history | `runSentimentV2Cycle` (first run newest only) | first cycle / catch-up |
| §3 | Zero paid fallback, quota guard | `sv2RunAi`, wrangler vars | AI describe block |
| §4.1–4.2 | Families, freshness, redundancy | `sv2SourceStatus`, `sv2Redundancy`, `sv2Families` | families, stale/missing |
| §4.3 | Event timestamps, dedup, relevance, overlap, role | `sv2Events`, `sv2EventReactions` | events describe block |
| §4.5 | Regime, analogues | `sv2Regime`, `sv2Analogues` | point-in-time block |
| §4.7 | AI schema, cited ids, no numbers | `sv2ValidateAiOutput` | AI block |
| §5 | All outputs, distinct fields | `sv2Compute`, migration 0027 | schema block |
| §6 | Separate tables, aligned timestamps, V1 untouched | migration, `sv2AlignSeries` | immutability, alignment |
| §8 | API periods, bounded, cached, idempotent | `getSentimentV2Comparison*` | API block, idempotency |
| §7 | Frontend tab and graph | CryptoPulseV2 | CryptoPulseV2 tests |

## 7. Operations

- Enable/disable: `SENTIMENT_V2_ENABLED` ("1"/"0"). Rollback: set "0" and redeploy; the table can stay.
- Apply migration 0027 to production D1 **before** deploying the Worker (owner action; Claude Code's guard refuses
  remote D1 commands). Until then the cycle fails on its first query and logs it; nothing else is affected.
- Logs: `sentiment v2 (cron): {...}` per non-tick hour.

## 8. Limitations and open hypotheses

- **AI disabled** until Workers AI availability, licence and free quota are verified for this account.
- **H1–H8** are unvalidated design choices; incremental-value testing needs V2 history and must be prospective.
- `market_structure_breadth` has no live source; `riskregime` appears in history but not in current observations and
  is not mapped.
- V1 source readings carry no individual timestamps; freshness is inferred from unchanged values (EXP-005 rule).
- Reconstructed V2 backtests were not produced; if made, they must be labelled and point-in-time (spec §1).

## 9. Independent Codex reviews (sequential, adversarial)

| # | Scope | Verdict | Findings → resolution |
|---|---|---|---|
| B1 | backend | needs-attention | high: public `all` route read D1 without a row bound → newest-first `LIMIT` on every query, `truncated`, Cache API |
| B2 | backend | needs-attention | high: history-wide `COUNT/MIN/MAX` per request → indexed single-row first/last lookups |
| B3 | backend | needs-attention | medium: index lacked `calc_version` → UNIQUE (asset, calc_version, decision_ts); query plans verified |
| B4 | backend | needs-attention | medium ×2: deploy before migration; non-atomic AI ceiling → fail-closed table check; atomic per-day slot reservation (verified on SQLite) |
| B5 | backend | needs-attention | medium ×2: request-wide read bound; stale BTC prices → documented request bound; prices carry their own time, 3 h tolerance |
| B6 | backend | needs-attention | medium: BTC fallback could miss older points → separate 6,000-row price cap, `btc_truncated` stated |
| B7 | backend | needs-attention | medium ×2: oldest-first capped cycle inputs → newest-first, cap reached ⇒ DEGRADED `inputs_truncated` |
| B8 | backend | needs-attention | medium: no index on V1's `btc_data.ts` → optional migration 0028 and documented cost |
| B9 | backend | **approve** | none |
| F1 | frontend | needs-attention | medium ×2: no refresh on tab re-entry; stale hidden by short history → 5-min refresh, independent notices |
| F2 | frontend | **approve** | none |

## 10. Scope lock 2026-11-14: independent evidence, sv2-1.1.0, optional GLM

Owner decisions (2026-11-14):
- Collected evidence changes the score under a **new calc version `sv2-1.1.0`**. `sv2-1.0.0` results stay stored and
  shown; they are no longer computed.
- The collector runs **inside the existing hourly non-tick cron**, with a small per-run budget and no new Cron Trigger.
- Reviews: `@codex review` on each PR.

### 10.1 Free capacity (verified 2026-11-14, Cloudflare docs and repository settings)

| Resource | Free allowance | V2 limit (enforced in code) |
|---|---|---|
| Workers Free CPU | 10 ms per invocation | ≤ 3 feeds and ≤ 768 KiB per run (≈ 6 ms per MB parsed, measured locally: worst case ≈ 4-5 ms), index-based parser, hashing only for new items |
| Workers Free subrequests | 50 external per invocation (shared with Event Detection) | ≤ 8 per run, redirects and retries included |
| Cron Triggers | 5 per account, all in use | none added |
| D1 | 5 M rows read and 100 k rows written per day; 5 GB | Indexed, `LIMIT`-bounded reads. Writes only for new items (one `IN` lookup per feed). Estimated ≤ 2 k rows written per day. |
| Workers AI | 10,000 Neurons/day per account, **shared with V1** (sentiment-ff75 uses `llama-3.1-8b-instruct-fast`). On Workers Free, exhaustion returns error 3036 and is never billed. | `SV2_GLM_DAILY_NEURONS` (production `0` until staging verifies; intended ≤ 1,000). Atomic reservation of an upper-bound estimate. Stop on 3036 for the rest of the day. |
| GitHub Actions | Public repositories: standard runners free | TimesFM 2.5 / Chronos-2 workflows unchanged; measured and pinned in a separate PR |

GLM-4.7-flash (`@cf/zai-org/glm-4.7-flash`):
- Listed as available on Workers Free (Cloudflare changelog 2026-07-28).
- 5,500 Neurons per million input tokens and 36,400 Neurons per million output tokens (pricing page, 2026-11-12).
- **Account availability is verified on staging only**, with `POST /api/sentiment-v2/run {"steps":["glm_probe"]}`.

### 10.2 Collector (`sentiment-v2/collector.js`)

- **Registry:** `SV2_SOURCE_REGISTRY`, approved public RSS/Atom/official URLs.
  - Feeds V1 already reads (Cointelegraph, CoinDesk, The Block, BBC, Investing.com) are excluded, so V2's evidence is independent of V1's readings.
  - SEC sources need the owner-configured `EVENT_SEC_USER_AGENT`; without it they report `NOT_CONFIGURED`.
  - `SV2_SOURCES_DISABLED` switches individual sources off.
- **Safety:**
  - https only, and only registry hosts;
  - no IP literals or internal names, no credentials, default port only;
  - `redirect: manual`, with every hop re-validated (at most 2 redirects);
  - 8 s timeout, 256 KiB per response and 768 KiB per run;
  - one retry on network error, timeout, 429 or 5xx, and none on other 4xx;
  - content-type allowlist; conditional requests (ETag / Last-Modified).
- **D1 query budget shared across the cron chain:** the non-tick chain (detection → V1 archive copy → V2 → collector) counts its top-level D1 calls; the collector runs last and only uses `50 − 3 − used` (it starts no feed it cannot finish). Collector cost: at most 2 + 3 × feeds (one lookup, one duplicate-check batch and one write batch per feed; a `db.batch()` counts as one query against D1's per-invocation limit).
- **Storage (migration 0029):**
  - `sv2_evidence`: `evidence_id` = sha256 of the canonical URL; `title_hash` for cross-source duplicates (kept with `dup_of`, excluded from scoring); `content_hash`; publication and retrieval timestamps; provenance.
  - `sv2_source_state`: reliability (ok/fail counts), consecutive failures, exponential backoff up to 16×, ETag.
  - `sv2_collection_runs`: per-run budget use and errors.

### 10.3 Scoring (`sentiment-v2/evidence-score.js`, `sv2Compute`)

- **Headline scorer:** V1's own lexicon and arithmetic, copied verbatim and pinned by hash (108 terms, sha256 `989054b3…`). V1 and V2 therefore read a headline the same way.
- **Reading per evidence source at decision time t:**
  - only items retrieved ≤ t, not duplicates;
  - each item's time is its publication time, or its retrieval time when the publisher gives none or a future time;
  - items must fall within the source's `freshness_h`.
- **States:**
  - `fresh`: at least 2 lexicon hits. The reading is `round((tanh(mean score per hit / 1.5) + 1) × 50)` (V1 batch arithmetic).
  - `insufficient`: items present but fewer than 2 hits.
  - `stale`: only older items.
  - `missing`: nothing retrieved by t.
  - Only `fresh` enters the family score; nothing is imputed as 50.
- **In the families:**
  - Evidence sources join their registry family as additional sources.
  - `missing` sources are not "known", so they don't lower coverage. `stale` and `insufficient` sources are known but not fresh.
  - Redundancy (Pearson) still runs on V1 sources only, because evidence sources have no per-observation history.
  - Historical analogues still use V1 sources only, for comparability.
- **Point-in-time:** a V1 reading at 19:45 uses only evidence retrieved by 19:45. Evidence collected at 20:00 counts only for later readings.

### 10.4 GLM (`sv2RunAi`)

- **Off unless all of these hold:** a priced model, `SV2_GLM_DAILY_NEURONS > 0`, the AI binding, and no 3036 response seen today.
- **Budget:**
  - Before each call, one conditional `INSERT … SELECT` reserves an upper bound: input tokens ≤ the prompt's UTF-8 bytes + 256 (a byte-level tokenizer cannot exceed one token per byte), plus `max_completion_tokens` 1,200, at the published rates.
  - It succeeds only if the day's committed total (max of reserved and actual per call) plus the reservation stays within the budget.
  - Actual Neurons, from reported token usage, are recorded on the same row.
- **Output validation:**
  - JSON only, with the existing schema and cited-ID checks;
  - every number in free text must appear as a whole value in the evidence package (not as a fragment of a longer number);
  - any extra field (e.g. a score) is rejected.
- **The score is never changed.** An AI failure only marks the result DEGRADED.

### 10.5 Not in this scope

- No search API, crawling, paid provider or paid fallback.
- No TiRex, LightGBM, XGBoost or HMM.
- V1 is unchanged.
