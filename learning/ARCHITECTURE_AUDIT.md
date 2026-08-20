# CryptoPulseV2 — Architecture Audit (Phase 1)

Date: 2026-08-18
Scope: PulseWorkerV2 (`worker.js`, D1 database `sentiment-history`) and CryptoPulseV2 frontend.
Status: Audit only. No production behavior was changed.

---

## 1. Existing Tables (live D1 schema, `sentiment-history`)

Confirmed directly against the production database (not inferred from code):

| Table | Rows (n / resolved) | Role |
|---|---|---|
| `predictions` | 645 / 608 resolved | BTC core k-NN model, both horizons (12h/24h) |
| `link_predictions` | 548 / 506 resolved | LINK core k-NN model |
| `eth_predictions` | 34 / 18 resolved | ETH core k-NN model (self-contained features) |
| `challenger_predictions` | 912 / 840 resolved | Regime-conditional trend/reversion challenger (BTC + LINK) |
| `calibration_curve` | decile rows per coin/horizon | Core-model calibration (additive decile correction) |
| `challenger_calibration_curve` | decile rows | Challenger's own calibration |
| `selection_decisions` | one row per selection run | Dynamic variant selection (LCA-scored) audit trail |
| `backtest_results`, `regime_split_results` | — | Offline backtest history |
| `coin_catalyst_log` | 2 rows | Existing but effectively unused catalyst-matching attempt |
| `gemini_daily_analysis`, `link_gemini_analysis` | — | Gemini narrative + resolved-outcome tracking |
| `history`, `btc_data`, `link_data`, `eth_data` | — | Raw price/sentiment/technical time series |
| `foufi_digest`, `whale_snapshots`, `portfolio_snapshots`, `alert_configs`, `alert_log`, `txs_backup` | — | Shared with V1, out of scope here |

This table list is the ground truth going forward — `DATA_CONTRACT.md`'s "Historical Compatibility" list is accurate and complete except it omits `challenger_predictions` and `challenger_calibration_curve`, which also need explicit preservation.

---

## 2. Existing Prediction Flow

Three independent core models, same k-NN pattern (`runPrediction` / `runLinkPrediction` / `runEthPrediction`):

1. Load asset price series + engineered features (z-score normalized).
2. Exclude candidates too recent to have a resolved forward outcome (`ts <= today.ts - (lagMs + tolMs)`) — the fix noted in memory is present and correct in all three functions.
3. K-nearest-neighbor search (`K = min(15, max(5, candidates/3))`), distance-weighted outcome aggregation.
4. Regime-anomaly flag: closest-distance percentile ≥ 0.9 vs. historical closest-distances.
5. Adaptive-K / distance-weighted **experimental** variant computed in parallel, logged but not authoritative (`p_up_experimental`).
6. Two calibration layers applied at creation time only, both computed from strictly-prior data (`ts < today.ts`):
   - Decile-bucket additive calibration (`calibrated_p_up`)
   - Conditional (feature-neighborhood) calibration (`calibrated_conditional_p_up`)
7. Single `INSERT` writes the full row, including features (`features_json`), both probabilities, k used, volatility percentile, trend strength, regime-anomaly flag.

Challenger (`runChallengerPrediction`) is architecturally separate: regime-conditional trend/reversion, defers to trend-persistence when the core model's anomaly tripwire fires. Reads the core model's `is_regime_anomaly` for the same cycle rather than recomputing it — correct, avoids definition drift.

Selection layer (`selectBestVariant`) is a real, working prototype of "dynamic classifier selection": scores each eligible variant's local accuracy (LCA) against a meta-neighborhood of similar historical moments, requires 50+ resolved predictions per variant and a significance-gated margin over 50% before it will choose anything other than `'original'`. This is materially the same idea proposed in the on-the-horizon Dynamic Classifier Selection brainstorm — it already exists, just not documented as such, and not yet exposed to ChatGPT for audit.

---

## 3. Existing Outcome Resolution

`backfillPredictions` / `backfillLinkPredictions` / `backfillEthPredictions`: `UPDATE ... SET realized_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?`.

Confirmed: this update touches **only** the four outcome columns. Original features, probabilities, and calibration values are never rewritten after insert — the immutability principle in `DATA_CONTRACT.md` is already respected in practice, even though outcome data is *not* physically in a separate table as the contract specifies. That's a real gap (see §6) but not a correctness bug.

