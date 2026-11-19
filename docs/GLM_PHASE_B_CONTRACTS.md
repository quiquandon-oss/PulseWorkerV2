# GLM evidence intelligence — Phase B: contracts and feasibility (2026-11-19)

Status: **design, owner instructions of 2026-11-19 applied.** Phase A report:
CryptoPulseV2 `docs/phase-a/PHASE_A_AUDIT_2026-11-19.md` (branch `ccr-a79c2b4e-7mawou`).

Verdict: **FEASIBLE, inference BLOCKED.**
- Everything up to and including shadow plumbing can be built and deployed with inference disabled.
- Inference stays off until (a) V1's real Workers AI Neuron use is read from the dashboard and reserved, and
  (b) the GLM-4.7-Flash licence/terms are confirmed. Neither can be verified from this session.

Owner decisions applied (2026-11-19):
1. **Existing Gemini intelligence is an input, not an exclusion.** V1 `foufi_digest`, its derivatives and historical
   Gemini summaries are read and labelled `gemini_interpretation`. V2 never initiates a Gemini call.
2. **Horizon.** The 07:00 UTC run emits BTC 24 h only. 12 h is `NO_SIGNAL`, reason `no_live_model_forecast`. No
   19:00 run.
3. **Adjusted value.** The adjusted value is the displayed headline: `selection.cycle_p_up` when the chosen variant is
   not the original, else `p_up`. New fields are `baseline_p_up`, `adjustment` and `final_p_up`. Nothing stored or
   used by selection or calibration is overwritten.
4. **Shadow mode.** Shadow mode (applied cap 0) holds until ≥ 30 qualifying resolved signals exist and the gates in
   §11 pass.

---

## 1. Input inventory (production D1 `sentiment-history` plus artifacts, verified 2026-11-19)

Every input item carries one `provenance_class` (§3). "Rows" are the largest row id or the count observed today.

### 1a. V1 — read-only (written by `sentiment-ff75`; V2 never writes these tables)

| Table | Content | Rows / freshness | Class | Bounded read |
|---|---|---|---|---|
| `server_composite_readings` | Hourly server composite; `provenance_json.sources[] {id, score, base_weight, confidence, boost, observed_at}` | PK = `slot_ts`; latest 2026-11-19 06:00 | `v1_aggregate` | `slot_ts` range, ≤ 24 rows |
| `server_source_observations` | Per-source score per slot (`status`, `score`, `observed_at` = publisher time when given, else fetch time; `fetched_at`) | 5,888; latest 06:00 | `v1_aggregate` (per-source score; derived, not raw) | PK `(slot_ts, source_id)`, last slot ≤ 20 rows |
| `history` | Browser composite readings; `sources_json`, `technical_score`, `regime_mag`, `bottom_score`, `gold_regime` | 1,865 (since 2026-09-26) | `v1_aggregate` | `idx_ts`, last 24 h ≤ 12 rows |
| `technical_eval` | Llama-3.1-8B technical read (text plus score), 2×/day | 1,536; latest 06:00 | `v1_ai_interpretation` (non-Gemini) | `idx_teval_ts`, latest 2 |
| `risk_regime_readings` | Risk-regime score/regime/reading_json | PK `ts`; hourly | `v1_aggregate` | PK range, latest 1 |
| `research_source_snapshots` | Raw source values (`source`, `value`) | hourly | `market_data` / `v1_aggregate` | PK range, latest slot |
| `foufi_digest` | **Gemini-generated** (`gemini-3.6-flash`) summary of each Foufi video: `summary_json {market_summary, macro/micro/tradfi {lean,text}, coins{BTC,ETH,LINK,…{score,note}}, dominant_driver, secondary_driver, major_catalyst}`, `overall_lean`; original = YouTube `url`, `published_ts` | 764; latest fetched 2026-11-18 19:02 | `gemini_interpretation` (original: `original_source` = the video) | `rowid DESC LIMIT 10`, keep `published_ts ≤ cutoff`, newest 3 |
| `coin_catalyst_log` | Headline-matched catalysts: `headline_source`, `source_url`, `extracted_reason`, `verdict`, `direction`, `first_public_timestamp`, `investigation_id`, `source_grounded` | 41; latest 2026-10-24 | Row with `investigation_id`: `gemini_interpretation`; else `v1_ai_interpretation`; `source_url`: `original_source` | 41 rows, last 7 days |
| `sentiment_weight_adaptation` | Shadow weight proposals | small | `v1_aggregate` | not used in v1 of the package (no decision content) |

