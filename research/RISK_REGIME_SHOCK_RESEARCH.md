# Risk Regime Shock: GDELT × Hyperliquid convergence research

Research only. Candidate #1 stays **NEW_SIGNAL / Risk Regime Shock / REGIME_MODIFIER /
DATA_COLLECTION_REQUIRED**. There is no Risk Regime Shock score and there are no weights. The two components
stay separately observable: **GEO COMPONENT**, **MARKET COMPONENT**, **CONVERGENCE STATE**.

Files: `risk_regime_shock.py` (pure logic), `risk_regime_shock_run.py` (one read-only run),
`test_risk_regime_shock.py` (13 tests, FIXTURE data only), artifact `results/risk_regime_shock.json` plus
per-observation rows `results/risk_regime_shock_v1_rows.json`.

## Inputs, and what was reused unchanged

| Input | Source | Check |
|---|---|---|
| GEO COMPONENT | Frozen GDELT module (`gdelt_geo_shock`, `gdelt_research_run`): official GDELT 2.0 15-minute exports, 72h baseline, 15 min availability lag, `geo_shock_score >= 90` = elevated | All 4,122 export files re-downloaded and MD5-verified against the master list (0 mismatches). The 15-minute grid reproduces the committed `gdelt_risk_regime_shock_v1_scores.json` exactly (575/575, sha256 identical) |
| MARKET COMPONENT | Hyperliquid hourly candles from the asset-universe run's cache: BTC, `xyz:SP500`, `xyz:XYZ100`, `xyz:GOLD`, `xyz:SILVER`, `xyz:COPPER`, `xyz:BRENTOIL`, `xyz:EUR`, `para:10Y` | 27 requests, all served from cache; 1,175 hourly candles each |
| V1 | 575 observations (same timestamps as before) + 10 source readings + V1 score; 190 V1 predictions (`knn-core-v1`, 12h/24h) | Read-only `SELECT`s; every column sum checked against D1 |

## Rules (declared in code before any result was computed)

- **Abnormal**: an instrument's 1h/3h/6h return outside its own trailing 7-day 5th-95th percentile (the
  convention of both Hyperliquid studies). An instrument that has not moved or traded in 6 h is **INACTIVE** and
  never counts (`para:10Y`: 0.4 % of hours; the `xyz` markets: 0 %).
- **MARKET ELEVATED**: ≥ 3 of the 7 breadth instruments (SP500, GOLD, SILVER, COPPER, BRENTOIL, EUR, 10Y)
  abnormal on 6h. If they were independent, ≥ 3 of 7 would occur ~2.6 % of the time, close to GDELT's elevated
  rate (2.8 %). Thresholds 2 and 4 are reported as well.
- Three breadth formulations stay visible: **any direction**; **classic risk-off** (equity down, gold up,
  copper down, EUR down, 10Y yield down: textbook flight to quality, declared a priori); and **BTC-aligned**
  (directions seen alongside BTC sell-offs in the asset audit, which is **in-sample** and labelled as such).
  Equity, commodity, rates and FX stress are kept separately.
- **Weekends**: the underlying markets are treated as shut from Friday 21:00 to Sunday 22:00 UTC (summer time;
  holidays not modelled). Every reading records `underlying_open`. Market-elevated hours: 84 with the
  underlying open, **1** with it shut. The weekend Hyperliquid prices almost never drive the result.
- **Windows** 0/±1/±3/±6/±12/±24 h are all reported, in two forms: **symmetric** (looks forward, so it is
  descriptive only) and **trailing** ([t−w, t], point-in-time, usable). No window was selected.
- **Chance level**: a deterministic circular-shift null. The market series is shifted against GDELT by every
  7-hour step ≥ 48 h away from zero (123 shifts).

## Results

### 1. The two components are independent of each other and of V1

| | Value |
|---|---|
| Hours scored | 957 (27 Aug to 6 Oct 2026) |
| GEO elevated | 2.8 % of hours, 25 episodes |
| MARKET elevated | 8.9 % of hours, 41 episodes (≥ 2: 19.8 %, ≥ 4: 4.5 %). The basket is correlated, so this is well above the 2.6 % independence figure |
| Spearman geo_shock vs market breadth | +0.08 |
| Spearman geo_shock vs V1 `geopolitics` / `macrogeo` | −0.07 / +0.14 |
| Spearman market breadth vs any V1 source | \|ρ\| ≤ 0.19 (largest: `usd` −0.19, `sp500` −0.15) |

