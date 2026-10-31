# Research portfolio: status, evaluation routes and recommended actions (2026-10-31)

**Supersedes:** `2026-10-31-all-experiments.md` (PR #99, closed unmerged). It corrects its EXP-003 finding.

**Evidence labels:**
- **VERIFIED:** inspected and reproduced in this review.
- **REPORTED:** documented elsewhere, not reproduced.
- **INFERENCE:** an interpretation of verified evidence.
- **BLOCKED:** the evidence for a valid conclusion is not available.

**Actions:** CONTINUE, EVALUATE_NOW, PAUSE, CLOSE_OR_REJECT_PROPOSED, DEFER, BLOCKED. Actions are recommendations; no
owner decision is recorded by this review.

**Sealed period:** 2026-07-22 → 2026-10-09T21:00Z was not read beyond counts.

**Machine-readable copy:** every row below is also served live by `/api/research-lab/registry` as `decision.proposal`
(worker.js `PROPOSED_DECISION_RECORDS`), with evaluation records in `standalone_evaluations`.

## Correction: EXP-003

The earlier report proposed REJECT because momentum's LCA exceeded `production_chosen_lca` in only 7 of 40 post-seal
decisions. That column stores the LCA of the **best-scoring** variant (`decision.winner.lca` in `selectBestVariant`),
not of the variant production **chose**. Production usually falls back to `original` without clearing the gate.
(VERIFIED in `worker.js`.)

On the registered reading, each row's own `scores_json`, which holds every variant's LCA on the same set of similar days:
- **After deduplication** (6 rows logged twice), momentum ranked above the chosen variant in **15 of 34** unique
  post-seal decisions (12h 10/17, 24h 5/17), with 8 ties. (VERIFIED, `selection_decisions_momentum.csv@a1bc1fe`.)
- **The criterion has no statistical rule,** so no conclusion follows. The REJECT proposal is withdrawn.
- **A prospective sign test is now frozen** (see below).

## Portfolio

| ID | Objective | Collection (VERIFIED) | Evaluation | Production influence | Action | Next checkpoint |
|---|---|---|---|---|---|---|
| EXP-001 | Anomaly-conditioned accuracy audit (descriptive) | none (on demand) | n/a | none | DEFER | when an anomaly question needs it |
| EXP-002 | Softened selection bar on anomaly days (shadow) | active, 86 rows | BLOCKED: no sample or rule registered; endpoint aggregates the sealed period | none (logged only) | BLOCKED | owner pre-registers a sample and rule; then evaluate later rows |
| EXP-003 | Momentum LCA ranks above production choice | active, 52 rows (34 unique post-seal) | prospective sign test frozen; 2026-10-10 to 10-30 descriptive only | none (logged only) | CONTINUE | 60 non-tied per horizon from 2026-11-01 (~2 months) |
| EXP-004 | TimesFM 2.5 BTC direction | active daily, 21/21 post-seal runs | prospective McNemar plan frozen; 19 paired post-seal origins per horizon show no difference (descriptive) | none | CONTINUE | 100 resolved origins per horizon (~mid-Feb 2027) |
| EXP-005 | V1 source incremental value | active weekly (last 2026-10-26) | BLOCKED: ~90-day windows overlap the seal until ~2027-01-07, and weekly windows overlap, so "independent weekly runs" cannot be met as registered | none (research only) | BLOCKED | owner rules on independence; first fully post-seal window ~2027-01-07 |
| EXP-006 | V1 coefficient OOS validation | none | no hypothesis | none | DEFER | after EXP-005 yields a candidate |
| EXP-007 | Sentiment vs no-sentiment | none | no protocol; needs V4 data | none | DEFER | when a protocol and data path exist |
| EXP-008 | Chronos-2 BTC direction | active since 2026-10-31 (real model) | plan frozen (origins from 2026-11-01) | none (research only) | CONTINUE | 100 paired origins per horizon |
| EXP-009 | Event × source evidence (schema validation) | active weekly | BLOCKED: milestone check needs stored batches overlapping the seal | none | BLOCKED | owner allows a structural-only check, then PAUSE the weekly run if it holds |
| EXP-010 | Source dialogue validation (diagnostic) | active weekly | BLOCKED: as EXP-009; registry next_action stale | none | BLOCKED | as EXP-009 |
| EXP-011 | Experiment 5, agentic evidence | active | BLOCKED: no minimum sample; results aggregate the sealed period | none | BLOCKED | owner sets a minimum and a post-seal cut; decides PRs #83/#84 |
| EXP-012 | Stage 7 human-controlled workflow | staging dispatcher runs daily | staging only; PRs #82/#85 inactive since early October | none | DEFER | owner finishes Stage 7 or closes #82/#85 |
| EXP-023 | BTC 12h early warning (Goal B) | active daily | 4/60 events (all DOWN), REPORTED by the daily evaluator | none | CONTINUE | early November: October Binance funding archive (F3/F4) |
| EXP-024 | Forward OI and funding collection | active daily and hourly | collection only | none | CONTINUE | monthly completeness check |
| EXP-025 | V1 source `hloi` | active hourly (375 snapshots) | BLOCKED: criterion has no test, window or sample | **V1 composite since 2026-10-11, owner decision** (V1 changelog `2026-10-11-hloi`), weight 4 × 0.3 | BLOCKED | owner freezes an OOS test before any weight change |
| EXP-026 | Binance OI candidate | active via EXP-024 (22 days) | pre-registered test 46c0d52b (REPORTED) | none | CONTINUE | at the pre-registered horizon |
| EXP-027 | Risk Regime Shock (GDELT + market) | market component stored hourly by V1 | BLOCKED: no forward test | **market component `riskregime` (rrs-v1) in V1 since 2026-10-12, owner decision** (V1 changelog `2026-10-12-riskregime`), weight 4 × 0.3 | BLOCKED | owner freezes a forward test of stored rrs-v1 readings |
| EXP-028 | DVOL and USDT supply candidates | active hourly (770 snapshots) | BLOCKED: no plan | none | BLOCKED | owner freezes a plan |

## Unregistered workloads

**Event Detection (Goal A):**
- **Runs:** enabled, every run succeeds. But GitHub starts only about 5 of the 24 hourly scheduled runs per day
  (2026-10-29 to 31: 10:12, 16:58, 21:52, 02:01, 08:46, 16:01, 20:54, 00:52 UTC). (VERIFIED)
- **Effect:** between runs the OKX (4 h freshness limit) and Deribit detectors read STALE (both STALE at 06:30 UTC on
  2026-10-31). (VERIFIED, `/api/events/health`)
- **Consequence:** fewer watched episodes. Goal A evaluability (≥ 20 watched episodes, ≥ 5 per direction) is still
  not exposed by the API (BLOCKED).
- **Owner decision H3 (2026-10-31): add a second trigger.** The hourly funding job is not a usable anchor: it is also a
  GitHub schedule and started only 4–6 times a day over 2026-10-24 to 31 (VERIFIED; corrects an earlier note here).
  Implemented as the Worker's own hourly cron (the former 3h trigger, no new trigger): detection at the 16 hours not
  divisible by 3, prediction tick unchanged at the other 8.

**V1 shadow weight adaptation:**
- **Status:** its verdict endpoint (`sentiment-ff75…/weight-adaptation`) is unreachable from this environment
  (BLOCKED).
- **Code (VERIFIED):** it only proposes. The owner must approve before any default weight changes.

## Shared dependencies

- **EXP-024's forward store** feeds EXP-023 (funding variants F3/F4), EXP-026 (OI) and the research archive. Keep it.
- **The hourly funding job** feeds EXP-023 and the forward store. Keep it. (It starts only ~5 times a day.)
- **EXP-008** depends on EXP-004: it forecasts only at TimesFM's origins. **The EXP-004 prospective test** depends on
  EXP-008 for the persistence comparator. Keep both.
- **`export-learning-data`** feeds the archive and the k-NN baseline used by EXP-004 and EXP-008. Keep it.
- **EXP-005, 009 and 010** are weekly. They are cheap, but they produce data that cannot be evaluated until the seal or
  independence ruling. PAUSE is reasonable for EXP-009 and EXP-010 once their structural milestone is checked.

## Owner decisions (approved 2026-10-31, as written)

Served live as `decision.owner_directives` (per experiment) and `decision_model.global_owner_directives` (H3). They
govern how work proceeds; they do not change any experiment's state and are not REJECTED / IMPLEMENTED decisions.

1. **H1, seal overlap (mixed):** EXP-002, 005 and 011 wait for fully post-seal windows before any predictive
   evaluation. EXP-009 and 010 may run structural checks only (schema, duplicates, labels, temporal integrity), with no
   predictive conclusion; a check that cannot be isolated from sealed outcomes stays blocked.
2. **H2, pre-registration:** EXP-002, 011, 025, 027 and 028 need a frozen prospective plan before any data supports a
   performance claim. Sample sizes come from a stated minimum effect, power, alpha and multiplicity correction, never
   invented. Priority: EXP-025 and the EXP-027 market component (prospective validation of existing production use;
   production decisions and weights unchanged). EXP-027 is scoped to the market component (rrs-v1); the combined
   signal was never built.
3. **H3, Event Detection:** add a second, reliable trigger, deduplicated and verified by executed runs and freshness.
4. **H4, Stage 7 / PRs #82–#85:** finish Stage 7 in staging against explicit acceptance criteria; production
   migrations need separate authorization. EXP-011 PRs #83/#84 stay blocked until a prospective plan and a seal-safe
   evidence path exist.
