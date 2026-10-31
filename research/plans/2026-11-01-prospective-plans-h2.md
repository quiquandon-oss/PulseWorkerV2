# Prospective evaluation plans: EXP-025, EXP-027 (market component), EXP-028, EXP-002, EXP-011 (FROZEN)

**Frozen:** 2026-10-31, under owner decision H2 (approved 2026-10-31): a frozen prospective plan is required before any
data supports a performance claim. No observation in any population below exists yet: every population starts at
**2026-11-01T00:00:00Z** (epoch ms 1793491200000), or later where stated.

**Changes:** none after the first observation, except by a new versioned plan committed before the checkpoint it
affects. These plans are not changed to fit results.

**Sample sizes:** computed by `research/power.py` (tested in `research/test_power.py`) from the minimum effect, power,
alpha and Holm family size stated for each plan. Sizing uses the strictest Holm step (alpha / m). Nothing is chosen by
hand.

**Common rules:**
- **Sealed period:** no outcome before 2026-10-09T21:00Z is read (owner decision H1).
- **BTC price at time t:** the last `btc_data` price at or before t. **Resolution price for horizon h:** the first
  `btc_data` price at or after t + h. If that price is more than 60 minutes after t + h, the unit is excluded as
  unresolved.
- **Direction:** UP if the return is strictly above 0, otherwise DOWN.
- **Exclusions:** every exclusion is counted and reported. Nothing is excluded after looking at outcomes.
- **No interim analysis for success.** One confirmatory analysis per plan, at its checkpoint.
- **Production:** no plan changes any production weight or decision. A result is reported to the owner, who decides.

## A. Daily incremental-association design (EXP-025, EXP-027, EXP-028)

**Question:** does the source carry information about BTC's next 24 hours beyond the V1 composite?

**Unit:** one per UTC day. The day's reading is the first V1 `history` row at or after 00:00 UTC, if it is within 60
minutes. Otherwise the day is excluded (counted). One reading a day with a 24h horizon gives non-overlapping outcome
windows.

| Variable | Definition |
|---|---|
| `x` | the source's score in that row's `sources_json`, as served to the composite |
| `c` | that row's V1 composite score (covariate) |
| `y` | 24h BTC log return from the row's timestamp, resolved as above |

**Statistic:** Spearman partial correlation of `x` and `y` given `c`. That is the Pearson partial correlation of the
three rank-transformed variables. Test: Fisher z, two-sided, with n − 1 − 3 degrees of freedom for one covariate.

**Why `c` includes the source:** the stored composite already contains the production source (EXP-025, EXP-027) at
about 1% effective weight. Controlling for it removes the composite's information and leaves the source's own
component only, a conservative choice. No composite is recomputed.

**Minimum effect:** |partial ρ| = 0.20. This is the smallest association treated as decision-relevant for a weight
change. The exploratory estimates for these sources were 0.17–0.28 (CIs including 0). A smaller effect is not
detectable at one reading a day within a year: ρ = 0.10 would need about 785 days.

**Power and alpha:** 0.80; α = 0.05 two-sided, Holm within each plan.

**Outcome labels at the checkpoint:**
- **Supported:** significant, and the sign matches the direction in which the composite uses the source (higher score
  = more bullish).
- **Opposite:** significant with the other sign. This is evidence against current use and is reported to the owner.
- **Not detected:** not significant. This is not proof of no effect at the size planned for.

| Plan | Source (`sources_json` key / table) | Holm family | Checkpoint (daily units) | Earliest date (no exclusions) |
|---|---|---|---|---|
| EXP-025 | `hloi` (V1, production since 2026-10-11) | 1 | **195** | ~2027-05-15 |
| EXP-027 | `riskregime`, rule rrs-v1 market component only (V1, production since 2026-10-12) | 1 | **195** | ~2027-05-15 |
| EXP-028 | DVOL and USDT supply (`research_source_snapshots`; daily value = last snapshot at or before 00:00 UTC) | 2 | **236** each | ~2027-06-25 |

**EXP-025 and EXP-027 are prospective validation of existing production use** (H2). Their production decisions
(`2026-10-11-hloi`, `2026-10-12-riskregime`) stand and their weights are not changed by this plan.

**EXP-027 scope:** only the market component (rrs-v1) exists. The combined GDELT + market signal registered as EXP-027
was never built and is not evaluated here. Building it would need its own plan.

**EXP-028 data:** it is not in the composite, so `c` is the composite alone. A day without a snapshot at or before
00:00 UTC within 2 hours is excluded (counted).

## B. EXP-002: anomaly-day shadow selection vs production selection

**Registered criterion (frozen):** "Shadow choices outperform production choices on resolved outcomes."

**Pairing:** each `selection_decisions_anomaly` row (BTC, `ts` ≥ 2026-11-01) is paired with the `selection_decisions`
row of the same coin, horizon and `prediction_ts`.
- Shadow call: `chosen_p_up_anomaly` ≥ 0.5 means UP.
- Production call: its chosen variant's `p_up` ≥ 0.5 means UP.

**Informative unit:** a pair whose two calls differ, so exactly one is right once resolved. Pairs with the same call
cannot discriminate; they are counted and excluded.

**Independence:** decisions every 3 hours with 12h or 24h horizons overlap. Per horizon, informative units are taken
in time order, keeping a unit only if its window `[prediction_ts, prediction_ts + h)` does not overlap the previous
kept unit's window. Dropped units are counted.

**Test:** exact two-sided sign test of P(shadow right) = 0.5 per horizon; Holm over 2 horizons, α = 0.05.
- **Minimum effect:** P(shadow right | informative) = 0.65. Power 0.80.
- **Checkpoint:** **111** kept informative units per horizon.
- **Supported:** significant with more than half shadow-right at a horizon.

**Limitation:** anomaly days are rare and informative pairs rarer still. No date is estimated until the first month's
count of informative units is known. The plan is not changed if the count is low.

## C. EXP-011: Experiment 5 agentic evidence

**Registered criterion (frozen):** "Hypotheses pass out-of-sample holdout."

**Population:** resolved Experiment 5 directional decisions whose anchor is at or after the later of 2026-11-01 and
the date the pipeline is activated in production. It is not active today, and PRs #83/#84 stay blocked (owner decision
H4), so this population is empty until then.

**Unit:** one decision per subject per non-overlapping 24h window (the first in each window; later ones are counted
and dropped).

**Test:** exact two-sided binomial test of hit rate = 0.5, α = 0.05.
- **Minimum effect:** hit rate 0.60. Power 0.80.
- **Checkpoint:** **210** units.
- **Supported:** significant with a hit rate above 0.5.

**State:** blocked until production activation and a seal-safe evidence path exist (H1, H4). The plan is frozen now
so that activation cannot be followed by a post-hoc rule.

## Evaluators

- **EXP-003, 004, 008:** `research/prospective_eval.py` implements the earlier frozen plans.
- **This plan:** its evaluators are committed and tested before the earliest checkpoint they serve. Until then the
  registry reports these experiments as collecting toward their checkpoint (or blocked, for EXP-011), never as
  evaluated.