Not available, and not assumed recoverable:
- raw article text behind V1 headline scores;
- publication time for most V1 sources (`observed_at` = fetch time except fng/global);
- any first-available time.

### 1b. Historical Gemini in V2's own tables (frozen; read-only; no new Gemini calls)

| Table | Content | Rows / last | Use |
|---|---|---|---|
| `gemini_daily_analysis` | BTC bias short/medium/long, support/resistance %, macro/geopolitical risk, narrative, **resolved `bias_short_correct`** | 38; last 2026-11-09 | Track record only. Stale beyond the 24 h window, so it is excluded from live reasoning (reason `stale`) |
| `link_gemini_analysis` | LINK equivalent | 64; last 2026-11-15 | LINK is `NO_SIGNAL`; inventory only |
| `gemini_investigations` | Grounded catalyst investigations | 6; last 2026-08-24 | Provenance join for `coin_catalyst_log.investigation_id` |

V2 still contains an admin-gated manual Gemini path (`/run-analysis` and `/run-link-analysis` call
`callGeminiGenerateContent`; cron is off via `GEMINI_DAILY_CRON_ENABLED="0"`). The GLM code never calls it. Removing it is
out of this mandate's scope and is left unchanged (flagged for the owner).

### 1c. Evidence collector (V2, unchanged)

`sv2_evidence` (243 items since 2026-11-15; 7 sources): `evidence_id`, `source_id`, `family`, `canonical_url`, `title`,
`summary`, `published_ts?`, `retrieved_ts`, `title_hash`, `content_hash`, `dup_of`.
- Read through `idx_sv2_evidence_retrieved`, last 48 h, ≤ 60 items, newest first.
- `event_ledger` (`idx_event_ledger_detected`, last 48 h, `asset IN ('BTC','*')`, ≤ 20).
- The research collector `research_event_evidence` is inventoried but not read in v1. It is the GitHub Actions system
  over V1's 5 feeds; G-09 is still unlinked.

### 1d. Market / V2

| Input | Read |
|---|---|
| `btc_data` | `idx_btc_data_ts`, `ts ∈ [cutoff − 48 h, cutoff]`. Derive returns 1/6/24/48 h, realised vol, range |
| Latest V2 BTC predictions (12/24 h) plus selection | `prediction_snapshots` PK (2 rows) |
| V2 24 h track record | `predictions` `idx_predictions_horizon_ts`, last 14 resolved |
| `sentiment_v2_results` latest BTC | UNIQUE `(asset, calc_version, decision_ts)`, latest 1 |

### 1e. Forecast models (existing workflows, unchanged)

| Model | Source | Read |
|---|---|---|
| TimesFM 2.5 | D1 `experiment_4_timesfm` | `idx_..._coin_horizon_ts`: latest per horizon (2 rows) plus last 10 resolved 24 h |
| Chronos-2 | `research-data/exp008-chronos` `exp008_chronos/derived/ledger.json` | 1 fetch to raw.githubusercontent.com (same pattern as EXP-023), ≤ 512 KiB, 8 s timeout, no retry; latest 24 h origin plus last 10 resolved |

Pairing key is `(horizon_hours, input_end_ts)`. A missing pair is recorded as `chronos_missing` or `timesfm_missing`,
never imputed. TimesFM has no uncertainty (point forecast). Chronos gives q10/q90 only. Nothing is invented.

## 2. Normalized input item (contract `glm-input-1`)

