# EXP-029: prospective comparison of V1 composite weightings (PROPOSED, not registered)

**Status: PROPOSED.** Written 2026-11-01. The owner must approve two things before this applies:
- registration of EXP-029 in the experiment registry, which needs a registry migration;
- activation of provenance recording (PulseWorker `HISTORY_PROVENANCE=1`, quiquandon-oss/PulseWorker#2).

Until then nothing is collected under this plan. It changes no production weight or behaviour, and it does not modify
any frozen experiment:
- **EXP-023, EXP-025, EXP-027 and EXP-028:** untouched. Their plans, populations and checkpoints stand. EXP-025 and EXP-027 use the
  stored production composite as a covariate, which this plan never alters.
- **EXP-002, 003, 009, 010, 011:** untouched.

**Sample size:** computed by `research/power.py`. `research/test_exp029_spec.py` asserts that the numbers quoted here
equal its output.

## Why a new experiment

The 2026-11-01 sentiment weighting audit and PEV-1 (quiquandon-oss/PulseWorkerV2#111) found:
- **No detectable effect.** The current composite shows no detectable directional skill on the post-seal sample (18
  daily units, every CI including 0).
- **Unreproducible history.** Only 63 of 206 post-seal composites can be reproduced from stored data: confidence,
  boost and per-reading weights were not recorded.
- **Sparse readings.** Browser-driven writes leave most daily units without a reading.

Weighting can therefore only be compared prospectively, on readings whose inputs are recorded. No past data is
reused for this test.

## Population and unit

- **Start:** the first UTC day after provenance recording is activated and has run for 24 h. The day is recorded in
  the registry at registration, before any outcome exists.
- **Unit:** one per UTC day.
  - **Reading:** the first `history_provenance` row with `history_ts` at or after 00:00 UTC, within 60 min, and
    `reproducible = 1`. Otherwise the day is excluded (counted by reason: no reading, late reading, not reproducible).
  - **Why reproducible readings only:** each weighting is recomputed from that row's recorded inputs, so a row whose
    inputs do not reproduce the stored composite cannot be re-weighted faithfully.
- **Outcome:** the 24 h BTC log return from the reading, using the H2 common rules:
  - price: the last `btc_data` price at or before t;
  - resolution: the first price at or after t + 24 h, unresolved if more than 60 min late;
  - direction: UP if strictly above 0.

## Weightings (fixed now, m = 3 comparisons)

Each weighting is applied to the same recorded inputs with production's arithmetic: weight × confidence, renormalised
over the sources that answered. A missing source is never filled with a neutral value. No source is removed for low
variability; per-source variability is reported descriptively only.

| Id | Weighting | Question |
|---|---|---|
| W0 | Production, exactly as recorded: base weight × boost × confidence | Reference |
| W1 | Equal base weight 1 for every answering source; recorded confidence kept; no boost | Do the hand-set base weights add value? |
| W2 | Recorded base weights, confidence fixed at 1, no Foufi boost | Does the confidence and boost layer add value? |
| W3 | The PulseWorker weight-adaptation shadow proposal in force at the start date, frozen (its JSON and sha256 recorded at registration); recorded confidence kept; no boost | Does the existing adaptive proposal add value out of sample? |

**Calls:** composite > 50 → UP; < 50 → DOWN; exactly 50 → no call (counted).

## Test

**Comparison:** each Wk (k = 1, 2, 3) against W0.
- **Informative unit:** a day on which the two calls differ, so exactly one is right once the day resolves. Days with
  the same call are counted and excluded.
- **Test:** an exact two-sided sign test of P(Wk right | calls differ) = 0.5.
- **Multiplicity:** Holm across the 3 comparisons, α = 0.05.
- **Minimum effect and power:** P = 0.65, power 0.80.
- **Checkpoint:** **122** informative units per comparison (`n_sign_test(0.65, m=3)`). Each comparison is evaluated
  once, when it reaches its checkpoint.
- **Limit:** a comparison that has not reached its checkpoint by 2028-11-01 is reported as **NOT EVALUABLE**, with
  its counts.
- **Labels:**
  - **Supported:** significant, with Wk right on more than half of the informative units.
  - **Opposite:** significant, with W0 right on more than half.
  - **Not detected:** otherwise. This is not proof of equivalence.
- **No interim look:** no analysis for success happens before the checkpoint.

**Expected duration:** informative units come only from days on which the calls differ. If, for example, 25% of days
differ, 122 informative units need about 490 eligible days. The first month's discordance rate is reported (rate
only, no outcomes) so the owner can judge feasibility. The plan is not changed because the rate is low.

## Descriptive monitoring (no inference, no outcome-dependent changes)

Reported monthly from provenance alone, with no returns:
- reproducibility rate;
- completeness distribution;
- per-source availability and missing reasons;
- freshness coverage (share of sources with an observation time);
- boost frequency;
- per-source score variability.

## Decision use

**Any result is reported to the owner, who decides.** A "Supported" result is evidence for a weighting change. It is
not an automatic change.

**Before production use, a separate release plan is needed.** That plan defines a controlled rollout and states how the
changed composite interacts with EXP-025 and EXP-027, because both use the composite as their covariate.
