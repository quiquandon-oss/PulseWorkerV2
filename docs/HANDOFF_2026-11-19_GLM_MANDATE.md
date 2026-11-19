# Handoff 2026-11-19: GLM evidence-intelligence mandate (new Claude Code session)

Purpose: give a fresh Claude Code session everything it needs to execute the owner's mandate
"CryptoPulse V2 — Integrated Evidence Intelligence, GLM production influence and autonomous deployment",
without re-deriving context from the previous (very long) session. Documentation only; nothing here changes code,
configuration or production.

**Read the full mandate first.** The owner attaches the mandate document (`.docx`) to the new session; it is not stored
in this public repository. Where this handoff and the mandate differ, the mandate's owner amendment controls, except
for the owner decisions in §3, which were taken after it.

---

## 1. Starting the new session with the same access

| Access | How the new session gets it |
|---|---|
| Cloud environment | Start the session in the same personal environment (the one used since 2026-11-14). Its network allowlist and two proxy-injected network secrets (see below) come with it. No change needed. |
| GitHub repositories | Select the same 7 repositories: `quiquandon-oss/PulseWorkerV2`, `CryptoPulseV2`, `PulseWorker`, `CryptoPulse`, `CryptoPulseV3`, `CryptoPulseV4`, `cryptopulse-ai-control-tower`. The GitHub connector (MCP `mcp__github__*`) is account-level. |
| Cloudflare (production D1, Workers metadata) | The claude.ai **Cloudflare Developer Platform** connector (MCP `mcp__Cloudflare_Developer_Platform__*`), account-level. It reports itself as deprecated in favour of the Cloudflare API MCP server; either works for D1 queries and Worker metadata. |
| Cloudflare REST (`api.cloudflare.com`, `/client/v4/` only) | Proxy-injected network secret "Cloudflare D1 staging access" on the environment. |
| Staging Worker admin routes | Proxy-injected network secret "PulseWorkerV2 staging admin" for `pulseworker-v2-staging.quiquandon.workers.dev`. |
| Production Worker public routes | `pulseworker-v2.quiquandon.workers.dev` is on the allowlist (no credential). |
| Workers analytics / CPU metrics | **Not available** to sessions (the token cannot read GraphQL analytics). CPU time, `exceededCpu` and Cron Events are read by the owner in the Cloudflare dashboard. |
| Codex review | Quota has been exhausted on every recent PR. Never present another review as Codex. |

Secrets are never copied into chat, logs, commits or PRs. No new permission, token or paid service may be requested.

## 2. Production state at handoff (verify before acting)