```json
{ "ref": "ev:ev_3f…" ,            // stable package-local reference the model must cite
  "provenance_class": "collector_evidence",
  "origin": "pulseworker-v2|sentiment-ff75|github-actions|exp008-artifact",
  "table": "sv2_evidence", "record_id": "ev_3f…",
  "asset_scope": ["BTC"] | ["*"],
  "independence_key": "url:9c1e…",   // §4
  "derived_from": ["src:youtube:abc123"],   // for interpretations: what they summarise
  "times": { "event_ts": null, "published_ts": 1795…, "retrieved_ts": 1795…, "first_available_ts": 1795…,
             "first_available_basis": "retrieved_ts_proxy" , "created_ts": null, "input_cutoff_ts": null,
             "target_ts": null, "resolved_ts": null, "uncertain": ["published_ts"] },
  "content": { /* class-specific, bounded, sanitised: text ≤ 300 chars, numbers as given */ },
  "content_hash": "sha256:…", "freshness_h": 3.2, "stale": false }
```

Classes: `original_source`, `gemini_interpretation`, `v1_ai_interpretation`, `v1_aggregate`, `collector_evidence`,
`market_data`, `v2_prediction`, `v2_sentiment`, `forecast_timesfm`, `forecast_chronos`.

## 3. Provenance rules

- An interpretation (`gemini_interpretation` or `v1_ai_interpretation`) must carry `derived_from` when its original is
  identifiable:
  - Foufi: the video URL, as `src:youtube:<video_id>`, class `original_source`, metadata only (title, url,
    published_ts). No transcript is stored, so none is claimed.
  - Catalyst: `source_url`.
- Gemini and original are both supplied. The model is asked to compare them: the `gemini_vs_original` section of the
  output.
- Aggregates are never decomposed into facts they don't contain. A V1 per-source score is a score, not a headline.

## 4. Independence and deduplication (computed in code, never by the model)

`independence_key` per item:

| Item | Key |
|---|---|
| Collector evidence | `url:<sha256(canonical_url)>`. Items with the same `title_hash` within 7 days, or `dup_of`, collapse to the earliest item's key |
| Foufi Gemini summary **and** its video | `src:youtube:<video_id>` (same key: one confirmation, not two) |
| Catalyst row | `url:<sha256(normalised source_url)>`. It equals the collector key when the URL matches |
| V1 per-source score | `v1src:<source_id>` |
| V1 composite / history / risk regime | `v1agg` (one key for all V1 aggregates: they share inputs) |
| `technical_eval` | `v1tech` (derived from price, so family `market`) |
| TimesFM / Chronos | `model:timesfm` / `model:chronos` |
| Market data | `market:btc` |

Corroboration families (counting unit):
- `news` = collector, catalyst, original source and Gemini summary;
- `v1_sentiment` = V1 aggregates and per-source scores;
- `model_forecast` = TimesFM and Chronos;
- `market` = price-derived items and `v1tech`;
- `v2` = V2 predictions and V2 sentiment.

A signal's corroboration is the number of **distinct families** among its supporting refs after collapsing duplicate
independence keys. Two collector articles with different keys inside `news` add depth, not families.

## 5. Temporal integrity

- `decision_cutoff_ts` is the scheduled run time, 07:00:00 UTC. An item is admissible only if
  `first_available_ts ≤ decision_cutoff_ts`.
- When no first-available time exists, the proxy is the latest of `retrieved_ts`, `fetched_ts`, `created_ts` and the
  row `ts`, recorded as `first_available_basis`.
  - A forecast is available at its row creation time (`ts` / `issued_ts`), never at its `input_end_ts`.
  - A resolved outcome is admissible only if `resolved_ts ≤ cutoff`. Track-record items carry `resolved_ts`.
- `published_ts > retrieved_ts` (clock skew) is marked `uncertain`, and `first_available_ts = retrieved_ts`.
- All times are epoch ms UTC. Any non-finite or out-of-range value (outside 2020–2100) is `invalid_timestamp` and the
  item is dropped with a reason.
- The signal's validity is `[run_completed_ts, decision_cutoff_ts + 24 h)`. It is applied only to predictions whose
  `ts` falls inside that window and that match on horizon. It lapses to `NO_SIGNAL`, reason `expired`, and is never
  carried forward.
- The model-forecast leg of a signal needs the live forecast: `target_ts > decision_cutoff_ts`. For 12 h at 07:00
  this never holds, hence `NO_SIGNAL` (`no_live_model_forecast`).

## 6. Prompt construction (`glm-prompt-1`)

