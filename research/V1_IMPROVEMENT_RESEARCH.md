# Accelerating V1 improvement: ranked findings and plan (2026-10-09)

Research only. No V1 source, weight, score or methodology change; no activation; Candidate #1, the frozen data and the
OI pre-registration (`46c0d52b…`) are untouched; nothing merged or deployed.

**Status of evidence.**
- **Tracks A and B are EXPLORATORY.** They are computed on the 575 frozen V1 observations (27 Aug – 6 Oct; 41 UTC
  days), the same window the earlier Risk Regime work studied. They suggest hypotheses and validate nothing.
- **Uncertainty.** Units are one call per UTC day (41), with 95 % day-block bootstrap intervals.
- **Code and outputs.** `v1_failure_analysis.py` writes `results/v1_failure_analysis.json`. The V1 archive extract
  (read-only, 605 rows, sha256 `51e8d9e5…`) matches the frozen data exactly. Its 30 later rows were deliberately not
  analysed.

## Ranked: the three most promising ways to improve V1

### 1. Make candidate validation honest (holdout) and faithful to V1's actual calls
**Why first.** Every other improvement depends on it, and it removes a real integrity gap in the product.
- **No holdout.** `analyse()` validated candidates on the same V1 history the researcher studied; only ±24 h around
  the event was excluded. So a candidate inspired by a pattern in this window could be marked SUPPORTED by that same
  pattern.
- **Reconstructed baseline.** The baseline is a reconstructed V1. Only 240 of 575 stored scores are reproduced
  exactly by the published defaults (mean gap 0.74), and the reconstructed call differs from the stored call 26 times
  on 13 days.

**Implemented (research branch, not merged).** `validateRecalculation(…, { discoveredAt })`:
- The verdict uses only observations made after the candidate was created.
- In-sample results appear separately as `exploratory_in_sample`.
- A new status `AWAITING_HOLDOUT` applies until enough independent holdout days exist.
- The stored V1 call is reported next to the reconstructed baseline, with the disagreement count.
- `analyse()` passes `candidate.created_ts`.
- Without `discoveredAt`, output is unchanged.
- Approval already requires `SUPPORTED` or an explicit acknowledgement, so the new status only makes approval
  stricter.

### 2. Test removing or reclassifying low-information inputs that carry large weight (pre-registered; holdout only)
**Exploratory evidence (41 days).**
- `fng` (weight × confidence 14, the second largest) is contrarian-looking: ρ with the forward 24 h return −0.33
  (95 % CI −0.58 to +0.01). Its lean hit rate is 49 % on 39 days.
- `etfflows` (8) is saturated: 77 % of readings are ≤ 10 or ≥ 90, in runs of about 17 observations. Forward ρ −0.20
  (CI −0.52 to +0.12).
- The FRED inputs `sp500`, `nasdaq` and `yield10y` are unchanged in about 97 % of weekend observations (daily series),
  with forward ρ ≈ 0. Their 24/7 Hyperliquid equivalents carry the same information (S&P reading vs Hyperliquid 24 h
  change ρ 0.65), not more (forward ρ −0.08 to 0.03).
- `funding` (weight 15) is exactly 50 in 68 % of readings and `hypefunding` in 75 % (the Hyperliquid funding floor
  maps to 50). Treating the floor as missing changes only 1 of 575 calls: a clarity issue, not an accuracy lever.

**How fast it can be tested honestly** (counted without outcomes): inverting `fng` changes V1's call on 29 of 41 days,
removing `etfflows` on 31 of 41, removing the FRED trio on 13 of 41.

**Registered now** in `v1_candidate_registration.json` (sha256 `66067b32…`, registered 2026-10-09 16:00 UTC):
- T-A1: invert `fng`;
- T-A2: remove `etfflows`;
- T-A3: remove the FRED trio (research replay).

They are evaluated only on V1 observations after registration, under the product's existing rule (≥ 5 independent
disagreement days, one-sided binomial). A research claim of support needs p ≤ 0.1/3. At current V1 cadence the first
verdicts are possible in **about 1–3 weeks**, instead of months.

### 3. Investigate V1's lag: its sources mostly restate the past 24 h move
**Exploratory evidence.**
- Many V1 inputs correlate with BTC's *past* 24 h return but not its *forward* return:
  - `gold` past 0.76 / forward −0.19;
  - `sp500` 0.54 / 0.06;
  - `ninemag` 0.49 / −0.17;
  - `longshort` 0.42 / −0.13.
  BTC's own 24 h change is past 0.92 / forward −0.22.
- Calls that agree with the past 24 h move failed 64 % of the time (observation level; 55 % at day level, CI 40–70 %),
  against 41 % (46 %, CI 29–63 %) for calls against it.
- DOWN calls in a 7-day downtrend failed on 14 of 17 days (82 %, CI 65–100 %); UP calls in a downtrend failed on 4 of
  16 (25 %).
