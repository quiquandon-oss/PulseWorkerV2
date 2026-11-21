# PulseWorkerV2 operations and resource controls

**Status:** operational contract updated 2026-11-21 from the independent audit and owner mandate OD-18.
This document describes the current constraints and the implementation targets. Where a figure is not yet measured,
it is explicitly marked unverified.

## 1. Execution priority

The non-tick hourly chain must preserve this order:

1. Event detection / source discovery work already scheduled in the chain.
2. V1 archive synchronisation (read V1; do not modify V1).
3. Sentiment V2 baseline decision.
4. Evidence collector.
5. Optional GLM forecast research trigger, last.

**Invariant:** optional GLM and research work must never defer, block, consume the reserved resources for, or
silently reduce baseline V2 or collector work. Remove the previous `glmTriggerRan` deferral. If GLM cannot safely
fit the remaining D1 budget, it does not run and does not write a marker that would prevent a later offer.

A GLM CPU overrun may terminate the optional final step, but must not undo already committed baseline/collector
writes. Runs left in `started` for more than one hour are treated as failed and become eligible for recovery under
the trigger rules.

## 2. D1 per-invocation budget

Cloudflare D1 has a hard per-invocation query ceiling of 50 in the reviewed configuration. This is distinct from
daily D1 row-read/write allowances. The shared invocation counter must be used by every step; do not create
independent counters that can each spend the full allowance.

| Step | Operational rule |
|---|---|
| Event detection | Preserve its existing bounded query plan and accounting |
| V1 archive sync | Preserve bounded batch path and its fallback cap |
| Sentiment V2 | Reserve its known worst-case query requirement before starting; do not start if the reserve cannot be met |
| Collector | Runs after V2; starts only feeds that fit the remaining shared budget |
| GLM | Runs last; requires conservative headroom for scan, gate, package reads and marker writes (audit estimate ≈12 queries) |

Record per-step query counts in existing run records, preferably in already-batched writes. Do not add an unbounded
diagnostic query path. The exact live headroom varies by invocation and must be read from the recorded counters.

## 3. V2 freshness alert

Add an operations alert when there is no V2 decision for **three consecutive non-tick hourly opportunities**.
The alert must distinguish:
- no scheduled invocation;
- invocation failed;
- invocation ran but V2 was deferred;
- V2 ran but wrote no eligible decision;
- V2 decision exists and is current.

The alert is diagnostic only; it must not automatically change configuration or replay historical inputs.
Regression-test the rule, including a replay of the six-hour gap described in the 2026-11-21 audit.

## 4. GLM trigger completeness and ledger semantics

- Scan forecasts issued within the last 36 hours with no trigger/coverage marker, bounded to at most four candidates
  per provider, oldest first.
- Preserve each forecast's provider, asset, horizon, forecast ref, issue time and original cutoff.
- At most one model inference per Worker invocation.
- Every forecast gets an auditable ledger outcome. A byte-identical package may share an inference: the later
  forecast is recorded as `duplicate_package` / `covered`, linked to the covering run. Do not count this as a
  second independent model inference.
- If the invocation cannot fit a forecast, leave it unmarked for the next hourly scan; do not create a false
  completion/coverage record.
- A `skipped_budget` forecast may be offered once after the next 00:00 UTC budget reset if still within the 36-hour
  freshness window. Do not auto-retry timeout, invalid-output or uncertain-consumption outcomes.
- A `started` run older than one hour is stale and may be marked failed/re-eligible.
- Keep package temporal filtering strict: no item later than the forecast's original cutoff.

## 5. GLM and provider budgets

| Control | Value / status |
|---|---|
| Internal GLM daily budget | 300 Neurons/day (conservative internal allocation; unchanged) |
| Hard internal clamp | 1,000 Neurons (unchanged) |
| Cloudflare Workers AI account free allocation | Documented as 10,000 Neurons/day, account-wide; actual account-wide usage is **unmeasured** |
| Reservation | Conservative upper-bound reservation; successful calls reconcile to actual usage; timeout retains reservation (fail-closed) |
| Paid fallback / budget increase | Forbidden |

Do not confuse the internal 300-Neuron budget with the Cloudflare account's free allocation. Do not increase either
limit under this mandate. GLM model availability, 3036 handling and reservation logic remain fail-closed.

## 6. Kill switches and rollback

Preserve the existing switches and their exact deployed names. The current GLM contract documents:
- `GLM_INFERENCE_ENABLED` — stop model inference;
- `GLM_TICK_HOOK_ENABLED` — stop the prediction-tick hook;
- `GLM_SIGNAL_ENABLED` — stop the scheduled signal step;
- `GLM_INFLUENCE_CAP = "0"` — return to shadow/no applied influence.

