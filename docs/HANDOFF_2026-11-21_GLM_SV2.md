# Handoff — CryptoPulse V2 GLM production influence + Sentiment V2 forecast research (2026-11-21)

For a new Claude Code session continuing the GLM / Sentiment V2 work. Read this first, then `docs/GLM_PHASE_B_CONTRACTS.md`
(revisions 2026-11-19b, 19c, 20) and `docs/SENTIMENT_V2_IMPLEMENTATION.md` §11.

## 1. Repositories and live state

| Item | Value |
|---|---|
| Backend | `quiquandon-oss/PulseWorkerV2` — production `pulseworker-v2.quiquandon.workers.dev`, staging `pulseworker-v2-staging.quiquandon.workers.dev` |
| Frontend | `quiquandon-oss/CryptoPulseV2` (GLM panel + headline influence line, PR #34 `bef750d`) |
| V1 (read-only, never modify) | sentiment-ff75 / PulseWorker / CryptoPulse |
| Production D1 | `sentiment-history` f91ca980-b886-423a-bd6f-f3baea46d181 (shared with V1) |
| Deployed production commit | `b6ba3b2` (PR #186), deployed 2026-11-21 ~10:35 UTC by the owner |
| Plan | Workers Free (owner-confirmed): 10 ms CPU, 5 account cron slots (V1 uses 3), Workers AI 10,000 Neurons/day account-wide; past the allocation calls fail (3036), nothing is billed |

Production GLM flags (`GET /api/glm/status`): rules `glm-qual-0.4`, input `glm-input-3`, prompt `glm-prompt-3`, schema
`glm-signal-2`, model `@cf/zai-org/glm-4.7-flash`; `GLM_SIGNAL_ENABLED=1`, `GLM_INFERENCE_ENABLED=1`, `GLM_TICK_HOOK_ENABLED=1`,
`GLM_INFLUENCE_CAP=0.03`, `GLM_ALLOW_FLIP=0`, `GLM_DAILY_NEURONS=300` (clamped to 1,000 in code), mode `active`.
`SENTIMENT_V2_AI_MODEL=""` and `SV2_GLM_DAILY_NEURONS=0` (per-cycle V2 AI annotation) stay OFF.

## 2. What was built (PRs, all merged)

| PR | Commit | Content |
|---|---|---|
| #179 | 54792ea | glm-input-3: V2 adapter data-quality layer (item quality, per-group input quality, package_status, insufficient → no model call) |
| #180 | b640e3c | Production influence: real inference, ELIGIBLE-only bounded adjustment of BTC 24 h p_up (cap 0.03, no-flip), explanation on snapshot, `/api/glm/evaluation` |
| #185 | fd7d920 | Model-call timeout 25 s → 120 s (first production call 2026-11-21 07:00 timed out) |
| #186 | b6ba3b2 | Sentiment V2 forecast family (RESEARCH STAGE) + forecast-triggered GLM + hard budget gate + persistence-window fix |
| CryptoPulseV2 #34 | bef750d | Headline shows `glm.final_p_up` only in active mode with non-zero adjustment, plus "Why" + findings |

Other sessions merged #181–#184 (observability sampling, comparison APIs, `/api/sv2-forecast`) — not this workstream.

### GLM influence on V2 predictions (live)
- 07:00 UTC daily run (BTC 24 h) + forecast-triggered runs (below). Qualification: UP/DOWN/NEUTRAL/NO_SIGNAL, ≥ 2 independent
  non-AI families, live model forecast, fresh support, contradiction check, AI-only chains never corroborate (Gemini read-only).
- Only ELIGIBLE (same direction on two daily signals 18–30 h apart) moves p_up: weak/moderate/strong = 1/2/3 points, never across 0.5.
- `predictions` keeps the baseline; `glm_adjustments` has baseline/applied/final/cap/reason per BTC 24 h tick; snapshot `glm` object.
- Kill switch (wrangler.toml + deploy): `GLM_INFLUENCE_CAP="0"` (shadow), `GLM_INFERENCE_ENABLED="0"`, `GLM_TICK_HOOK_ENABLED="0"`.

### Sentiment V2 forecast family (RESEARCH STAGE — production score unchanged)
Owner decisions 2026-11-21: one equal-weight `model_forecasts` family + GLM bounded (±5 × strength, ELIGIBLE only); sources =
production `original` + `calibrated` (BTC 24 h), TimesFM 2.5, Chronos-2; reading = measured directional accuracy per model /
asset / horizon / version, Beta(10,10) shrinkage (k=10 PROPOSED), resolved ≤ t, 90-day window; **do not change production scoring
yet** — research component first with comparison. Stored in `sentiment_v2_results.family_json.forecast_research` (new rows only).
Compare: `GET /api/sentiment-v2/forecast-research?limit=48`.

### Forecast-triggered GLM + budget gate
Hourly non-tick cron: each new TimesFM / Chronos-2 BTC 24 h forecast (≤ 36 h old) → one GLM run, cutoff = top of the hour,
idempotent (`glm_runs.reason` = `trigger:<ref>; counterpart …`), shared package recorded as "covered". Budget gate before any
package: today's committed Neurons (actual when reconciled, else reservation; timeouts keep reservation) + one max call (~214)
≤ 300 and no 3036 today, else `BUDGET_EXHAUSTED` recorded. When a package is built, the V2 cycle + collector wait one hour (CPU).
The ledger (`sv2_ai_neurons`) is an internal allocation, NOT proof of account-wide remaining Neurons.

## 3. Evidence so far
- 2026-11-20 07:00: glm-input-3 first run, package complete, model_disabled (pre-activation). Report sent.
- 2026-11-20 09:39 staging probe: real GLM call, 1,191 in / 656 out tokens, 30.43 Neurons.
- 2026-11-21 07:00 first production inference: **timeout at 25 s** → NO_SIGNAL, baseline served (fallback verified); 206.05 Neurons
  reserved, actual unknown. Fixed by #185. Because the timeout keeps its reservation, 2026-11-21 triggers are expected to be
  `BUDGET_EXHAUSTED` (206 + 214 > 300).
- Production `/api/sentiment-v2/forecast-research` live at ~10:35 UTC; rows written before deploy have `research: null`.
- No production influence has applied yet. Earliest ELIGIBLE: needs two answered qualifying daily signals 18–30 h apart
  → earliest after the 2026-11-22 and 2026-11-23 runs.

## 4. Open items / next steps
1. Verify after 11:00 UTC 2026-11-21: first `forecast_research` rows (source states for original/calibrated/TimesFM/Chronos), trigger
   outcomes in `glm_runs_7d`, Neurons. Report to owner.
2. 2026-11-22 07:00: first production call with 120 s timeout — check status, tokens, actual Neurons, signal; then forecast triggers.
3. Collect initial operating measurements (change request §9) for a few days: trigger counts, success/skip, % forecasts analysed,
   Neurons, budget events; assess GLM capacity (Comfortable / Restrictive / Insufficient). At 300/day expect ≈ 2 calls/day.
4. Owner decision pending: promote the research forecast family into the production `sentiment_score` (the change request's final
   acceptance criterion is NOT yet met by design). Also any budget increase (max 1,000) needs explicit approval.
5. Frontend: no Sentiment V2 research panel yet (API only).
6. Carried-over blockers: V1 Workers AI Neuron use and Worker CPU (exceededCpu) unmeasured — need Cloudflare dashboard or an Account
   Analytics Read token; 2026-11-19 12:00 missed tick root cause unconfirmed; hourly d1_usage ledger can drop records.

## 5. Scheduled check-ins (bound to the OLD session, they will not fire into a new one)
- `trig_01FP5jeMnZrvh61prEndRuJw` 2026-11-21 11:08 UTC — verify b6ba3b2 deploy + research rows + triggers.
- `trig_01G1teGo3Wi7qjFcR8N6umaM` 2026-11-22 07:20 UTC — first possible influence check.
A new session should recreate equivalents with `send_later` / `create_trigger` (or delete these with `delete_trigger`).

## 6. Binding rules (unchanged)
- V1 untouched: no code, scoring, schedules or historical-record changes. No new paid services, providers, plan upgrades, Gemini calls.
- D1: bounded queries only (≤ 50k rows each, ≤ 250k direct/day), no full scans / correlated MAX; avoid `ORDER BY ts DESC LIMIT n` on
  unindexed `btc_data`. No production D1 writes for diagnostics. Subagents never run D1/production calls.
- Production deploys go through `deploy.yml`'s approval gate — only the owner approves (Claude cannot). Staging deploy:
  `deploy-sv2-staging.yml` (input `ref`); staging probes: `sv2-staging-run.yml` steps `glm_probe` or `glm_daily,glm_tick_probe`.
- No claim that GLM improves accuracy without ≥ 30 resolved applied ticks (prospective, leakage-safe; `/api/glm/evaluation`).
- Owner authorises autonomous implement → test → PR → merge on green CI; escalate hard-constraint / cost / security issues.
- Commits: trailer `Co-Authored-By` + `Claude-Session`; no model identifiers in commits or PRs.

## 7. Useful read-only checks
```
curl -s https://pulseworker-v2.quiquandon.workers.dev/api/glm/status
curl -s "https://pulseworker-v2.quiquandon.workers.dev/api/glm/adjustments?limit=10"
curl -s https://pulseworker-v2.quiquandon.workers.dev/api/glm/evaluation
curl -s "https://pulseworker-v2.quiquandon.workers.dev/api/sentiment-v2/forecast-research?limit=12"
```
D1 (bounded): `SELECT * FROM glm_runs ORDER BY rowid DESC LIMIT 3`; `SELECT * FROM sv2_ai_neurons WHERE day = '<today>'`;
`SELECT * FROM d1_usage_daily WHERE day = '<today>' AND kind = 'scheduled'`.
Tests: `npx vitest run` (backend, 76 files / 1,694 at b6ba3b2); `node --test tests/*.test.mjs` (frontend, 54).
