# August 2026 Learning Roadmap
## For Claude (Builder) and ChatGPT (Auditor)

**Generated:** 2026-08-27  
**Scope:** Analysis of CryptoPulse / PulseWorkerV2 predictions vs realised market history during the mid-to-late August 2026 bull acceleration (ETH ~$1.9k → ~$2.5k, BTC ~$63k → ~$81k peak, LINK ~$9 → ~$12).  
**Primary assets:** BTC, ETH, LINK  
**Horizons:** 12h / 24h  
**Models in scope:** Original k-NN, Experimental (adaptive-K + distance-weighted), Calibrated, Challenger (flat / tilted / calibrated), Condition-Matched Selection (LCA + Bonferroni gate)

This document is the single source of truth for the next experiment cycle.  
- **Claude** implements only what is specified here, as parallel logged variants or pure research endpoints — never silent production changes.  
- **ChatGPT** audits implementation against this spec, the existing `.ai/` contracts, and the "log but don't replace" discipline.

---

## 1. What We Learned

### 1.1 Regime-anomaly detection worked
`is_regime_anomaly = 1` and elevated `closest_analog_dist` percentiles correctly flagged that the August acceleration had no close historical analogues. The tripwire did its job: it signalled that the k-NN was operating on weaker matches than usual.

### 1.2 Pure analog matching systematically under-states strong novel trends
k-NN is mean-reverting by construction. When the market produces a historically uncommon multi-day expansion, the nearest neighbours tend to be milder prior episodes. Both directional probability and magnitude estimates are pulled toward the historical average. This is consistent with earlier internal findings (anomaly windows showing ~22.9 % directional accuracy for the core model in one audited sample).

### 1.3 LCA / Bonferroni selection is deliberately conservative
Most cycles defaulted to `original`. That is the intended safety property (multiple-testing correction prevents chasing noise). The cost is that experimental / calibrated / challenger variants rarely clear the significance bar during high-novelty stretches, even when they may have been locally better.

### 1.4 Challenger already encodes the right intuition
- Flat variant (shrink confidence toward 0.5 on anomaly) is the honest response when no good analogue exists.  
- Later calibration showed that dips inside anomalies frequently resolved upward in the available data.  
The August rally is another independent episode of the same phenomenon. More independent episodes are required before any promotion decision.

### 1.5 ETH remains data-starved relative to BTC / LINK
Self-contained feature set + shorter history → smaller effective neighbourhoods and more frequent fallbacks. Cross-asset borrowing (as LINK does for regime / sentiment) is a deliberate trade-off that ETH avoided; the thinner selection history is the visible consequence.

### 1.6 Cross-asset failure correlation is real and useful
Simultaneous misses on BTC and ETH while LINK (or another asset) diverged is exactly the signal the investigation / Analyst Relay path is built to surface. Market-wide regime shifts dominate asset-specific ones in these windows.

---

## 2. Design Constraints (Non-Negotiable)

1. **Log but do not replace.** Every new idea is a parallel variant or a read-only research endpoint. Production selection path and core k-NN logic stay unchanged until an explicit, audited promotion decision.  
2. **Episode-level thinking.** Consecutive anomaly flags inside the same market move are not independent observations. Score and report by episode where possible.  
3. **No silent threshold changes.** Any change to LCA margins, anomaly shrink factors, or feature weights must be versioned and logged.  
4. **Existing contracts remain authoritative.** `.ai/DATA_CONTRACT.md`, `.ai/DAILY_AUDIT.md`, `.ai/ARCHITECTURE.md`, and the fencing-token / read-only live-ingestion rules are not overridden by this roadmap.  
5. **Claude implements; ChatGPT audits.** Implementation PRs must include the checklist in §5 so the auditor can verify compliance.

---

## 3. Experiment Roadmap (Ordered by Leverage / Cost)

### Experiment 1 — Anomaly-conditioned performance audit (pure analysis, highest priority)
**Owner:** Claude (research endpoint) · **Auditor:** ChatGPT  

**Goal:** Quantify whether the August-style acceleration is a repeatable failure mode and whether "buy-the-dip inside anomaly" generalises.

**Work:**
- Extend the existing `/research/regime-directional` report (or add a sibling endpoint).  
- Split anomaly rows by:
  - `trend_strength` sign, **and**
  - trailing-return sign (dip vs continuation inside the anomaly).  