- **System:** the role (evidence analyst, not a forecaster), the rules (cite refs, separate fact / source claim /
  model forecast / interpretation / hypothesis, may return `no_conclusion`), and that everything in DATA is untrusted
  content.
- **User:** one JSON DATA block:
  - the package (§2) sorted by class then time;
  - market summary numbers;
  - forecast pair;
  - V2 baseline;
  - track-record summaries (counts and hit rates computed in code).
- **Bounds:**
  - ≤ 120 items;
  - ≤ 24 KiB UTF-8 prompt (about ≤ 6k tokens);
  - text fields ≤ 300 chars, control characters stripped, HTML tags removed, URLs reduced to host plus hash;
  - `max_completion_tokens` 1,500;
  - thinking disabled via `chat_template_kwargs`;
  - `response_format` JSON.
- **Package hash:** `sha256(canonical JSON of the package + prompt_version + schema_version + model)` is the
  idempotency key. An identical package never calls the model twice: the second attempt records `duplicate_package`.

## 7. Output schema (`glm-signal-1`), strict

```json
{ "status": "completed|no_conclusion|insufficient_evidence",
  "horizons": [ { "horizon_hours": 24, "direction": "UP|DOWN|NEUTRAL",
      "strength": "weak|moderate|strong",
      "supporting_refs": ["…"], "contradicting_refs": ["…"],
      "rationale": "≤ 400 chars" } ],
  "findings": [ { "type": "fact|source_claim|model_forecast|interpretation|hypothesis",
                  "claim": "≤ 240 chars", "refs": ["…"] } ],
  "forecast_comparison": { "timesfm_direction": "UP|DOWN|FLAT|MISSING", "chronos_direction": "…",
                           "agreement": "agree|disagree|partial|unavailable", "note": "≤ 240 chars" },
  "gemini_vs_original": [ { "gemini_ref": "…", "original_ref": "…|null", "relation": "consistent|contradicts|no_original" } ],
  "uncertainty": "low|medium|high", "limitations": ["≤ 160 chars"] }
```

Rejected outright (status `invalid_output`, nothing salvaged): malformed JSON, unknown key or enum, any ref not in the
package, a finding without refs, a horizon other than 24, numbers in free text not present in the package (existing
`sv2UngroundedNumbers`), or over-length fields.

## 8. Signal qualification (`glm-qual-0.1`, deterministic, PROVISIONAL)

The model proposes. Code decides eligibility from verifiable facts only:
1. `direction ∈ {UP, DOWN}`. `NEUTRAL`, `no_conclusion` or `insufficient_evidence` give `NO_SIGNAL`.
2. Corroboration ≥ 2 distinct families (§4) among supporting refs, and ≥ 1 supporting item fresh (≤ 24 h before the
   cutoff).
3. Contradicting families < supporting families, else `NO_SIGNAL` (`contradicted`).
4. If TimesFM and Chronos are both present and **both** oppose the direction, strength drops one step (recorded). If
   it drops below weak, the result is `NO_SIGNAL` (`models_oppose`).
5. Persistence: `ELIGIBLE` only if the previous daily signal (cutoff 24 h ± 6 h earlier) had the same direction and
   was at least `CANDIDATE`. Otherwise `CANDIDATE`. A single day's view never qualifies.

`rule_version` and every reason code are stored. These thresholds are a starting rule, **not validated**. They are
re-examined with the shadow data before any cap > 0.

## 9. Bounded adjustment

- `k(strength)`: weak 1/3, moderate 2/3, strong 1. `dir` is +1 for UP, −1 for DOWN.
- `would_apply = dir × k × GLM_REFERENCE_CAP`. `GLM_REFERENCE_CAP` = 0.05: a measurement scale for shadow
  evaluation, not a validated value.
- `applied = dir × k × min(GLM_INFLUENCE_CAP, 0.10)`. `GLM_INFLUENCE_CAP = "0"` in shadow. 0.10 is a hard ceiling in
  code.
- `final_p_up = clamp(baseline_p_up + applied, 0.02, 0.98)`. If the baseline is already outside that range, it is
  never moved further out.
- Only `ELIGIBLE` signals apply. `CANDIDATE` signals record `would_apply` for evaluation but `applied = 0` regardless
  of the cap.
