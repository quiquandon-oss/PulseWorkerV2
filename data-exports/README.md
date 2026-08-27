# CryptoPulseV2 — Learning Data Export

Auto-refreshed daily (`.github/workflows/export-learning-data.yml`, 08:00 UTC,
also runnable on demand via workflow_dispatch) directly from production D1.
**Check `STATUS.md` in this same directory for the actual last-refresh
timestamp and row counts before treating any of this as current** — this
README describes the schema, not today's data.

## Files

| File | What it is |
|---|---|
| `btc_predictions.csv` | BTC core k-NN model — every prediction ever made, both horizons |
| `link_predictions.csv` | LINK core k-NN model — same shape |
| `eth_predictions.csv` | ETH core k-NN model — same shape, newer coin, less history. Includes `features_json` (raw feature snapshot at prediction time) |
| `challenger_predictions.csv` | Challenger model (flat + Foufi-tilted variants), all 3 coins together, distinguished by `coin` column |
| `selection_decisions.csv` | The meta-layer: which variant (original/experimental/calibrated/challenger_*) was actually chosen live for each coin/horizon/cycle, and why |
| `STATUS.md` | Auto-generated on every run: exact refresh timestamp, row counts, earliest/latest `ts` per file |

## Column notes (non-obvious ones)

**Core prediction tables (btc/link/eth_predictions):**
- `p_up`: raw k-NN analog-match probability (median-based)
- `p_up_experimental`: adaptive-K, distance-weighted variant, logged in parallel, not yet trusted over `p_up`
- `calibrated_p_up`: decile-recalibrated version of `p_up` (falls back to raw if no curve exists yet)
- `calibrated_conditional_p_up` (BTC only): a second calibration variant using condition-matched neighbors with feature weights `{regime_mag:1.5, technical_score:1.0, score:1.0, bottom_score:0.3}` — computed and logged but **not currently used in live selection** (it's not one of the choosable variants in `selection_decisions`)
- `is_regime_anomaly`: 1 if today's feature vector is in the most-distant 10% vs. historical feature vectors (a novelty/out-of-distribution flag, direction-neutral by design — it does NOT mean "bullish" or "bearish", just "unusual")
- `n_analogs` / `k_used`: how many historical neighbors the prediction was based on
- `closest_analog_dist`: distance to the single nearest historical match (higher = more novel conditions)
- `trend_strength`: signed [-1,1], short-MA-vs-long-MA slope (positive = uptrend, negative = downtrend) — a lagging indicator by construction
- `model_version` / `git_commit_sha`: which deployed code version produced this row — useful for correlating behavior changes with deploys
- `resolved_ts` / `realized_up` / `realized_return`: NULL until the `target_ts` has passed and the outcome is known

**challenger_predictions.csv:**
- `p_up_flat`: Challenger's own k-NN estimate, no external tilt
- `p_up_tilted`: adjusted by a Foufi-digest-derived directional tilt when a fresh digest exists (often falls back to flat — check `driver_used`/`driver_agreement`)
- `calibrated_p_up_flat`: Challenger's own calibration curve applied to `p_up_flat`

**selection_decisions.csv — the interesting one for a cross-coin audit:**
- `chosen_variant`: one of `original`, `experimental`, `calibrated`, `challenger_flat`, `challenger_tilted`, `challenger_calibrated` — whichever variant's Local Class Accuracy cleared the statistical significance gate; defaults to `original` if none did
- `lca_score`: the winning variant's Local Class Accuracy in its own neighborhood
- `comparison_count`: how many variants were eligible to compete this cycle (m in the Bonferroni correction, 1-6)
- `corrected_alpha` / `cleared_gate`: the significance test outcome — `cleared_gate=0` means no variant beat the bar, `original` was used by default (this is normal/expected, not a failure)
- `k_sel`: neighborhood size used for the LCA comparison itself (7-15) — separate from the core prediction's own `k_used`
- `scores_json`: every eligible variant's own LCA score this cycle, not just the winner's — useful for seeing how close competing variants were

## Historical context (may be stale — cross-check dates against STATUS.md / `git_commit_sha` transitions in the data itself)

- A concurrency/data-integrity fix was merged and deployed 2026-08-26
  23:54:48 UTC (`git_commit_sha` `135925d...` and later). Before this, live
  Dashboard traffic could occasionally create duplicate rows in these
  tables. Worth checking whether `model_version`/`git_commit_sha`
  correlates with any data-quality shift.
- A prior audit found real temporal clustering in BTC's k-NN neighbor
  selection (correlated, non-independent neighbors inflating apparent
  sample size) and a feature-weighting inconsistency between the primary
  k-NN (equal weights) and conditional calibration (`{1.5, 1.0, 1.0, 0.3}`)
  — confirmed via code + data, not yet acted on in production as of the
  date above.
- If you notice a gap in new rows for a specific coin/horizon in
  STATUS.md's latest-`ts` column, that's worth flagging explicitly rather
  than assuming it's expected.

## Suggested angles for a cross-coin audit
- Accuracy/calibration comparison across BTC/LINK/ETH — consistent, or does one look structurally different?
- Whether `chosen_variant` patterns differ meaningfully by coin (e.g., does one coin's Challenger clear the gate far more/less often than the others?)
- Anything in `is_regime_anomaly` timing or `git_commit_sha` transitions that lines up with accuracy shifts
- Any coin/horizon whose latest `ts` (per STATUS.md) looks stale relative to the others