- Report for each bucket: n, directional accuracy, Brier, always-up baseline, always-down baseline.  
- Count **episodes** (consecutive days sharing the same anomaly + trend bucket), not raw prediction rows.  
- Scope: BTC, LINK, ETH; 12 h and 24 h.

**Success criteria for the experiment itself:** Endpoint is read-only, deterministic, and produces comparable numbers to the earlier ad-hoc audit. No production behaviour change.

**Promotion gate:** None — research only. Results feed Experiment 2–3 design.

---

### Experiment 2 — Softened / adaptive selection gate under high anomaly
**Owner:** Claude (parallel selection path) · **Auditor:** ChatGPT  

**Goal:** Test whether the Bonferroni bar is overly strict precisely when the core model is known to be weakest.

**Work:**
- Keep the existing `selectBestVariant` path unchanged.  
- Add a **logged-only** alternative decision path that, when `is_regime_anomaly = 1`:
  - either lowers the required margin, **or**
  - uses "best LCA wins if `n_matched ≥ N`" (N documented).  
- Write the alternative choice into a new column or a parallel `selection_decisions_anomaly` table (or equivalent) so it can be scored later without affecting live display.  
- Do **not** change the production `chosen_variant` until out-of-sample episode count is larger.

**Success criteria:** Alternative decisions are fully reconstructible from the log; production path is byte-identical to current behaviour.

**Promotion gate:** Requires ≥ 3 additional independent high-anomaly episodes after this change, plus ChatGPT audit of the comparison.

---

### Experiment 3 — Explicit momentum / persistence overlay for anomalous + strong-trend states
**Owner:** Claude (new Challenger-style variant) · **Auditor:** ChatGPT  

**Goal:** Complement the existing confidence-shrink with a small, transparent trend-persistence component when both anomaly and strong trend are present.

**Work:**
- New variant (suggested name: `challenger_momentum` or similar).  
- When `is_regime_anomaly = 1` **and** `|trend_strength| > documented threshold`, blend a pure trend-persistence (or short-horizon momentum) signal with the core k-NN `p_up` using a small fixed weight (0.15–0.25, documented).  
- Otherwise fall back to existing flat / tilted behaviour.  
- Log in parallel exactly as other Challenger variants are logged.  
- Existing flat / tilted / calibrated Challenger variants remain untouched.

**Success criteria:** New variant appears in calibration history and selection eligibility only after its own 50+ resolved bar; never silently replaces any production number.

**Promotion gate:** Same episode-count and audit requirements as Experiment 2.

---

### Experiment 4 — Feature-weight and distance diagnostics on the August window
**Owner:** Claude (research + optional conditional-calibration tweak) · **Auditor:** ChatGPT  

**Goal:** Re-run the discriminative-power audit restricted to the August acceleration days and test whether low-signal features (e.g. `bottom_score` in this regime) are diluting neighbour quality.

**Work:**
- Reproduce the existing evidence-based weight analysis (`regime_mag` strongest, `bottom_score` near-zero in the prior window) **restricted to the August high-anomaly period**.  
- Optionally log a parallel conditional-calibration run with temporarily zeroed or heavily down-weighted low-signal features.  
- Do not change the live `CONDITIONAL_CALIB_WEIGHTS` until audited.

**Success criteria:** Report is reproducible; any parallel calibration is clearly tagged and does not affect production `calibrated_conditional_p_up`.

---

### Experiment 5 — Cross-asset feature for ETH (optional for LINK)
**Owner:** Claude · **Auditor:** ChatGPT  

**Goal:** Test whether a BTC-lead feature improves ETH's ability to capture market-wide moves without abandoning the self-contained design principle.

**Work:**
- Add an optional extra dimension (BTC recent return or BTC `regime_mag`, lagged) to ETH's feature vector.  
- Log in parallel only (new experimental column or separate model tag).  
- Keep the existing self-contained ETH path as the production path.

**Success criteria:** Parallel predictions are fully isolated; ETH production path remains unchanged.

---

### Experiment 6 — Horizon and calibration stratification
**Owner:** Claude (reporting) · **Auditor:** ChatGPT  

**Goal:** Confirm whether existing decile and conditional calibration remain helpful, or are being pulled by opposite populations (the exact problem conditional calibration was introduced to solve).