- V1's own conviction carries no signal: failure does not vary with the score's distance from 50 or with source
  agreement.

**Limits.** These subgroups were found by looking, on overlapping intervals. They are the leading *mechanism*
hypothesis (V1 behaves as a lagging momentum read at a horizon where BTC partly mean-reverts). They are not yet a
candidate. The next step is to register one specific lag-aware candidate (for example via the existing
`ADD_REGIME_CONDITION` on `btc_24h_change_pct`) and test it only on holdout data.

**Not ranked, but worth noting.** Day-level failure for weekend-boundary calls is 17 % (12 days, CI 0–42 %) against
62 % on weekdays (29 days). It is large but rests on few days, and the observation-level difference is small (49 % vs
56 %). Exploratory only.

## Track B: missing-source dimensions (verified status)

| Dimension | Verified source / route | Cost / restriction | History and resolution | V1 overlap | Exploratory forward ρ (41 days) | Advance warning or faster detection |
|---|---|---|---|---|---|---|
| Liquidations | none at €0 (Bybit/Binance live websockets only; Xoomar unidentified) | paid aggregators not used | — | none | — | unknown; not testable |
| Leverage: OI | Binance public archive (checksum-verified), forward collection running; Bybit refused (403) | €0; Binance REST 451 | 5-min, 2026-01 onward | none | OI 24 h −0.17 (CI −0.47 to 0.17) | no pre-event build-up at Event #15; pre-registered test pending |
| Leverage: positioning ratios | same archive files (top-trader, account and taker ratios), already stored raw | €0 | 5-min | account ratio overlaps V1 (ρ −0.51) | account 0.23, top-trader 0.18, taker −0.18 (all CIs span 0); account ratio is mostly backward-looking (past ρ −0.63) | faster detection at best |
| Cross-asset breadth | Hyperliquid 24/7 perps | €0 | 1 h, ~208-day rolling | same instruments as several V1 sources | −0.08 to +0.19 | faster detection (it removes FRED weekend staleness) |
| Stablecoin liquidity | DeFiLlama | €0 | daily | none directly | USDT −0.28 (CI −0.56 to 0.03), USDC −0.06 | weak; daily resolution limits warning |
| Volatility | Deribit DVOL | €0 | 1 h | none | level −0.26 (CI −0.51 to 0.05) | weak; it rose contemporaneously at Event #15 |
| News / event intensity | GDELT 2.0 events and DOC | €0 (DOC rate-limited) | 15 min (events); DOC only in event windows | V1 headline sentiment differs (ρ −0.07) | geo_shock −0.25 (CI −0.54 to 0.10), events 24 h −0.12 | weak; confounded by weekday cycle |

No research dimension's forward correlation is distinguishable from zero on 41 days. Several (DVOL, USDT, geo_shock)
are worth carrying into a registered holdout test once enough forward data exists.

## What can be done now, and what needs data

| Immediately (existing data and code) | Needs new data |
|---|---|
| Holdout validation and stored-baseline reporting (implemented, needs merge approval) | Bybit OI / funding (permitted location) |
| T-A1 to T-A3 evaluated on post-registration V1 observations (first verdicts in ~1–3 weeks) | Liquidations (no €0 source) |
| One registered lag-aware candidate (mechanism 3) | Forward V1 calls for the OI pre-registration (~5–6 months) |
| Positioning ratios from the existing archive into forward collection (raw already stored) | Any longer V1 history: none exists before 27 Aug 2026 |

## Smallest next implementation with user value

**Merge and deploy the holdout validation** (four files: `learning-method.js`, `learning-candidates.js`,
`learning-ui.js` and tests).
- **Effect for users:** every Learning candidate's verdict becomes out-of-sample, and users see V1's real stored call
  next to the reconstruction.
- **Deployment note:** `deploy.yml` does not watch `learning/`, so a merge alone does not deploy. A Worker deploy is a
  separate, explicit step.
- **Needs your authorisation** for both the merge and the deploy.

## Proposed sequence (research and product in parallel)

1. **Now:**
   - approve the merge, then the deploy, of holdout validation;
   - keep the OI schedule running unchanged (first scheduled run 2026-10-10 10:40 UTC, verified separately);
   - T-A1 to T-A3 start accumulating holdout days from 2026-10-09 16:00 UTC.
2. **Week 1–2:** register one lag-aware regime candidate before looking at any new data. Add a read-only weekly
   extract of new V1 observations so the replay can run.
3. **Week 2–4:**
   - first holdout verdicts for T-A1 and T-A2 (each changes calls on ~3 of 4 days);
   - T-A3 later;
   - any SUPPORTED result goes to a human approval as an APPROVED (not active) methodology version, never auto-activated.
4. **Ongoing:**
   - the OI pre-registration accrues until its minimum (≈ March 2027);
   - Bybit and liquidations stay missing until a permitted route exists;
   - Risk Regime Shock is not designed until a registered test supports a mechanism.