- **PulseWorkerV2 `main`**: `e7e10f4` (data-export refresh on top of `267f1a6`, the #168 merge).
- **Live Worker** `pulseworker-v2`: version `80a4ca0b-718b-470b-be21-43c5490025f2`, deploy run #132, commit `267f1a6`
  (2026-11-18 05:29 UTC), verified live (all 12 prediction series stamped with that commit).
- **CryptoPulseV2 `main`**: `fa8e9b5` (#31, paused experiments removed from the frontend).
- **Configuration live** (`wrangler.toml`): `CHALLENGER_ENABLED="1"`; `GEMINI_DAILY_CRON_ENABLED`, `EXP002_CRON_ENABLED`,
  `EXP003_CRON_ENABLED` = `"0"`; `RESEARCH_FUNDING_DISPATCH_ENABLED="0"`; `SENTIMENT_V2_AI_MODEL=""` and
  `SV2_GLM_DAILY_NEURONS="0"` (**GLM off**); `SV2_V1_SERVER_INPUT="1"`; `SV2_COLLECTOR_ENABLED="1"`. Crons:
  `0 * * * *` and `0 7 * * *` (the account's 5 free cron slots are V1 3 + V2 2: **none free**).
- **Migration 0030** (calibration-curve indexes) applied 2026-11-18 00:11 UTC; the prediction tick fell from ~180k to
  at most ~72k rows read.
- **Closed, not merged**: PR #170 (V2 server-reading budget). Consequence: at a 15-min app cadence V2 scores almost no
  V1 server reading, and readings passed by the V2 decision cursor are not recorded. Owner decision; do not reopen
  without being asked.
- **Open, not to merge without the owner**: #162 (TimesFM scheduling doc), #163 (D1 incident report), #164 (EXP-004
  resolve-only dispatch, shipped OFF), CryptoPulseV4 #5.
- **TimesFM** run of 2026-11-17 failed (D1 limit exhausted that day); Chronos-2 recovered 2026-11-18 03:06. Check that
  the 2026-11-18 TimesFM run succeeded.

## 3. Owner decisions on the mandate (2026-11-19, all validated)

1. **What GLM adjusts:** the served V2 prediction `p_up` per coin/horizon (`/predict`, 12 h and 24 h). The adjustment
   lives in its own table; served = baseline + bounded adjustment. `predictions`, calibration curves and selection keep
   using the **baseline only** (no feedback loop). Baseline and adjusted values both observable.
2. **Activation:** deploy the full influence layer in **shadow mode (cap 0)**: signal computed and recorded every
   cycle, "would-apply" adjustment logged, nothing applied, until a defined prospective sample (e.g. 30 qualifying
   signals) exists to calibrate thresholds. Then apply the owner-set maximum cap. Never present uncalibrated thresholds
   as validated.
3. **Scope:** BTC 12 h / 24 h first (TimesFM and Chronos-2 are BTC-only). ETH and LINK emit `NO_SIGNAL` until evidence
   supports them.
4. **Where GLM runs:** the existing **07:00 UTC daily cron** (lighter since #167). The signal carries an explicit
   validity window (e.g. 24 h) and lapses to `NO_SIGNAL` after it. No new schedule (a new one would need a second
   review).
5. **Neurons:** the 10,000 free Workers AI Neurons/day are **shared with V1** (`sentiment-ff75` uses Workers AI).
   Measure V1's real consumption first and reserve it; GLM's ceiling = remainder minus a margin
   (GLM-4.7-flash pricing in `SV2_AI_PRICING`: ~70 Neurons per call at ~6k in / 1k out). If V1's consumption cannot be
   measured with the session's access, GLM stays off and the blocker is reported.
6. **Production gate:** Deploy Worker waits on the GitHub `production` environment; the **owner clicks it**. Never
   approve, bypass or reconfigure that protection. "Autonomous" = Claude merges in-scope PRs, the owner approves the
   gate, Claude verifies the deployment.

## 4. Hard rules learned in production (binding)

- **D1 incident 2026-11-17:** one correlated `MAX` query read 28,685,030 rows (free limit 5M/day account-wide); since
  2026-09-01 exceeding the limit errors **all** D1 queries until 00:00 UTC (V1, V2, V4, research). Therefore:
  `EXPLAIN QUERY PLAN` first; ≤ 50k rows per direct query; ≤ 250k direct rows per day; no correlated `MAX`, full-table
  or fingerprint queries; no polling; no retries of expensive queries; log `rows_read` of every query.
- **Subagents never run D1 queries or production calls.** Only the lead session does, bounded as above.
- **Workers Free CPU: 10 ms per cron invocation**, shared by detection → archive sync → V2 cycle → evidence collector
  in each non-tick hour. Local Node timings already exceed 10 ms for one V2 reading, so they cannot prove CPU safety;
  only dashboard evidence (CPU time, `exceededCpu`) can. Cloudflare tolerates infrequent overruns, terminates
  consistent ones.
- **D1 query budget:** 50 top-level calls per invocation; the cron chain shares one counter (`sv2CountingDb`); V2 gets
  `47 − used`.
- **Rollback of a Worker deploy:** Cloudflare dashboard → Deployments → Rollback (or `wrangler rollback <version>`),
  then a revert PR (otherwise the next deploy reintroduces it). Feature flags are not automatically CPU rollbacks:
  check what the flag-off path does.
- **Frozen evaluation rules** (resolution rule "first `btc_data` price ≥ target_ts"; PEV-1P protocol
  `research/plans/2026-11-02-pev1p-protocol.md`) are never modified; alternative scoring only as separate versioned
  records.
- V1 (`sentiment-ff75`, `PulseWorker`, `CryptoPulse`) is read-only. No Gemini. Zero spend. No plan change.
- Deploy concurrency group `production-deploy-pulseworker-v2`: one running + one pending; a newer pending run cancels
  an older pending one. Several merges in a row produce one deployable run; verify run IDs before cancelling anything.

## 5. Where the data lives (starting points for the audit)

- **Production D1** `sentiment-history` (binding `DB`; shared by V1 and V2; V4 reads it too).
- **V1 source-level data:** `history.sources_json` (per-reading source scores), `research_sentiment_archive`
  (V2's archive copy; migration 0015), `server_composite_readings` (V1 hourly server readings with
  `provenance_json.sources`; written by `sentiment-ff75`).
- **Evidence collector:** `sentiment-v2/collector.js` (registry of 8 sources, 3 freshness groups), tables `sv2_evidence`,
  `sv2_source_state`, `sv2_collection_runs`, `sv2_ai_neurons` (migration 0029).
- **V2:** `predictions`, `eth_predictions`, `link_predictions`, `challenger_predictions`, `selection_decisions`,
  `calibration_curve`, `sentiment_v2_results` (migration 0027), `event_ledger`, `d1_usage_daily`.
- **TimesFM 2.5 (EXP-004):** `exp004-timesfm/run_experiment.py`, workflow `exp004-timesfm.yml` (07:30 schedule, released
  4–10 h late by GitHub), D1 table `experiment_4_timesfm` (migrations 0002/0003).
- **Chronos-2 (EXP-008):** workflow `exp008-chronos.yml` (follows TimesFM via `workflow_run`, 21:40 fallback); JSON on
  branch `research-data/exp008-chronos`; no API.
- **PEV-1P:** `research/plans/2026-11-02-pev1p-protocol.md`, `research/pev1p_window.py`, candle archive branch
  `research-data/pev1p-candles`.
- **Existing GLM code:** `sv2RunAi`, `sv2AiPackage`, `sv2ValidateAiOutput`, `SV2_AI_RESERVE_SQL`, `SV2_AI_PRICING`
  in `worker.js` (annotation only, never sets a score; atomic Neuron reservation, fail-closed). Staging 2026-11-15: the
  model's reasoning consumed the whole output cap → thinking disabled via chat template + JSON response format.
- **Docs:** `docs/SENTIMENT_V2_ARCHITECTURE_MAP.md`, `SENTIMENT_V2_BUILD_SPEC.md`, `SENTIMENT_V2_IMPLEMENTATION.md`.

## 6. First steps for the new session

1. Read the mandate and this handoff; verify §2 against GitHub and Cloudflare (read-only).
2. **Phase A audit, read-only**, delegated to lower-cost subagents (code and repository reading only):
   - Sonnet: routes, D1 tables (from migrations), V1 source data, collector, TimesFM/Chronos artifact schemas,
     Forecasting Lab consumers (CryptoPulseV2).
   - Haiku: simple listings (workflow files, JSON field inventories).
   - Lead session: architecture decisions, signal/influence design, temporal-leakage design, every production D1 query,
     merges and deployment.
3. GLM eligibility check (mandate §3): model id in the current Workers AI catalogue, terms, free allocation, limits,
   and V1's Neuron consumption (decision 5).
4. Report Phase A (architecture/lineage map, cost/query baseline, gap register) before Phase B.