- Each prediction is adjusted once, always from its own baseline. Nothing accumulates, so there is no drift.
- The adjustment is asset- and horizon-specific (BTC 24 h only in v1).

## 10. Persistence (migration `0031_glm_signal.sql`, additive, reversible)

| Table | Key / indexes | Rows/day |
|---|---|---|
| `glm_runs` | PK `run_id`; UNIQUE `package_hash`; index `(asset, decision_cutoff_ts)`. Columns: versions, model, status, reason, `input_refs_json` (refs, classes, keys, times; **no text**), `package_bytes`, `output_json`, tokens, neurons, timings | ≤ 2 |
| `glm_signals` | PK `signal_id`; index `(asset, horizon_hours, decision_cutoff_ts)`. Direction, strength, eligibility, reasons, families, refs, validity window, rule_version, `would_apply`, ref cap | ≤ 2 |
| `glm_adjustments` | PK `(coin, horizon_hours, prediction_ts)` (idempotent `INSERT OR IGNORE`). `baseline_p_up`, `baseline_source` (`cycle_p_up\|p_up`), `signal_id`, `would_apply`, `applied`, `cap`, `final_p_up`, `mode`, `rule_version` | ≤ 8 (BTC 24 h ticks) |

- Original evidence, forecasts and predictions stay authoritative. GLM tables store references and hashes only.
- Rollback file: `.ai/rollback/0031_glm_signal.down.sql` (drops the 3 tables). Retention: unlimited (≤ 4 KB/day).

## 11. Integration, APIs and Forecasting Lab

**07:00 run.** A step `glmDailySignal` is added to the `0 7 * * *` handler, gated by `GLM_SIGNAL_ENABLED`. D1 reads
go into ≤ 3 `db.batch()` calls. Then one Chronos fetch and at most 1 AI call. Then writes in 1 batch.
- If `SENTIMENT_V2_AI_MODEL` is empty or `SV2_GLM_DAILY_NEURONS = 0`, it still builds and stores the package and run
  with status `model_disabled`. This gives full provenance and lineage evidence without inference.
- Neurons come from the existing `sv2_ai_neurons` ledger (atomic reservation, one ceiling shared with the SV2
  annotation).

**Tick hook.** It sits in `runCoinHorizonChain`, after selection and before `savePredictionSnapshot`, for BTC 24 h
only. It is gated by `GLM_TICK_HOOK_ENABLED` and shipped **off** until per-invocation call counts are measured on
staging. It does 1 indexed read and 1 idempotent insert, and adds a `glm` object to the snapshot payload.

**Routes (public, read-only, edge-cached 5 min, bounded):**
- `GET /api/glm/status`: flags, mode, cap, versions, last run, budget used, blockers.
- `GET /api/glm/signals?asset=BTC&limit≤30`: signals with eligibility and reasons.
- `GET /api/glm/run?id=`: refs and times (no raw text beyond the bounded snippets already public in
  `/api/sentiment-v2/evidence`), output, validation.
- `GET /api/glm/adjustments?limit≤60`: baseline, would-apply, applied and final, joined with realized outcome once
  resolved.

**`/predict`, `/eth-predict`, `/link-predict`:** an additive `glm` object
`{mode, baseline_p_up, baseline_source, adjustment, would_apply_adjustment, final_p_up, signal_id, signal_status, rule_version}`.
`NO_SIGNAL` for ETH, LINK and 12 h. Every existing field is unchanged.

**Forecasting Lab (CryptoPulseV2):** a GLM panel with three visually separate blocks: numerical forecasts (TimesFM,
Chronos), GLM interpretation and signal, and V2 production (baseline → adjustment → final). Plus an evidence
drill-down by ref and class, a Gemini-vs-original view, shadow/degraded states, and a staging API-base override for
tests.

`deploy.yml` path filter must add `glm/**` (new module directory), or GLM changes won't deploy.

## 12. Budgets and cost controls

