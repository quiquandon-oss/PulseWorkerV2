# GLM-PE-1: prospective evaluation of the GLM evidence layer against the V2 baseline (frozen 2026-11-19)

**Why.** The owner's mandate allows a bounded GLM influence on the served V2 BTC 24 h probability. It also requires
that no improvement be claimed until a prospective, leakage-safe evaluation shows it, and that no cap above 0 be
applied before **≥ 30 qualifying resolved signals** exist. This protocol fixes, before any GLM signal exists, how
that evaluation is done.

**Frozen at:** this commit. No GLM run with inference has happened. At freeze time production has inference **off**
(`GLM_INFERENCE_ENABLED = "0"`), so every stored signal is `NO_SIGNAL` (`model_disabled`). No outcome of any GLM
signal has been computed or viewed.

**Changes after this commit:** only by a new versioned protocol (GLM-PE-2, …) committed before the analysis it governs.
The qualification rules (`glm-qual-0.1`) and the reference cap may be revised for a later protocol. They are never
tuned on the data of the protocol that evaluates them.

**Separation from PEV-1P.** PEV-1P (`2026-11-02-pev1p-protocol.md`) is unchanged and unaffected: its window, units,
models, counts-only mode and 2027-05-01 checkpoint stay as frozen. GLM-PE-1 uses different units (served V2 BTC 24 h
predictions) and a different resolution: the prediction's own `realized_up`, the rule V2 already uses.

## Units and data

- **Signal unit:** one `glm_signals` row with `asset = 'BTC'`, `horizon_hours = 24`, `eligibility = 'ELIGIBLE'`, from a
  run with status `completed`, rule version `glm-qual-0.1`, and `decision_cutoff_ts ≥` the first production run with
  inference on. **`CANDIDATE` rows are recorded and reported separately; they never count toward the 30.**
- **Prediction rows of a signal:** the `glm_adjustments` rows whose `prediction_ts` falls in `[valid_from_ts,
  valid_until_ts)` of that signal. These are BTC 24 h served predictions from the 3-hourly ticks, at most 8 per
  signal. They are one **cluster**: scored per signal (cluster mean) and also listed per prediction. Per-prediction
  numbers are never treated as independent.
- **Outcome:** `predictions.realized_up` of the same `(horizon_hours, ts)`, available once `resolved_ts` is set. A
  signal is **resolved** when all of its prediction rows are resolved.
- **Baseline:** `glm_adjustments.baseline_p_up`, the displayed headline (`cycle_p_up` for a non-original chosen
  variant, else `chosen_p_up` when that is absent, else `p_up`), exactly as served.
- **GLM-adjusted (shadow):** `baseline_p_up + would_apply`, clamped to [0.02, 0.98] (never pushed further out than the
  baseline). `would_apply = dir × k(strength) × 0.05`, with k = 1/3, 2/3, 1. This is the reference scale, not a
  validated cap.
- **Temporal integrity checks**, required for a signal to count:
  1. every `input_refs[].fa` ≤ `decision_cutoff_ts`;
  2. the signal's `valid_from_ts` > `decision_cutoff_ts`;
  3. every prediction row has `prediction_ts ≥ valid_from_ts`;
  4. no input's `resolved_ts` > cutoff.

  A failing signal is excluded **and reported**.

## Metrics, reported per signal (cluster mean) and pooled

- **Brier score**, baseline vs GLM-adjusted: difference and its sign per signal.
- **Directional accuracy** at 0.5, baseline vs adjusted, and the number of **flips** (predictions whose 0.5 side
  changed).
- **False trend activation:** share of `ELIGIBLE` signals whose direction is wrong at the 24 h target of the signal's
  first prediction.
- **Abstention rate:** share of daily runs (inference on) ending in `NO_SIGNAL`, by reason. **Candidate rate:** share
  ending in `CANDIDATE`.
- **Coverage:** share of BTC 24 h served predictions inside any `ELIGIBLE` window.
- **Operational:** run status counts; validation failures; Neurons per run; D1 rows/calls per run.
- **Slices,** only where ≥ 10 signals per slice; otherwise listed as "insufficient":
  - signal direction (UP/DOWN);
  - strength;
  - TimesFM/Chronos agreement (both agree with the signal / split / both oppose);
  - Gemini-derived support present or absent among the supporting refs.

## Decision rule for the owner's cap decision (not automatic)

At **30 resolved ELIGIBLE signals** (the earliest; later analyses keep the same rule):
- **Supports enabling a cap > 0** only if **all** of the following hold:
  - the mean Brier difference (adjusted − baseline) over signals is **< 0**;
  - a one-sided sign test over per-signal Brier differences has **p < 0.10**;
  - there is no temporal-integrity failure among the counted signals;
  - false trend activation is **< 50 %**.
- **Otherwise:** report "no demonstrated improvement". The cap stays 0, and the thresholds may be revised only in a
  new protocol.
- The result never sets the cap by itself. The owner chooses the cap (≤ 0.10 hard maximum in code) after reading the
  report. Negative and inconclusive results are reported in the same format as positive ones.

## Reproducibility

- **Inputs:** `GET /api/glm/signals?horizon=24&limit=30`, `GET /api/glm/adjustments?limit=60` (joined outcomes), and
  `GET /api/glm/run?id=…` (lineage per signal), or the same tables read directly with bounded queries.
- **Code:** `research/glm_pe1.py` (to be added before the first ELIGIBLE signal resolves), with tests on synthetic
  fixtures only.
- **Output:** a dated report under `research/results/glm-pe1/`, with the commit SHA and the exact signal ids used.
