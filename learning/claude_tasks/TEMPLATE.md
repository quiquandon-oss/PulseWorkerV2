# EXP-NNN — Claude Implementation Task

Derived from: `learning/experiments/EXP-NNN.md`
Status: IMPLEMENTED / BACKTESTING / OUT_OF_SAMPLE (update as work proceeds)

## Scope

Restate the change in implementation terms: files touched, functions touched, new columns/tables if any.

## Explicit Non-Goals

Copied from `.ai/IMPLEMENTATION_PLAN.md` Critical Restrictions, re-stated per-experiment:
- No change to Production model weights unless this experiment is specifically about weights, and even then: Challenger/Research only, no auto-promotion.
- No change to feature weights outside this experiment's declared scope.
- No automatic promotion.

## Implementation Notes

What was actually built. Deviations from the spec, and why, if any.

## Tests Added

List new test file(s)/case(s). All existing tests must still pass — paste the full `vitest run` summary line.

## Backtest

Period, sample size, results.

## Out-of-Sample Validation

Period (must be temporally AFTER the backtest period), sample size, results.

## Leakage Review

Answer each of `.ai/EXPERIMENT_PROTOCOL.md`'s 5 leakage questions explicitly for this change.

## Result

Link to `learning/results/EXP-NNN.md` once complete.
