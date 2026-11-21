# PulseWorkerV2 operations (state 2026-11-21, after the audit review)

What runs when, what it may spend, what stops it, and what is still not measured. Rules and owner decisions live in
`docs/GLM_PHASE_B_CONTRACTS.md`, `docs/SENTIMENT_V2_IMPLEMENTATION.md` and CryptoPulseV2 `docs/governance/OWNER_DECISIONS.md`.

> The owner's "Full Implementation Plan for Claude v2" (20 Nov 2026) is referenced but not stored in either repository.
> The owner should add it, or an approved summary, under CryptoPulseV2 `docs/governance/`.

## Schedules (Workers Free: 5 account cron slots; V1 uses 3)

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
- **Comparison:** V1 is market-wide: the same reading stands for BTC, ETH and LINK.
