# All registered experiments: status, evidence and proposed decisions (2026-10-31)

**Scope:**
- every experiment in the live registry (`GET /api/research-lab/registry`, 18 entries, generated 2026-10-31 05:14 UTC);
- GitHub Actions run history;
- the public `data-exports/` CSVs at `origin/main`;
- the risk-regime forward store (`research-data/risk-regime-forward`);
- read-only Worker endpoints.

**Decisions:** this report proposes; the owner decides. No experiment was accepted, rejected, enabled or changed by it.

**Evidence labels:**
- **VERIFIED:** inspected and reproduced in this review.
- **REPORTED:** stated by the registry or an earlier record, not reproduced here.
- **INFERENCE:** an interpretation of verified evidence.
- **BLOCKED:** the evidence needed for a valid conclusion is not available.

**Sealed holdout:** the period 2026-07-22 → 2026-10-09T21:00Z was not read beyond counts.
- Outcome-level analysis below uses only rows made at or after 2026-10-09T21:00Z.
- Endpoints that aggregate over the whole history, sealed window included, were not read for results:
  - `/research/anomaly-gate`;
  - `/research/anomaly-conditioned-audit`;
  - `/api/research-lab/source-effectiveness`;
  - `/api/research-lab/experiment5-results`.

## Executive summary

- **No experiment has met its registered success criterion.** None has an owner decision recorded (`conclusion` is null for all 18).
- **Collection:** healthy for every running experiment. In the last 100 Actions runs, every collection workflow succeeded (VERIFIED). The only failures were in `research-archive-feed`, before the settled-partition fix; it has succeeded since (VERIFIED).
- **Two experiments have outcome evidence I could check without touching the seal:**
  - **EXP-004 TimesFM:** no demonstrated value and no demonstrated harm. It has 19 shared post-seal observations per horizon, and every paired difference vs k-NN and always-up is inside the noise (exact McNemar p ≥ 0.5).
  - **EXP-003 Momentum overlay:** its frozen criterion, "momentum LCA ranks above the production choice", held in 7 of 40 post-seal decisions (4/20 at 12h, 3/20 at 24h). This is the only experiment where a **REJECT** proposal is supportable.
- **Protocol defects block confirmatory conclusions for most others:**
  - **No pre-registered test:** EXP-004 (no split, statistic or alpha); EXP-025 and EXP-028 (no plan for "out-of-sample association").
  - **No required sample:** EXP-002 and EXP-011.
  - **Undefined baseline:** "last-tick persistence" (EXP-004, EXP-008).
- **Evaluation windows overlap the sealed period:** EXP-005, 009, 010, 011 and 002.
- **Registry inconsistency:** EXP-027 says "no production use", but the V1 worker has used Risk Regime Shock (`riskregime`) as a composite source since 2026-10-12, at weight 4 × 0.3 (VERIFIED in `PulseWorker` `RISK_REGIME_SHOCK.md`).

## Inventory and results