V1 `geopolitics` reads ≤ 20 in 66-83 % of observations in **every** class: it is nearly always low, so it
cannot mark geopolitical stress. Neither component duplicates a V1 source.

### 2. Convergence: none beyond chance

| | Observed | Chance |
|---|---|---|
| Hours with both elevated | **1** | 2.4 if independent |
| Classes (957 h, same hour) | A both 1 · B geo only 26 · C market only 84 · D neither 846 | |

Share of GEO-elevated hours with a MARKET-elevated hour within ±w (circular-shift null in brackets: median;
share of shifts at or above the observed value):

| Threshold | 0h | ±1h | ±3h | ±6h | ±12h | ±24h |
|---|---|---|---|---|---|---|
| ≥ 2 | 0.19 (0.19; 59 %) | 0.44 (0.33; 17 %) | 0.67 (0.48; 11 %) | 0.70 (0.63; 37 %) | 0.81 (0.78; 39 %) | 0.96 (0.93; 35 %) |
| **≥ 3** | 0.04 (0.07; 91 %) | 0.15 (0.15; 62 %) | 0.33 (0.30; 37 %) | 0.44 (0.44; 54 %) | 0.74 (0.63; 25 %) | 0.85 (0.85; 54 %) |
| ≥ 4 | 0.04 (0.04; 67 %) | 0.07 (0.07; 69 %) | 0.19 (0.15; 38 %) | 0.26 (0.22; 40 %) | 0.52 (0.37; 16 %) | 0.74 (0.59; 13 %) |

**At no threshold and no window does GDELT-market co-occurrence differ from chance** (at least 11 % of random
shifts do as well or better). Wide windows look "convergent" only because both series are elevated often enough
that almost any 48-hour span contains both.

### 3. BTC stress episodes (BTC 6h return ≤ own 7-day 5th percentile; 21 episodes)

| | Before episode (prior 24 h) | Base rate (any hour) | At start (same hour) |
|---|---|---|---|
| GEO elevated | **14 %** (3/21) | 41 % | 0/21 |
| MARKET elevated | 71 % (15/21) | 63 % | 7/21 |
| Both | 2/21 | | 0/21 |

GDELT is elevated **less** often before BTC stress than at a random hour. The market component is close to its
base rate beforehand and coincides with a third of BTC stress starts: it describes the same moment, but does not
anticipate it.

### 4. Event #15 (boundary Sun 27 Sep 12:01 UTC, event Mon 28 Sep 03:01 UTC)

| | At prediction (12:01 Sun) | During event (03:01 Mon) | First elevated in −48h..+24h |
|---|---|---|---|
| GEO | 60.2, not elevated | 48.1, not elevated | **Mon 17:01** (90.5), 14 h after the event: POST-EVENT |
| MARKET breadth | 0 of 7 abnormal; underlying markets shut | 3 (GOLD, SILVER, COPPER down; XYZ100 down): elevated | **Mon 01:01**, when the underlying markets had reopened and BTC was already falling: CONTEMPORANEOUS |
| BTC 6h | +0.51 % (72nd pct.) | −1.34 % (5th pct.) | |
| Convergence | D NEITHER at every trailing window | trailing: C MARKET ONLY; A BOTH only in the symmetric ±24h window, which looks forward to the post-event GDELT reading | **never in the same hour** |

**There was no convergence, and no component elevated, before the V1 prediction.** All ten predictions of the
Event #15 cluster (ids 1125-1134) were issued in state D NEITHER, except the last two (C MARKET ONLY), which were
issued during the fall.

### 5. V1 performance by state (descriptive; not causal)

V1 predictions (186 resolved, overall hit rate 52.7 %, Brier 0.307):

| State | Same time | Trailing 6h | Trailing 24h |
|---|---|---|---|
| A both | n = 0 | n = 4 (small) | n = 51: hit 51 %, Brier 0.32 |
| B geo only | n = 5 (small) | n = 22 (small): hit 50 % | n = 21 (small): hit 62 %, Brier 0.22 |
| C market only | n = 10 (small): hit 60 % | n = 38: hit 63 %, Brier 0.26 | n = 70: hit 64 %, Brier 0.23 |
| D neither | n = 171: hit 52 %, Brier 0.30 | n = 122: hit 50 % | n = 44: hit 32 %, Brier 0.46 |