---

## 4. Existing Calibration

- `refreshCalibrationCurve` (daily cron, 07:00 UTC): rebuilds `calibration_curve` from all resolved rows at time of refresh, decile bucketed. Appends new rows rather than overwriting predictions — safe.
- `computeConditionalCalibration`: k-NN over feature space at prediction time, strictly historical (`ts < today.ts`), independently unit-tested (`conditional-calibration.test.js`).
- Challenger has its own mirrored calibration curve and refresh path — kept fully separate from the core model's, correctly.

---

## 5. Existing Challenger Flow

Confirmed live: 912 challenger predictions, 840 resolved, running in parallel with the core BTC/LINK models exactly as `AI_COLLABORATION.md`'s Production/Challenger/Research model describes. ETH deliberately has no challenger yet (correct per the code comment — no core-model track record to extend from yet).

---

## 6. Missing Components (relative to `.ai/` contract)

- **No `model_version` or `git_commit_sha` on any prediction row.** Nothing in the schema identifies which code version produced a given prediction. This is the single largest gap against `DATA_CONTRACT.md` and blocks reproducibility (`"what did the model know at the exact moment"` is answerable for *features*, not for *code version*).
- **Outcome data lives in the same row as prediction data**, not a separate table as `DATA_CONTRACT.md` specifies. Low risk in practice (see §3) but worth a real decision: keep the current shape (simpler, already immutable in practice) or migrate — recommend keeping current shape and documenting the deviation rather than a risky migration, unless ChatGPT's audit needs point otherwise.
- **No daily learning report.** `/api/learning/daily` and `/api/learning/chatgpt` don't exist. No read-only, AI-facing summary endpoint of any kind currently exists on PulseWorkerV2.
- **No market catalyst infrastructure matching the contract.** `coin_catalyst_log` exists but has 2 rows, no `discovery_timestamp`, no `available_before_prediction`, no category enum matching `MARKET_CATALYST.md`. Effectively needs to be rebuilt, not extended.
- **No experiment registry.** No `/learning/experiments/`, `/learning/claude_tasks/`, `/learning/results/` directories exist in either repo.
- **No leakage-audit documentation.** The leakage-relevant logic (candidate cutoff, strictly-prior calibration history) is correct in code but has never been written up as a standalone audit artifact.
- **Selection layer is undocumented as what it is.** It's a working Dynamic Classifier Selection implementation but isn't referenced anywhere in `.ai/` or in the "on the horizon" list — worth reconciling so the contract doesn't propose rebuilding something that already exists.

## 7. Test Coverage

`tests/` covers `adaptive-calibration`, `conditional-calibration`, `eth-model`, `selection-layer` (389 lines total) — all pure-function unit tests per the existing "extract pure logic for testability" pattern. **No tests exist yet** for: outcome resolution, daily metrics calculations, catalyst timestamp integrity, or API output shape — all required by `EXPERIMENT_PROTOCOL.md` Phase 9.

## 8. Potential Leakage Risks — Preliminary Read

No leakage found in the core prediction paths reviewed:
- Candidate/neighbor cutoff correctly excludes unresolvable-too-recent points in all three core models.
- Both calibration layers query `ts < today.ts` only.
- Challenger reuses the same-cycle anomaly flag rather than an independent, potentially-inconsistent recomputation.

Not yet reviewed in this pass (flag for Phase 8's dedicated leakage audit): `selectBestVariant`'s meta-neighborhood window (`ts < latestCore.ts`, looks correct on read but wasn't traced end-to-end), and whether `refreshCalibrationCurve`'s daily rebuild could, in a narrow timing edge case, let a same-day resolved outcome influence a same-day later prediction's decile bucket before midnight UTC rollover.

---

## Conclusion

The existing system is materially closer to the target architecture than the `.ai/` contract assumes — three working core models, a working challenger, a working (if underdocumented) dynamic selection layer, and calibration that already respects temporal ordering. The real gap is entirely in the **learning/reporting/versioning layer**: no immutable version tagging, no daily report, no catalyst infrastructure, no experiment registry, no leakage documentation. Phase 2 onward should build additively on what exists rather than re-architecting the prediction paths.
