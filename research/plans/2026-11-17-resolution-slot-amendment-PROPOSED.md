# PROPOSED amendment (not adopted): resolve at the target's own 3-hourly tick

**Status: PROPOSED, NOT ADOPTED. Owner decision 2026-11-17:**
1. The frozen rule stays the **primary protocol** through the 100-origin checkpoint. It is not replaced on the basis
   of interim results.
2. The slot-aware rule is a **sensitivity analysis only**, always reported separately next to the primary result.
3. Original outcomes stay immutable. Any alternative scoring is stored as separate, explicitly versioned records
   (`resolution_rule: slot-v2-sensitivity`) and never overwrites a `frozen-v1` outcome.
4. Production resolvers and research workflows are unchanged.

The frozen plan
(`2026-11-01-prospective-evaluation-plans.md`) stays the rule until the owner adopts a versioned plan. Its own change
rule allows that only before the checkpoint it affects (100 resolved origins per horizon; 15 at 12 h and 14–15 at
24 h on 2026-11-17).

## Finding (production evidence, read-only, 2026-11-17)

Frozen rule: *the first `btc_data` price at or after `target_ts = input_end_ts + horizon`.*

`btc_data` gets one price per 3-hourly prediction tick, written a few seconds after the hour with seconds of jitter.
`input_end_ts` is itself such a tick timestamp. So `target_ts` lands within seconds of the tick that belongs to it. If
that tick's price is written even 1 ms *before* `target_ts`, the frozen rule skips it and takes the next tick, about
3 h later.

Example (the case behind the 2026-11-17 report):
- Origin 2026-11-16 15:01:07.811 (TimesFM id 153 and the Chronos-2 12 h forecast).
- Target 2026-11-17 03:01:07.811.
- The 03:00 tick was written at 03:00:59.981, **7.83 s before the target**. The frozen rule therefore waits for the
  06:00 tick, about 06:01 UTC.

Over the EXP-008 ledger on 2026-11-17 (34 resolved forecasts, read-only):
- **19 of 34** were resolved on the next tick, about 3 h late: 11 of 17 at 12 h, 8 of 17 at 24 h. In these cases the
  slot tick was written 1.8–50.4 s before the target.
- One more was resolved 12 h late because of a genuine gap in `btc_data` (2026-11-08 12:01 to 2026-11-09 00:01).
- **2 correctness outcomes change** under the slot rule: one prospective (12 h, origin 2026-11-03 15:01, correct → wrong)
  and one post-seal descriptive (12 h, origin 2026-10-31 12:01, wrong → correct).
- EXP-004 (TimesFM) uses the same rule on the same origins, so it has the same exposure.

The measured "12 h" and "24 h" outcomes are therefore a mix of 12/15 h and 24/27 h horizons. Both models are
equally affected, so paired comparisons stay paired, but they are not measured at the stated horizon.

## Proposed rule

> The actual price is the first `btc_data` price with `ts >= target_ts - 120 s`. If there is none, the forecast stays
> pending: it is retried and never imputed.

- 120 s is more than twice the largest early arrival observed (50.4 s). It is also 1/90 of the tick spacing, so it can
  never reach the previous tick.
- A genuine data gap still resolves on the next available price, exactly as now. That is counted and reported.
- Every resolution would record `resolution_rule` (`frozen-v1` or `slot-v2`) and the price timestamp it used.

## Options considered (decided above: option 2 as primary, the slot rule as sensitivity analysis)
1. **Adopt for both EXP-004 and EXP-008 before the 100-origin checkpoint.** Re-resolve every prospective origin under
   `slot-v2` as new, separate records. The immutable `frozen-v1` files and rows stay. Report both, with `slot-v2`
   primary.
2. **Keep the frozen rule** and report the horizon mix (12/15 h, 24/27 h) as a disclosed limitation.

## Tooling
`research/resolution_slot_audit.py` (tested in `research/test_resolution_slot_audit.py`) computes both rules from a
ledger and a price list. It is read-only: it writes nothing and changes no resolution.