| ID | Objective | Registered status | Collection | Sample / required | Evidence | Proposed decision | Next action |
|---|---|---|---|---|---|---|---|
| EXP-001 | Anomaly-conditioned accuracy audit | ON_DEMAND_AUDIT | n/a (on demand) | n/a | Descriptive audit, no hypothesis (REPORTED). Not read here: the endpoint covers the sealed window. | None (audit, not a test) | Run on demand with a post-seal window if needed. |
| EXP-002 | Softened selection bar on anomaly days (shadow) | ACCUMULATING | Active (VERIFIED: data_age 0.9 d) | 86 rows / none defined | BLOCKED. No required sample or test rule, and no post-seal-only data path (no CSV export; the endpoint aggregates the sealed window). | None supportable | Owner: pre-register a sample size and decision rule; add a post-seal export or window parameter. |
| EXP-003 | Momentum overlay (challenger_momentum) ranks above production | ACCUMULATING (suggested READY_FOR_ANALYSIS) | Active (VERIFIED) | 52 rows / 50; 40 post-seal | VERIFIED from `selection_decisions_momentum.csv`, post-seal rows: momentum LCA > production LCA in 4/20 (12h) and 3/20 (24h). Momentum rank median ≈ 4 of 5–7 variants. Production was `original` in 39/40. Limits: LCA rests on 3–15 matched analogs per row; daily rows overlap in their trailing windows; the criterion has no statistical rule (INFERENCE). | **REJECT** supportable on the frozen criterion, with these caveats. | Owner decision. |
| EXP-004 | TimesFM 2.5 BTC 12h/24h direction challenger | ACCUMULATING (suggested READY_FOR_ANALYSIS) | Active: daily, 21/21 post-seal runs succeeded, latest 2026-10-30 14:26 (VERIFIED) | 55 / 30 per horizon (counts VERIFIED; 24/55 and 23/55 hit rates REPORTED, they include sealed outcomes) | VERIFIED post-seal, n = 19 shared per horizon: 12h TimesFM 42.1% vs k-NN 47.4% vs always-up 47.4%; 24h 52.6% vs 63.2% vs 42.1%. All exact McNemar p ≥ 0.5; Wilson 95% intervals ±~20 points. Frozen criterion not applicable: no pre-registered test, persistence baseline undefined, no replication. Full analysis: chat report of 2026-10-31. | None supportable (insufficient evidence, not underperformance). | Owner decision: continue or stop. A confirmatory test needs a plan frozen before outcomes after 2026-10-31. |
| EXP-005 | V1 source/coefficient incremental value | ACCUMULATING (suggested READY_FOR_ANALYSIS) | Active: weekly, last success 2026-10-26 (VERIFIED) | 7 runs / 4 | BLOCKED. Per-pair results live only in D1 `research_analyses`. Run windows overlap the sealed period (REPORTED 2026-10-12). Only 3 of the first 4 runs were independent. Latest single run: 39 significant pairs from 23 sources (REPORTED, descriptive). | None supportable | Owner: rule whether runs whose windows overlap the seal may be used; then check per-pair replication across ≥ 4 independent post-seal runs. |
| EXP-006 | V1 coefficient OOS validation | PROPOSED | Not started | – | No scope or criterion (NOT_AVAILABLE). | None | Choose a candidate hypothesis. |
| EXP-007 | Sentiment vs no-sentiment control | PROPOSED | Not started | – | No protocol; needs V4 signal data linked in. | None | Define the protocol. |
| EXP-008 | Chronos challenger | PROPOSED → being built (research-only) | Not started in production | – | No success criterion yet (NOT_AVAILABLE). | None | See the Chronos-2 work in this cycle; pre-register its evaluation plan before data exists. |
| EXP-009 | Event × source × real-world evidence (schema validation) | ACCUMULATING (suggested READY_FOR_ANALYSIS) | Active: weekly, last success 2026-10-26 (VERIFIED) | 7 runs / 4 | BLOCKED. Its milestone (4 well-formed batches) can only be checked on D1 batches whose windows overlap the seal. No scientific outcome by design. | None (not a hypothesis test) | Owner ruling on seal overlap; then a structural check (duplicates, labels, temporal integrity). |
| EXP-010 | Source dialogue validation (diagnostic) | ACCUMULATING (suggested READY_FOR_ANALYSIS) | Active: weekly, last success 2026-10-26 (VERIFIED) | 6 runs / 4 | BLOCKED, same as EXP-009. The registry's `next_action` text is stale. | None | Same as EXP-009; refresh `next_action`. |
| EXP-011 | Experiment 5, agentic market evidence | ACCUMULATING | Active (VERIFIED: data_age 0.1 d) | 17 decisions / none defined | BLOCKED. The results endpoint aggregates all resolved decisions since 2026-08-27, sealed window included; no required sample. Staging PRs #83/#84 open since early October. | None | Owner: decide PRs #83/#84 and a minimum sample; add a post-seal results view. |
| EXP-012 | Stage 7 human-controlled AI workflow | STAGING_ONLY | Staging only | – | Production migration 0016 not applied; PRs #82/#85 open since early October (VERIFIED). | None | Owner: merge or close #82/#85. |
| EXP-023 | BTC 12h early warning (Goal B) | ACCUMULATING | Active: daily, last success 2026-10-30 (VERIFIED) | 4 / 60 evaluable events (all DOWN; ≥ 25 per direction needed) | INSUFFICIENT_SAMPLE (REPORTED by the daily evaluator). Linear ETA 2027-09-07 (REPORTED). F3/F4 wait for the Binance monthly funding archive. | None | Check in early November that the October Binance funding archive is collected. |
| EXP-024 | Risk-regime forward collection (OI/funding) | COLLECTING | Active: daily and hourly runs succeed (VERIFIED) | OI 22 complete days; HL funding 24/25; OKX 73/74; Binance funding 0 | VERIFIED from the forward-store index. Binance funding is monthly and is collected only after a month closes, so 0 is expected until the October archive (INFERENCE from the collector code). | None (collection) | Same check as EXP-023. |
| EXP-025 | V1 source: Hyperliquid BTC OI (`hloi`) | ACCUMULATING | Active (VERIFIED: data_age 0.0 d) | 375 snapshots / none defined | No pre-registered OOS test. The source is already live in the V1 composite at the lowest tier (REPORTED). | None | Owner: freeze an evaluation plan and date. |
| EXP-026 | Candidate: Binance OI and positioning ratios | COLLECTING | Active (VERIFIED, via EXP-024) | 22 days | Pre-registered OI test `46c0d52b` exists (REPORTED); its horizon has not been reached. | None | Evaluate at the pre-registered horizon. |
| EXP-027 | Candidate signal: Risk Regime Shock | PROPOSED | – | – | **Discrepancy:** the registry says "no production use", but V1 uses `riskregime` in its composite (VERIFIED in the PulseWorker repo). | None | Owner: re-register as a production-source evaluation, or set the V1 weight to 0. |
| EXP-028 | Candidates: Deribit DVOL, DeFiLlama USDT supply | COLLECTING | Active (VERIFIED: data_age 0.0 d) | 770 snapshots / none defined | No pre-registered OOS test. | None | Owner: freeze an evaluation plan and date. |