| Resource | Limit (enforced) | Expected |
|---|---|---|
| D1 rows read per 07:00 run | ≤ 2,000 (each query `LIMIT`ed; no scans) | ~600 |
| D1 rows written per run | ≤ 10 | 3–4 |
| D1 calls per 07:00 run (GLM part) | ≤ 5 (3 read batches, 1 write batch, 1 neuron reservation) | 5 |
| Tick hook (when enabled) | 2 calls, ≤ 2 rows read, 1 written | 8 runs/day |
| Neurons | ≤ 1 call/day at ~90 Neurons (6k in / 1.5k out); ceiling = `SV2_GLM_DAILY_NEURONS` (prod "0"), to be set to (10,000 − measured V1 peak − 30 % margin), capped at 300 | 0 until enabled |
| Subrequests | 1 Chronos fetch, 1 AI call | 2 |
| Retries | 0 for AI (quota or invalid output are never retried); Chronos fetch 0 | — |
| Wall / CPU | AI timeout 25 s; package build O(items) ≤ 120; CPU must be confirmed on the dashboard after the shadow deploy (10 ms Free limit) | — |

Failure statuses (machine-readable, all non-blocking):
`model_disabled`, `skipped_budget`, `quota_exhausted` (`account_limited`), `model_unavailable`, `timeout`,
`invalid_output`, `insufficient_evidence`, `no_conclusion`, `duplicate_package`, `inputs_degraded`, `failed`.
Any failure: the signal is `NO_SIGNAL`, adjustment 0, and V1/V2 continue unaffected.

## 13. Evaluation (Phase F) — separate from PEV-1P

PEV-1P stays frozen: its protocol, windows and counts-only mode are not touched. GLM evaluation is a new,
pre-registered protocol `GLM-PE-1` (written before any signal resolves):
- **Unit:** one `ELIGIBLE` BTC 24 h signal. The predictions inside its window are a correlated cluster, scored per
  signal (mean) and also listed per prediction.
- **Comparison:** baseline headline vs `baseline + would_apply` against the prediction's own realized outcome
  (`realized_up`). The metrics are:
  - Brier score;
  - directional accuracy at 0.5;
  - number of flips;
  - false-trend rate: the signal direction is wrong at the 24 h target;
  - abstention rate;
  - coverage.
- **Gate for cap > 0:** ≥ 30 resolved `ELIGIBLE` signals, all temporal checks passing, complete provenance, a
  reproducible export, resource checks passing, and owner sign-off on the cap value. A non-improving result is
  reported as such.

## 14. Rollback

1. `GLM_INFLUENCE_CAP = "0"`: applied adjustment 0 on the next tick; signals keep being recorded.
2. `GLM_TICK_HOOK_ENABLED = "0"`: no tick reads or writes.
3. `GLM_SIGNAL_ENABLED = "0"`: no 07:00 step.
4. Worker rollback (dashboard or `wrangler rollback`), then a revert PR.
5. Data: run the down-migration only on owner request (tables are additive; nothing else references them).

## 15. PR plan

| PR | Repo | Content | Production effect |
|---|---|---|---|
| B | PulseWorkerV2 | This document | none |
| C | PulseWorkerV2 | `glm/` module: adapters, normalization, independence, temporal filter, package builder, schema validator, qualification, adjustment math; tests | none (not wired) |
| D | PulseWorkerV2 | Migration 0031 plus rollback; 07:00 step wired with `GLM_SIGNAL_ENABLED="1"`, inference still off (`model_disabled`); routes; `/predict` `glm` object; tick hook shipped off; `deploy.yml` paths | Additive tables; ≤ 5 D1 calls and ~600 rows/day; no change to served values |
| E | CryptoPulseV2 | Forecasting Lab GLM panel, evidence drill-down, separation, tests | UI only |
| F | PulseWorkerV2 | `GLM-PE-1` protocol plus evaluation export | none |
| G | both | Enable inference (after D-3/licence), then the tick hook (after staging measurement); cap stays 0 | Neurons ≤ 300/day |

Migration 0031 follows the 0029/0030 practice: it is applied to production D1 (additive `CREATE … IF NOT EXISTS`
only) **before** the Worker that uses it is deployed. It is applied to staging first, and each apply is recorded with
its `rows_written` and timestamp. The worker code tolerates a missing table (status `inputs_degraded`, nothing
written), so a deploy that lands before the migration is harmless.
