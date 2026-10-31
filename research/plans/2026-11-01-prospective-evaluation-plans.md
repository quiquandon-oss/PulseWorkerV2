# Prospective evaluation plans: EXP-003, EXP-004, EXP-008 (FROZEN)

**Frozen:** 2026-10-31, under the owner's authorization of 2026-10-31 ("pre-register the evaluation protocol before
confirmatory analysis"). The plans were written before any observation in their population exists: every population
below starts at **2026-11-01T00:00:00Z** (epoch ms 1793491200000).

**Changes:** none after the first observation, except by a new versioned plan committed before the checkpoint it
affects. The earlier post-seal looks (EXP-004 and EXP-003 data from 2026-10-10 to 2026-10-31) are descriptive only and
excluded from every confirmatory population here.

**Common rules:**
- **Sealed period:** no outcome before 2026-10-09T21:00Z is read.
- **Direction:** UP means a return strictly above 0, otherwise DOWN. This is EXP-004's rule.
- **Resolution:** the first `btc_data` price at or after `target_ts = input_end_ts + horizon`, compared with the price
  at the origin. EXP-004's rule, also used by EXP-008.
- **Exclusions:** every exclusion is counted and reported. Nothing is excluded after looking at outcomes.
- **Tests:** exact two-sided tests (McNemar for paired correctness, sign test for paired rankings).
- **Multiplicity:** Holm correction across the comparisons listed for each plan, at α = 0.05.
- **Evaluator:** `research/prospective_eval.py` (tested in `research/test_prospective_eval.py`).

## Last-tick persistence (definition used by EXP-004 and EXP-008)

For a forecast origin `t0 = input_end_ts`:
1. Take `btc_data` up to and including `t0`.
2. Deduplicate it with EXP-004's `dedupe_near_simultaneous` (60 s tolerance).
3. Let `P0` be the price at `t0` and `P-1` the previous deduplicated point.
4. Persistence calls UP if `(P0 − P-1) / P-1 > 0`, otherwise DOWN.

EXP-008 records this as `persistence_direction` in every forecast file. EXP-004's persistence call at an origin is taken
from the EXP-008 forecast file at the same origin. If that file is missing, the origin is excluded from the persistence
comparison only.

## EXP-004: TimesFM BTC challenger

| Item | Rule |
|---|---|
| Population | `experiment_4_timesfm` rows with `input_end_ts ≥ 2026-11-01T00:00Z`, resolved, per horizon (12h, 24h) |
| Comparators | always-UP; last-tick persistence (above); descriptive only: production k-NN (`p_up ≥ 0.5`, nearest prediction within [−3 h, +10 min] of the origin) |
| Test | exact McNemar, TimesFM vs always-UP and vs persistence, per horizon. Holm over these 4 comparisons. |
| Checkpoint | first analysis at **100** resolved origins per horizon. No interim analysis for success. |
| Success | at a horizon, TimesFM is significantly better than **both** always-UP and persistence at the checkpoint **and** in the next 100 non-overlapping origins (replication). This is the frozen registry criterion made operational. |
| Otherwise | "no evidence of skill" at that horizon after the replication window |

## EXP-008: Chronos-2 BTC challenger

`exp008-chronos/EVALUATION_PLAN.md` is frozen with the same population start (origins ≥ 2026-11-01T00:00Z). The
checkpoint is 100 paired origins per horizon, and success means significantly better than both always-UP and
persistence (Holm over 8 comparisons), replicated in the next 100. Comparisons with TimesFM and k-NN are reported but
are not part of the success criterion.

## EXP-003: Momentum overlay (challenger_momentum)

**Registered criterion (frozen):** "Momentum LCA ranks above the production choice on resolved outcomes."

**Operational definition:** each `selection_decisions_momentum` row's own `scores_json` contains the LCA of every
scored variant, computed on the same set of similar historical days.
- **"The production choice"** is the row's `production_chosen_variant`.
- **The comparison** is momentum's LCA against that variant's LCA in the same `scores_json`.
- **Not the stored `production_chosen_lca`:** that column holds the LCA of the *best-scoring* variant (`decision.winner.lca`
  in `selectBestVariant`), not the chosen one's. Production often falls back to `original` without clearing the gate.

| Item | Rule |
|---|---|
| Population | unique (horizon, `prediction_ts`) rows with `ts ≥ 2026-11-01T00:00Z`, BTC, per horizon. A repeated row keeps its first log. |
| Exclusions | ties; rows where the chosen variant has no LCA in `scores_json`. Both counted. |
| Test | exact two-sided sign test on non-tied rows per horizon, H0: P(momentum above) = 0.5. Holm over 2 horizons. |
| Checkpoint | first analysis at **60** non-tied rows per horizon |
| Success | at a horizon, momentum ranks above significantly more often than half at the checkpoint **and** in the next 60 non-tied rows |
| Otherwise | "criterion not met" at that horizon after the replication window |

**Limitation:** LCA is a backward-looking local accuracy score. Meeting this criterion means momentum *ranks* above the
production choice, which is what was registered. It is not a test of forward accuracy.