The V1 score direction against BTC's next 24 h (547 usable observations) shows the same shape, e.g. trailing 24h
D NEITHER hit 26 % (n = 132). **This is not a usable finding.** The observations fall on few days (the
trailing-24h D NEITHER group sits on 19 days, the largest being 27 Sep, the Event #15 day, with 17), V1
observations and predictions overlap in time, and the pattern would make "no stress" the worst regime, which no
hypothesis predicted. It is reported, not interpreted.

### 6. Incremental value beyond V1

- **V1 has no joint cross-asset representation.** It scores gold, oil, usd, nasdaq, sp500 and yield10y one by
  one (FRED daily for equities and rates), and only `gold` looks at another asset. The market breadth state is
  uncorrelated with every V1 source (|ρ| ≤ 0.19), so it is information V1 does not hold.
- **GDELT is uncorrelated with V1's `geopolitics` and `macrogeo`** (−0.07, +0.14), so it is also information
  V1 does not hold.
- **But the *convergence* of the two identifies nothing.** It occurred in 1 hour of 957, which is chance level.
  The framework therefore does not identify a regime V1 misses; at most the market component alone describes
  concurrent cross-asset stress.

### 7. Open interest

**FUTURE LEVERAGE COMPONENT: PROSPECTIVE COLLECTION REQUIRED.** No free history; not used.

## Limitations

- 40 days, 957 hours, 21 BTC stress episodes, one event of interest. A weak convergence effect would not be
  detectable; a strong one would have shown.
- Hyperliquid instruments are deployer-oracle synthetic perps. Weekend prices are Hyperliquid-internal (but
  they produced only 1 of 85 market-elevated hours).
- The market-hours rule ignores holidays (e.g. US Labor Day, 7 Sep) and the daily maintenance break.
- `para:10Y` is thin ($0.24 M/day).
- V1 readings and predictions are clustered in time; performance splits are descriptive only.

## Final verdict

1. **Does GDELT provide a useful independent geopolitical component?** Independent: yes (uncorrelated with V1
   and with the market). Useful for BTC stress: **not shown**. It was elevated before BTC stress less often than
   at random, and only after Event #15.
2. **Does Hyperliquid provide a useful independent market-regime component?** Independent: yes. Useful:
   **only as a contemporaneous description.** It coincides with a third of BTC stress starts, but sits near its
   base rate beforehand and was contemporaneous at Event #15.
3. **Do the two components converge meaningfully?** **No.** 1 shared hour vs 2.4 expected; every window and
   threshold is within the circular-shift null.
4. **Does convergence occur early enough to be useful?** **No.** It does not occur beyond chance at all, and
   at Event #15 neither component was elevated at the prediction.
5. **Does the combined framework add information beyond V1?** Each component holds information V1 does not.
   The **convergence state does not add anything** demonstrable.
6. **Is Event #15 explained better by the combined framework?** **No.** It was D NEITHER at the prediction;
   the market moved with BTC and GDELT moved 14 h after.
7. **Is there enough evidence to proceed to formal Risk Regime Shock methodology design?** **No.**

**Verdict: INSUFFICIENT DATA — MORE RESEARCH REQUIRED.**

This is not an approval with caveats. On the data available, the convergence hypothesis is **not supported**.
Formal design should not start. The one test that could change this is cheap and stays within the rules: rerun
this experiment unchanged (same declared rules, no re-tuning) on a longer history, using Hyperliquid BTC
stress episodes rather than V1 observations. The hourly macro candles reach back to about March 2026 (10Y only
to 12 Aug) and GDELT is free throughout. That gives ~7 months and several times more BTC stress episodes. If
convergence is still at chance level there, the convergence design should be rejected and the market component
considered on its own. Candidate #1 is not activated.

## Running it

```
python3 research/risk_regime_shock_run.py --v1 <v1_full.json> --predictions <predictions.json> \
  --gdelt-cache <GDELT export cache> --hl-cache <asset-universe cache> \
  --out research/results/risk_regime_shock.json
```

The V1 extracts are read-only `SELECT`s (not committed; their sha256 are recorded in the artifact). The GDELT
cache is filled by `gdelt_research_run.py --scope history --max-files 5000`; the computed 15-minute grid is
cached so reruns are offline. The Hyperliquid cache is the asset-universe run's cache.
