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