**Not in the registry, but running:**
- **Event Detection (Goal A):** 24 of 24 recent hourly runs succeeded (VERIFIED). Whether it can be evaluated is not exposed by the API: the protocol needs ≥ 20 watched episodes with ≥ 5 per direction (BLOCKED).
- **V1 shadow weight adaptation:** its verdict could not be read; the V1 host is unreachable from this environment (BLOCKED).

## Decisions requiring the owner

1. **EXP-003:** reject on the frozen criterion (7/40 post-seal), or keep collecting.
2. **EXP-004:** continue or stop collection. A confirmatory test needs a frozen plan (split, statistic, alpha, a persistence definition, replication) applied to outcomes after 2026-10-31.
3. **Seal-overlap policy:** may evaluations of EXP-002, 005, 009, 010 and 011 read stored results whose windows overlap 2026-07-22 → 2026-10-09? Until you rule, they stay BLOCKED.
4. **Missing pre-registration:** freeze evaluation plans for EXP-002, 011, 025 and 028, and define "last-tick persistence" for EXP-004 and EXP-008.
5. **EXP-027 vs V1:** reconcile the registry with V1's production use of `riskregime`.
6. **Stale PRs:** #82–#85 (EXP-011, EXP-012).

## Reproducibility

- **EXP-004:** `GET /research/timesfm-recent?horizon={12,24}&limit=21` (newest rows only; all verified to be after the seal), and `data-exports/btc_predictions.csv` at `a1bc1fe` with rows earlier than 2026-10-09T21:00Z dropped while parsing. Pairing: the k-NN row from the same horizon nearest to `input_end_ts`, within [−3 h, +10 min]; `p_up ≥ 0.5` means UP; ground truth is the TimesFM row's `actual_direction`.
- **EXP-003:** `data-exports/selection_decisions_momentum.csv` at `origin/main` (2026-10-31), rows with `ts ≥ 1791579600000`; compare `momentum_lca > production_chosen_lca` per horizon.
- **Collection health:** the latest 100 completed runs from GitHub Actions `list_workflow_runs`.
- **Forward store:** `research-data/risk-regime-forward:risk_regime_forward/index.json`.