Before any production change, verify the active names and values in the repository and deployed Worker. Do not invent
new secret names or change a switch outside a reviewed PR. For an incident, prefer the narrowest applicable switch;
rollback the Worker through the approved production deployment path if needed.

## 7. Telemetry and evidence

The following remain unverified until read-only Cloudflare Account Analytics access or dashboard exports are
provided:
- account-wide Workers AI Neuron consumption, including V1;
- Worker CPU p50/p99 and CPU-limit overruns per cron;
- D1 analytics by invocation/step;
- Workers Logs / Observability ingestion and account plan.

Until measured, retain the existing 300-Neuron internal budget, hard clamp and fail-closed reservation. No plan,
subscription, billing or quota change is permitted. Per-step D1 query accounting and V2 staleness alerts are
implementation requirements and do not depend on account-level telemetry access.

## 8. Release and verification

- Backend: required CI suites green; PR review; staging where applicable; merge; existing `production` environment
  approval; verify deployed Worker commit and public read-only route/runtime evidence.
- Frontend: test job must pass before Pages deploy; manual dispatch must be restricted to `main`; run mandatory
  Playwright smokes `stage2-smoke`, `stage7-portfolio-smoke` and `projection-smoke`.
- Keep all public APIs bounded, cached where specified and read-only unless the existing contract explicitly says
  otherwise.
- Record commit SHA, workflow run, test counts, deployment result and post-deploy observations in the handoff.
- Never equate a green workflow check with a verified deployment.

## 9. Hard constraints

V1 immutable; no additional spend; no new paid provider or fallback; TimesFM 2.5 and Chronos-2 remain in their
existing GitHub Actions workflows; no automatic promotion/retraining/trading; PEV-1P and GLM-PE-1 protocols remain
frozen; portfolio remains local; no destructive data changes. Any deviation requires a new owner decision.


## 10. Implementation notes (PR "audit review", 2026-11-21)

How the contract above is implemented. The contract wins where they differ.

### Schedules (Workers Free: 5 account cron slots; V1 uses 3)

| Cron | Hours (UTC) | Chain (in order) |
|---|---|---|
| `0 * * * *` | divisible by 3 | production prediction tick (BTC, ETH, LINK × 12 / 24 h), GLM tick hook, report cache |
| `0 * * * *` | every other hour | event detection → V1 archive sync → **Sentiment V2 cycle** → **evidence collector** → **GLM forecast trigger (last)** |
| `0 7 * * *` | 07:00 | daily GLM run (cutoff 07:00) + records pending forecast triggers as covered by it; Gemini daily (if enabled); recalibration |

TimesFM 2.5 (`exp004-timesfm.yml`, 07:30) and Chronos-2 (`exp008-chronos.yml`) run in GitHub Actions. The Worker reads
TimesFM from D1 and the Chronos ledger from the `research-data` branch, at most once per isolate every 10 min.

## The binding limit: 50 D1 queries per invocation

One counter covers the whole non-tick chain (`sv2CountingDb`; a `db.batch` counts as one query).

| Step | Typical queries | Rule |
|---|---|---|
| Event detection | 19–21 | own limit of 44 |
| V1 archive sync | 2 reads (+1 batch when copying) | always reads |
| V2 cycle | needs 17 fixed (worst case) + 1 per reading (AI annotation off; 5 when on) | defers itself (logged as `DEFERRED (D1 budget)`) when the remainder is short |
| Collector | what is left | stops when the remainder is short |
| GLM trigger | 1 scan + 1 gate + about 12 for one analysis + 1 marker batch | runs on what is left; if one analysis does not fit, it defers **without** a marker and the forecast is offered again next hour |

3 queries are held in reserve. Each log line carries `d1_queries_after` per step.

**Incident 2026-11-21:** from 10:00 to 16:00 the V2 cycle deferred every hour (the trigger then ran before V2), and no
alert fired. Fixed by #188 (cheaper trigger, accurate per-reading reservation) and by the audit-review change (GLM moved
after V2 and the collector). `ops/alert_run.py` now reports more than 4 h without a V2 decision.

## GLM budget (fail-closed)

- **Internal share:** `GLM_DAILY_NEURONS = 300`, clamped to 1,000 in code. It changes only with written owner approval.
- **Account allocation:** Workers AI free allocation is 10,000 Neurons/day, account-wide, reset 00:00 UTC (pricing page,
  checked 2026-11-19). Use by V1 and anything else on the account is **not measured**.
- **Reservation:** one call reserves its worst case (about 206–214 Neurons). A successful call reconciles to its actual use
  (staging probe: 30.4). A timeout keeps the full reservation, so later triggers that day record `BUDGET_EXHAUSTED`.