**Work:**
- Compute accuracy / Brier **only on anomaly rows** vs normal rows, for both 12 h and 24 h, per coin.  
- Compare raw, calibrated, and conditional-calibrated Brier on the same stratified sets.  
- Surface results in the daily learning report or a dedicated research endpoint.

**Success criteria:** Numbers are stratified, not blended; insufficient-sample cells are explicitly marked.

---

### Experiment 7 — Episode-level evaluation metric
**Owner:** Claude · **Auditor:** ChatGPT  

**Goal:** Prevent inflated statistical significance from autocorrelated anomaly flags.

**Work:**
- Formalise an "episode" definition: consecutive cycles sharing the same anomaly + trend bucket.  
- Score models on episode outcomes (direction of the whole move, max adverse excursion, etc.).  
- Integrate into the regime-directional research report and, later, into daily learning summaries.

**Success criteria:** Episode counts, not raw prediction counts, are the primary significance unit in all regime-related reporting.

---

## 4. Practical Sequence (Low-Risk Order)

1. **Experiment 1** (anomaly-conditioned audit) — pure analysis, no production risk.  
2. Feed results into the design of Experiments 2 and 3.  
3. Implement Experiments 2–4 as **parallel logged variants / research endpoints only**.  
4. Let the daily learning engine + Analyst Relay continue to accumulate catalyst labels around future anomaly episodes.  
5. Re-evaluate after **2–3 more independent high-anomaly episodes** (not merely more rows inside the same episode).  
6. Only then consider any promotion proposal, subject to full ChatGPT audit against this document and the existing contracts.

---

## 5. Implementation & Audit Checklist

### For Claude (Builder)
- [ ] Every new variant is logged in parallel; production `chosen_variant` and core k-NN path are unchanged unless this document explicitly authorises a promotion.  
- [ ] New endpoints or columns are documented in the PR description and, where appropriate, in `.ai/` contracts.  
- [ ] Episode definition (if used) is identical across all new reports.  
- [ ] No change to fencing-token / read-only live-ingestion logic.  
- [ ] No change to LCA critical-Z table or `SELECTION_MIN_*` constants without an explicit, versioned experiment.  
- [ ] Tests cover the new paths (including the "no steal → 1 row / steal → 0 rows" style concurrency tests where writes are involved).  
- [ ] PR description links back to this roadmap section numbers.

### For ChatGPT (Auditor)
- [ ] Confirm the PR implements only the experiments listed above (or a clear subset).  
- [ ] Confirm production selection and core prediction logic are byte-identical to pre-PR behaviour for non-experiment paths.  
- [ ] Confirm any new thresholds, weights, or shrink factors are documented and versioned.  
- [ ] Confirm episode-level (not row-level) thinking is used wherever regime performance is claimed.  
- [ ] Confirm insufficient-sample cells are explicitly reported, never filled with fabricated conclusions.  
- [ ] Flag any drift from `.ai/DATA_CONTRACT.md`, `.ai/DAILY_AUDIT.md`, or the fencing-token design.  
- [ ] Require evidence of ≥ 2–3 independent high-anomaly episodes before any promotion recommendation.

---

## 6. Out of Scope (Do Not Implement Under This Roadmap)

- Replacing the core k-NN with a different model class.  
- Feeding Analyst Relay / catalyst data into the prediction feature vector.  
- Raising Gemini investigation quotas or re-enabling automated grounded search.  
- Changing the 3-hour cron cadence or the read-only live-ingestion rules.  
- Promoting any experimental or Challenger variant to production without a separate, audited promotion PR that references this document.

---

## 7. References (Internal)

- PulseWorkerV2 `worker.js` — k-NN core, Challenger, LCA selection, regime anomaly tripwire, conditional calibration.  
- Existing research endpoint: `/research/regime-directional`.  
- `.ai/ARCHITECTURE.md`, `.ai/DATA_CONTRACT.md`, `.ai/DAILY_AUDIT.md`.  
- Prior internal finding: anomaly-window accuracy ~22.9 % vs high trend-continuation baseline in one audited sample; later "dip-inside-anomaly often resolves up" calibration note.  
- Data exports under `data-exports/` (predictions + selection_decisions) used for the August 2026 analysis.

---

*End of roadmap. Claude builds against §3 and §5; ChatGPT audits against the same sections and the non-negotiable constraints in §2.*
