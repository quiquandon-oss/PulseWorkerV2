# EXP-NNN — [short title]

Status: PROPOSED
Proposed by: ChatGPT
Date: YYYY-MM-DD

> Filled out by ChatGPT after independent review of `/api/learning/chatgpt`
> evidence. Claude does not fill this out or propose experiments on its own
> initiative — see `.ai/AI_COLLABORATION.md`.

## Observation

What happened? (cite the specific daily report date(s) / metric(s) that triggered this)

## Evidence

What data supports the observation? Include sample sizes — a single bad prediction is not evidence (`.ai/ARCHITECTURE.md` core principle).

## Hypothesis

What might explain it?

## Mechanism

Why should the proposed change plausibly fix or improve on the observation?

## Baseline

Which exact Production model/table/horizon is being compared against? (e.g. `predictions`, BTC, 24h, `calibrated_p_up`)

## Proposed Experiment

Exactly what should Claude change? Be specific enough that Claude does not need to guess scope.

## Metrics

Which of accuracy / Brier / log loss / calibration error / other decide this?

## Acceptance Criteria

What result would count as PASSED?

## Failure Criteria

What result would count as FAILED? (define this before implementation, not after seeing results)

## Leakage Requirements

What must Claude verify has NOT happened (look-ahead, calibration leakage, neighbor leakage, etc. — see `.ai/EXPERIMENT_PROTOCOL.md`)?

## Sample Size / Statistical Notes

Minimum n required before this can be evaluated at all. Confidence interval expectations if relevant.