- **Forecast triggers:** one opportunity per TimesFM / Chronos forecast within 36 h; one analysis per invocation.
  Forecasts in the same package are recorded as covered. A budget skip is offered once more after 00:00 UTC if still fresh.
- **History:** every run and trigger record is listed at `GET /api/glm/runs?limit=50`.

## Kill switches (wrangler.toml + deploy through the approval gate)

| Switch | Effect |
|---|---|
| `GLM_INFLUENCE_CAP = "0"` | GLM back to shadow: no production influence |
| `GLM_INFERENCE_ENABLED = "0"` | no model call (daily or triggered) |
| `GLM_TICK_HOOK_ENABLED = "0"` | no tick read or write |
| `SENTIMENT_V2_AI_MODEL = ""` / `SV2_GLM_DAILY_NEURONS = "0"` | per-cycle V2 annotation off (current production) |
| `KNN_V2_SHADOW_ENABLED = "0"` | corrected k-NN shadow core off (current production; see the shadow PR) |

## Runtime couplings to V1 (V1 itself is immutable)

1. **Shared D1:** V2 shares the D1 database `sentiment-history` with V1.
2. **V1 readings:** V1 writes `server_composite_readings` and `history`, and V2 reads them.
3. **Frontend origin:** the frontend runs on the shared GitHub Pages origin `quiquandon-oss.github.io`.

## Not yet measured (owner telemetry needed)

Account-wide Workers AI Neurons, Worker CPU per cron (`exceededCpu`), D1 analytics, and the Workers Logs volume against the
plan and the announced 1 Dec 2026 pricing change. Source: a read-only Account Analytics token, or dashboard exports.
Until then all budgets stay as they are.

## Known limitations

- **k-NN look-ahead:** the cores still join V1 context two-sided, include the predicted row in scaling, and read
  open-stamped backfill rows (`knn-core-v1`).
  - Live predictions use no future data and their recorded accuracy is honest.
  - Replayed backtests are not valid evidence.
  - The corrected core runs only as a switched-off shadow until CPU is measured.
  - **Implemented (2026-11-21):** `knn-core-v2-asof` / `knn-core-v2-selfcontained`, pipeline `fp-asof-v2`
    (PR #110 redone on current main):
    - `pipeline: 'asof'` option on `runPrediction`, `runEthPrediction` and `runLinkPrediction`. The default `'v1'` is
      unchanged, and `'asof'` forces `persist: false`.
    - Fixes: one-sided context join (`asOfRow`, at most `TOL_MS` old), imputation means and z-score statistics fitted
      without the predicted row, and the read-time backfill restamp `bf-restamp-v1`. Stored rows are not rewritten.
    - **Switch:** `KNN_V2_SHADOW_ENABLED`, shipped `"0"`. When it is `"1"`, each persisted production prediction is
      followed by a shadow run (`runKnnV2Shadow`, try/catch, never throws). The run writes one `INSERT OR IGNORE` into
      `knn_v2_shadow` (migration 0032, owner-applied).
    - **Cost when enabled:** 3 extra unbounded reads plus 1 insert per coin and horizon in the tick, and roughly double
      the k-NN CPU. That is why it stays off until `exceededCpu` per cron is measured.
    - **Evidence route:** read-only `GET /api/knn-v2-shadow?limit<=200`, cached 5 min. It shows served vs shadow p_up
      with outcomes resolved through `served_prediction_id`, and per-coin hits and disagreements.
    - **Not included:** the backfill writer fix (stamp at candle close). Backfill is a one-off operator action, and the
      read-time restamp covers the stored rows.
    - Promotion stays a separate owner decision after prospective evidence.
- **Comparison:** V1 is market-wide: the same reading stands for BTC, ETH and LINK.

### Chain record and V2 freshness alert (implements §2-§3)

- **Storage:** each hourly non-tick invocation stores `{steps: {detection, sync, v2, collector, glm}, q_total}` on its
  existing `event_runs` row (`summary_json.chain`).
  - One bounded `UPDATE`, using the reserved queries. No new table.
  - Each step carries its cumulative D1 query count `q`.
  - V2 also records `deferred`, `written` and `reason`.
- **Route:** read-only `GET /api/sentiment-v2/chain?limit<=48`, cached 5 min.
- **Alert:** `ops/alert_run.py` classifies the last three non-tick slots as `no_invocation`, `invocation_failed`,
  `v2_deferred`, `v2_no_decision` or `current`. It raises a finding when none is current, and falls back to the newest
  decision's age while the route is not deployed.
- **Tests:** they include a replay of the 2026-11-21 gap.
