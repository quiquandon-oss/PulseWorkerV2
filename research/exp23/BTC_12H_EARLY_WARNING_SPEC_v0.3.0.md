# BTC 12-hour early-warning research specification

| Field | Value |
|---|---|
| Spec id | `btc-12h-early-warning` |
| Version | **0.3.0** (2026-10-10) |
| Supersedes | 0.2.0, sha256 `0c0bc154bddb8a3bd32f90624158a7a48136e49386aece508e2020094b1fdff1` (`BTC_12H_EARLY_WARNING_SPEC.md`, unchanged) |
| Earlier version | 0.1.0, sha256 `2fd3777e…88a5` |
| Design decisions | D1–D6 **APPROVED** by the user on 2026-10-10 |
| Hash registration | **Not registered**. Registration requires separate user approval of the sidecar hash (§13) |
| Mode | Specification only |
| Location | Session scratchpad `…/scratchpad/exp23/`, outside every repository working tree |

**Scope of this document.** It is a specification only:
- no detector, model, label builder, feature builder, scorer, feature table or label file exists;
- no performance metric has been computed.

**Labels used in this document**

| Label | Meaning |
|---|---|
| VERIFIED | Checked against an identified file, commit or local command output |
| APPROVED | User decision |
| PROPOSED | Awaiting approval |
| BLOCKED | Cannot proceed without access, data or a decision |
| NOT COMPUTED | Deliberately not calculated |

## 0. Approved decisions [APPROVED]

| # | Decision | Applied in |
|---|---|---|
| D1 | Market-moves v1.0.0 is preserved exactly: UP if R24 > +3%, DOWN if R24 < −3%, both strict. A move of exactly ±3% does not qualify | §2.1 |
| D2 | The primary event reference is the v1.0.0 **estimated onset**. It is a proxy, not the true economic onset. The first threshold crossing is reported separately as a secondary measure. Every warning also reports how far price had already moved when it fired | §2.2, §3, §6 |
| D3 | Minimum lead time is 12 h; **exactly 12 h qualifies**. The qualifying window runs from 36 h to 12 h before the reference (W = 24 h). Target warning rate q = 5%, with sensitivity checks at 2.5% and 10%. There is no 6-hour horizon | §2.3, §5 |
| D4 | Pre-seal price observations may be used as lookback features only. They never become evaluation labels or events. Point-in-time availability is enforced. Sealed-period outcomes are never used for feature selection, tuning or model comparison | §2.4, §4.2, §8, §9 |
| D5 | **60 qualifying forward events, including at least 25 per direction**, is a preliminary minimum checkpoint, not proof of performance. Baseline comparisons and uncertainty reporting are required | §6, §10 |
| **D6** (new in 0.3.0) | **Sealed-period PRICE observations** may be used as **lookback-only history** for features at eligible post-seal decision times. This does **not** authorise sealed-period outcomes, labels, events, target returns or performance results. Non-price sealed-period observations (open interest, funding) are not covered by D6 | §2.4, §4.2, §8, §9, §10 |

---

## 1. Research objective and scope

**Objective** [APPROVED]: determine whether information available at decision time t can issue a useful warning **at least 12 hours before** a qualifying large BTC move.

The study keeps two questions separate. A result for one is never reported as evidence for the other.
- **A. Detection:** is a large move already under way? This is covered by the existing market-moves plan v1.0.0, which this spec leaves unchanged. [VERIFIED]
- **B. 12-hour advance warning:** this specification.

**In scope.** BTC only, on the Hyperliquid BTC perpetual, using hourly candles, with the v1.0.0 event definition and local files only.

**Out of scope.** ETH and LINK; detection scoring; live alerting; any production use. HIP-3 non-crypto markets are also out of scope; they are not BTC proxies.

## 2. Definitions

### 2.1 Event: market-moves v1.0.0, reused exactly [VERIFIED, APPROVED D1]

**Source.** `research/market_moves/definition.json` v1.0.0 on `claude/epic-planck-uyapsw-market-moves` @196b87e.
- Canonical-JSON sha256 `c3ef65830c3bf729354749a94c2038c072fa72f99b5977608f41aa6bd19c0b92`, which equals the pinned `definition.sha256`.
- Evaluation plan canonical sha256 `51d94913b6011a966ffd7b881e3f47509e20dc176b1c6ba4c821dfd084d2e41d`, which equals the pinned `evaluation_plan.sha256`.
- Both were re-verified for 0.3.0.

**Observation and return**
- t is `available_at` = close_ts + 1 ms of each hourly candle.
- C(t) is the close `c`, a Decimal string.
- R24(t) = C(t) / C(t−24h) − 1.
- The reference observation is the one nearest t−24h within ±1 h; ties go to the earlier one.

**Classification**
- UP if R24 **>** +0.03.
- DOWN if R24 **<** −0.03.
- Otherwise NONE. A move of exactly ±3% is not a crossing.

**Coverage.** An hour is VALID only if the 24-hour reference exists within the tolerance and no gap in [t−25h, t] exceeds 2 h. Otherwise it is LOW_COVERAGE.

**Episodes**

| Rule | Definition |
|---|---|
| Start | The first VALID crossing |
| Continuation | Further crossings in the same direction while the episode is active |
| Merge | A same-direction crossing within 24 h after resolution re-opens the episode |
| Opposite direction | Ends the active episode and starts a new one |
| Resolution | 6 consecutive VALID hours with \|R24\| < 0.02 |
| Onset | The lowest close (UP) or highest close (DOWN) in [T_x − 24h, T_x]; ties go to the latest hour; never revised |

### 2.2 Event times [APPROVED D2]

| Symbol | Definition | Role |
|---|---|---|
| `T_on` | `available_at` of the v1.0.0 onset candle | **Primary `T_ref`**. A proxy, not the true onset |
| `T_x` | `available_at` of the first VALID crossing | **Secondary** measure, reported separately and never decisive |

Both times are **labels only**. They are never used as features.

### 2.3 12-hour warning rule [APPROVED D3; mechanics PROPOSED]

**Decision grid.** Decision times t are the hourly `available_at` instants. At each one, the output is `WARN_UP`, `WARN_DOWN`, `WARN_ANY` (direction-agnostic) or nothing.

**Runs.** A run is a sequence of consecutive warning hours of the same type. A non-warning hour or a NOT_EVALUABLE hour breaks it. Each run is scored once, at its issue time `t0`.

**Qualifying window.** A run qualifies for an episode when **`T_ref − 36h ≤ t0 ≤ T_ref − 12h`**.
- Both ends are inclusive; **exactly 12 h qualifies**.
- W = 24 h is a validity window, not a second horizon. There is no 6-hour horizon.

**Direction.** `WARN_UP` qualifies only for UP episodes and `WARN_DOWN` only for DOWN episodes. `WARN_ANY` qualifies for either. UP and DOWN results are always reported separately.

**Outcomes**

| Outcome | Rule |
|---|---|
| Hit | Credited to the earliest qualifying `T_ref`; at most one episode per run |
| Duplicate | A further qualifying run for an episode that already has a hit; reported separately |
| Inside-move | `t0` > `T_ref − 12h` and at or before resolution, for a compatible direction. Not a hit; counted in the precision denominator |
| False warning | Any other scored run |
| Miss | An evaluable episode with no qualifying run |

**Overlapping events.** Each episode needs its own qualifying run. A `WARN_ANY` run that qualifies for two episodes is credited to the earlier one only.

**Censoring**
- Runs with `t0` later than period_end − 36 h are not scored and are counted.
- Episodes whose window starts before the first scored decision hour are not evaluable and are counted.

**Burden and diagnostic (always reported)**
- Share of warned hours, runs per 30 days, median run length, and over-long runs.
- An already-moved diagnostic per run: R24(`t0`), the move from the onset close to C(`t0`), and the move from C(`t0`) to C(`T_x`).

### 2.4 Three separate layers: labels, lookbacks, eligibility [APPROVED D4/D6; rules PROPOSED]

The three layers are built and checked separately.

| Layer | What it is | Data it may read | Data it may never read | Boundary |
|---|---|---|---|---|
| **L — Event labels** (episodes, `T_on`, `T_x`, R24 for classification, resolution) | The target | **Forward evaluation:** only price observations with `available_at` > `SEAL_END`. **Development screening:** only `available_at` < `DEV_END` | Sealed-period prices or outcomes, in any form. Pre-seal prices for forward labels | Label burn-in, defined below |
| **F — Feature lookbacks** (P1–P5, O1–O4, U1f–U3f) | Point-in-time context at decision time t | Price: any observation with `available_at` ≤ t, including pre-seal (D4) and sealed-period (D6) prices, tagged `LOOKBACK_ONLY`. OI and funding: only observations outside the seal (§4.2, U12) | Anything with `available_at` > t. Any label, outcome or target return. Sealed-period OI or funding | Computed **only at eligible decision hours** (E). Never at sealed hours |
| **E — Evaluation eligibility** | Which decision hours and episodes are scored | The eligibility rules of §2.5 and §7 | — | `t0` > `SEAL_END`, plus §7 completeness |

### 2.5 Post-seal evaluation boundary [VERIFIED convention; PROPOSED resolution of the end-point]

**Protocol source.** `evaluation_plan.json` v1.0.0 sets `evaluation_scored_utc = ["2026-07-22T00:00:00Z", "2026-10-09T21:00:00Z"]`. Its data snapshot runs "candle open … to 2026-10-09 20:00 (availability through 2026-10-09 21:00)".

**Timestamp convention (preserved).** Observation time is `available_at` = close_ts + 1 ms, in UTC epoch milliseconds.

| Constant | Value |
|---|---|
| `DEV_END` | 2026-07-22T00:00:00.000Z = **1784678400000** |
| `SEAL_END` | 2026-10-09T21:00:00.000Z = **1791579600000** |

**End-point resolution.** The plan does not say whether its end-point is inclusive. The observation with `available_at` = `SEAL_END` (candle open 20:00) is the last one in the sealed snapshot. This spec **resolves the ambiguity conservatively: it treats that observation as sealed.**

**Sealed and post-seal**
- Sealed period: `DEV_END` ≤ `available_at` ≤ `SEAL_END`.
- Post-seal: `available_at` > `SEAL_END`.
- The first post-seal observation, and the first eligible decision time, is `T1` = 2026-10-09T22:00:00.000Z = 1791583200000 (candle open 21:00).
- The sealed boundary itself is **unchanged**. [VERIFIED]

**Label burn-in**
- **First labelable hour.** A forward hour t is labelable only if its whole label window [t − 25h, t] is post-seal. That makes the first labelable hour t ≥ `SEAL_END` + 26h = 2026-10-10T23:00:00Z. This ensures no label uses a sealed reference price.
- **Unknown starting state.** The episode state at the boundary is unknown, because it would require sealed labels. Crossings are therefore treated as **BURN_IN** (not events) until the first run of 6 consecutive VALID, post-seal-labelled hours with |R24| < 0.02 has completed.
- **Burn-in exit.** After that point, a crossing can only start a new episode. A sealed-period episode cannot merge, because its last crossing is at or before `SEAL_END` and the 24-hour merge window has expired.
- Burn-in crossings are counted and reported. They are never scored.

**Event eligibility.** An episode is a forward evaluation event only if all of the following hold:
- its first crossing `T_x` falls after the label burn-in;
- its whole onset window [`T_x` − 24h, `T_x`] is post-seal;
- its warning window [`T_on` − 36h, `T_on` − 12h] consists of eligible decision hours (§7).

## 3. Event-onset ambiguity [APPROVED D2]

R24 > 3% marks a completed 24-hour change. `T_x` lags the move, which can be up to 24 h old by then.

`T_on` is v1.0.0's estimated onset: the extreme close in the 24 h before `T_x`. **It is a reproducible proxy, not the true economic onset.** It is anchored to the 24-hour lookback, located only at `T_x`, and blind to build-ups longer than 24 h.

| Use | Reference | Status |
|---|---|---|
| **Primary (decides)** | `T_on` | Decides results |
| Secondary | `T_x` | Separate tables, descriptive only |
| Already-moved diagnostic | — | Reported for every run |

A result that holds only under `T_x` is labelled "consistent with in-move detection, not anticipation".

## 4. Features and data availability

### 4.1 Sources: local only [VERIFIED]

| Source | File (sha256) | Coverage | Available at | Historical | Live |
|---|---|---|---|---|---|
| Hyperliquid BTC perp, 1 h OHLCV (market-moves) | `research-data/market-moves` @0eb5be0 `market_moves/hourly/BTC/observations.jsonl` (`e0141555…6a501d`, the plan pin) | Candle open 2026-03-23 22:00 → 2026-10-09 20:00 | close + 1 ms | yes | feasible |
| Hyperliquid BTC perp, 1 h (forward store, `hyperliquid_hip3` file, instrument `BTC`) | `research-data/risk-regime-forward` @22d1c76, partitions 2026-10-06 → 10-09 (index sha256 c81a2912…) | Open 10-06 08:00 → 10-09 23:00; normalised `close`, with o/h/l in `raw` | open + 1 h | yes | feasible |
| Binance BTCUSDT OI, 5 min | frozen b5bcff8 `risk_regime_oi/observations.jsonl.gz` (`0cadd101…1d01`) plus forward 10-08 and 10-09 | 2026-01-01 → 10-09 23:55, continuous | create_time + 5 min | yes (point-in-time assumption) | **BLOCKED** (archive is next-day; REST returns 451) |
| Binance BTCUSDT funding, 8 h | Frozen file as above | 2026-01-01 → 09-30 16:00. October not yet published (monthly) | calc_time | yes | **BLOCKED** |
| Hyperliquid BTC funding | Frozen raw | From 08-18 only (sealed) | — | **excluded** | — |

### 4.2 Lookback availability checks [VERIFIED; local, counts only]

Script `exp23/lookback_availability_check.py` (sha256 `04f2b0c6…21d8`) prints timestamps and counts only: no price, return, label, episode or metric.

**Window checked.** The longest price lookback is P3 (720 hourly returns ending at t−24h, plus the 1-hour reference tolerance). At `T1` it needs every hourly close with `available_at` in [1788901200000, `SEAL_END`], i.e. 2026-09-09 13:00 → 2026-10-09 21:00.

**Price results** (market-moves file plus forward store)

| Check | Result |
|---|---|
| Hours present | **745 of 745 required** |
| Missing | 0 |
| Duplicates | 0 |
| market-moves `available_at` | equals close_ts + 1 ms for every row |
| Forward-store `available_at` | equals open + 1 h for every row |
| **Source consistency** | The 85 overlapping hours (10-06 09:00 → 10-09 21:00 `available_at`) are **exactly equal** in close for all 85. The check compares equality only; no value was printed or used |
| **Continuity at the seal** | Market-moves ends at `available_at` = `SEAL_END`; the forward store continues at `T1` with a 1 h step, so there is **no gap** |
| Forward store high/low | o/h/l/c are present in `raw` for all 88 forward BTC rows (needed by P4/P5) |

**Result for price features.** Every required historical price input passes the timestamp, completeness and source-availability checks. Under D6, **the price-feature warm-up is removed**: P1–P5 are computable from `T1`. [VERIFIED]

**Open interest and funding**
- D6 covers **price observations only**. Sealed-period OI and funding are therefore **not** permitted as lookback.
- Their lookbacks must be built from post-seal observations. The frozen and forward OI series would technically pass continuity (2026-01-01 → 10-09 23:55, 5 min, no gaps per the earlier audit), but that is not authorised. See U12.

| Feature | Earliest computable (post-seal data only) |
|---|---|
| O1 | `T1` (plus 5 min) |
| O2 72h | 2026-10-12 22:00 |
| O3 7-day percentile, ≥ 90% of slots | about 2026-10-16 05:00 |
| U1f | first post-seal settlement, 2026-10-10 00:00 |
| U2f (9 settlements) | about 2026-10-12 16:00 |
| U3f (≥ 80 of 90 settlements) | about 2026-11-05 08:00 |

**Research latency for funding.** October's settlements become readable only after the monthly file is published (about early November 2026). This delays research, not point-in-time correctness. [VERIFIED rule; dates PROPOSED, derived arithmetically]

**Common scored start.** §5 requires identical eligible hours for F1–F4. The paired comparison therefore starts at the **latest** warm-up end, which is U3f at about 2026-11-05 (about 26–27 days after `T1`). The remaining warm-up is driven by funding, not price.

### 4.3 Feature definitions [PROPOSED; none implemented]

Unchanged from 0.2.0. Lookbacks, aggregation and staleness are fixed now.

| Id | Feature | Fields | Lookback | Available | Max staleness | Missing |
|---|---|---|---|---|---|---|
| P1 | R_k for k ∈ {1,4,12,24} h (reference within ±1 h) | `c` | ≤ 25 h | close + 1 ms | ±1 h; gaps ≤ 2 h | NOT_EVALUABLE |
| P2 | σ_24h, σ_168h of hourly log returns (≥ 90% present) | `c` | 24 / 168 h | same | same | NOT_EVALUABLE |
| P3 | σ_720h (as B3, excluding the last 24 h) and σ_24h/σ_720h (≥ 648 of 720) | `c` | 720 h | same | same | INSUFFICIENT_HISTORY |
| P4 | Mean (h−l)/c over 24 h | `h`,`l`,`c` (forward: from `raw`) | 24 h | same | same | NOT_EVALUABLE |
| P5 | Distance from the 24 h and 7-day high/low, in σ units | `h`,`l`,`c` | 24 / 168 h | same | same | NOT_EVALUABLE |
| O1 | OI level (BTC, USD) | `sum_open_interest(_value)` | — | create_time + 5 min | 15 min | NOT_EVALUABLE |
| O2 | ΔOI over k ∈ {1,4,12,24,72} h (±10 min) | same | ≤ 72 h | same | 15 min at both ends | NOT_EVALUABLE |
| O3 | OI percentile within its own trailing 7 days (≥ 90% of slots) | same | 7 d | same | 15 min | NOT_EVALUABLE |
| O4 | ΔOI_k − R_k for k ∈ {4,24} h | O2 + P1 | 24 h | later of the two | both | NOT_EVALUABLE |
| U1f | Funding level | `funding_rate` | — | calc_time | 8 h 15 min | NOT_EVALUABLE |
| U2f | Change vs the previous settlement; mean of the last 9 | same | 3 d | same | 8 h 15 min | NOT_EVALUABLE |
| U3f | Percentile within the trailing 30 days (≥ 80 of 90) | same | 30 d | same | 8 h 15 min | NOT_EVALUABLE |

**Staleness limits.** 15 min, 8 h 15 min and ±10 min are PROPOSED. ±1 h and the 2 h gap rule are VERIFIED from v1.0.0.

**Excluded.** Hyperliquid funding, HIP-3 markets, long/short ratios and volume.

**Rule family [PROPOSED].** One pre-declared logistic rule per set and warning type. Coefficients and the alert threshold are fitted on development folds only. The threshold is set at the matched warning rate q.

## 5. Baseline comparison design [APPROVED D3/D5; mechanics PROPOSED]

| Set | Features | Role |
|---|---|---|
| F0 | — | Chance: 1,000 circular shifts of the compared set's own warning series |
| F1 | P1–P5 | **Price-only baseline** |
| F2 | F1 + O1–O4 | Price + OI |
| F3 | F1 + U1f–U3f | Price + funding |
| F4 | F1 + O + U | Price + both |

**Fairness rules**
- All sets are scored on identical eligible hours: the intersection where F4 is evaluable, starting at the common scored start (§4.2).
- All sets use identical episodes, `T_ref`, window, censoring, consolidation and scorer.
- Thresholds are set on development folds to **q = 5%** of eligible hours.
- Sensitivity checks run at **2.5% and 10%**. These are never chosen afterwards as the headline.

**Comparisons.** F2, F3 and F4 against F1, Holm-corrected over the three; every set against F0.

## 6. Evaluation metrics [all NOT COMPUTED]

Every metric is reported for UP, DOWN and `WARN_ANY`; under `T_on` (primary) and `T_x` (secondary); and at q = 5%, then 2.5% and 10%.

| Metric | Definition and uncertainty |
|---|---|
| 12-hour warning recall | hits / evaluable episodes, with a Wilson 95% interval |
| Warning precision | hits / (hits + false warnings + inside-move runs), with a Wilson 95% interval |
| False warnings per 30 days | Poisson 95% interval |
| Misses, inside-move runs, duplicates | counts and lists |
| Lead time of hits | median and 10th–90th percentiles of `T_ref` − `t0` |
| Already-moved diagnostic | distributions over hits and over all runs |
| Warning burden | share of warned hours, runs per 30 days, over-long runs |
| Incremental value vs F1 | episode-level exact McNemar (Holm); Δ false warnings per 30 days at matched q, with a day-block bootstrap 95% interval |
| vs chance | recall vs the F0 distribution at the same burden (one-sided, 95th percentile) |

**Reporting rules (D5)**
- Every point estimate is reported with its interval.
- Any cell with fewer than 20 episodes shows counts only.

**Decision rule**
- **SUPPORTED** requires **all** of:
  - the primary reference (`T_on`);
  - the D5 checkpoint is met;
  - Holm-adjusted McNemar p < 0.05;
  - false warnings per 30 days do not rise at matched q;
  - the effect has the same sign in both chronological halves;
  - the set beats F0.
- Below the checkpoint the result is **INSUFFICIENT_SAMPLE**: counts only, no p-values.
- Anything else is **NOT_SUPPORTED**.
- SUPPORTED is evidence for further study, **not proof** and not grounds for deployment. The development period alone can never produce SUPPORTED.

## 7. Missing and stale data [PROPOSED; VERIFIED where marked]

- **Labels.** v1.0.0 VALID / LOW_COVERAGE rules apply [VERIFIED], plus the §2.5 burn-in.
- **No filling.** No imputation, interpolation or zero-fill. Forward-fill is allowed only within the staleness limit, and never across a gap or a LOW_COVERAGE hour.
- **NOT_EVALUABLE decision hours.** A decision hour is NOT_EVALUABLE if any required feature of the scored set is missing or stale, or if its lookback would need non-permitted data (sealed-period OI or funding, §4.2). Such an hour cannot issue warnings and breaks runs. These hours are counted.
- **Evaluable episodes.** An episode is evaluable only if it is eligible (§2.5), at least 90% of its warning-window hours are evaluable, and there is no LOW_COVERAGE hour in [`T_on`, `T_x`]. Exclusions are counted.
- **INCONCLUSIVE.** If more than 10% of scored hours or more than 20% of episodes are not evaluable, the result is INCONCLUSIVE.
- **Forward-store integrity.** A CONFLICT, a superseded partition or a checksum failure makes the affected hours NOT_EVALUABLE, and the run IDs are listed.

## 8. Leakage-prevention rules [PROPOSED; D4 and D6 APPROVED]

1. **Separate builders.** The label builder (L), feature builder (F) and eligibility builder (E) are separate processes. They are joined only in the scorer.
2. **Label input filters.**
   - For forward evaluation, the label builder accepts only price rows with `available_at` > `SEAL_END`. For development screening, it accepts only rows with `available_at` < `DEV_END`.
   - Both filters are asserted at load. Any row with `DEV_END` ≤ `available_at` ≤ `SEAL_END` aborts the label builder.
3. **Feature lookback.** The feature builder accepts price rows with `available_at` ≤ t from any period, tagged `LOOKBACK_ONLY` if they are pre-seal (D4) or sealed (D6).
   - It accepts OI and funding rows only with `available_at` outside the sealed interval (and ≤ t).
   - It computes features **only at eligible decision hours** (t ≥ `T1` for evaluation; development hours for screening). It **never** computes features, signals or warnings at sealed hours.
4. **Excluded from features.**
   - No label, outcome or target return enters F: no `T_on`, no `T_x`, no episode state, no future R24.
   - A backward-looking R24 at t used as a feature (P1) is allowed, because it uses only `available_at` ≤ t. The sealed-period target R24 labels are never computed.
5. **Nothing is chosen using sealed-period information.** No sealed-period outcome or metric is used to select features, lookbacks, thresholds, rule family, q, W or models. All of these are fixed in this spec or fitted on development folds only.
6. **Development cross-validation.** Three chronological blocked folds, with a 60 h purge around each validation block. No shuffling. The last 36 h of development decision hours are censored.
7. **Leakage tests** (to be written later, not now):
   - features at t are byte-identical when any data with `available_at` > t is removed or altered;
   - removing all sealed-period rows leaves the label output byte-identical;
   - no sealed OI or funding row reaches F;
   - the maximum `available_at` used ≤ t;
   - the label builder rejects any row in the sealed interval.

## 9. Sealed-period protection [VERIFIED boundary unchanged; D4/D6 APPROVED]

| Period | `available_at` | Labels / events | Features (lookback only) | Metrics |
|---|---|---|---|---|
| Warm-up | < 2026-04-24 | no | price (D4); OI and funding outside the seal | no |
| Development | [2026-04-24, `DEV_END`) | development screening only | price; OI; funding | development screening only; never decisive |
| **Sealed** | [`DEV_END`, `SEAL_END`] | **never** | **price only** (D6), for post-seal decision hours | **never** |
| Forward | > `SEAL_END` | after burn-in (§2.5) | price; OI; funding | at the D5 checkpoint |

**Prohibited for the sealed period** [APPROVED; enforced in §8]:
- scoring any observation or episode as an evaluation event;
- using outcome labels or target returns;
- computing performance metrics;
- using outcomes for feature, parameter, model or threshold selection;
- using any future information in a feature.

The sealed period remains reserved for the existing detector plan's single authorised run.

**Earlier analyses that overlapped the seal** (contamination risk; recorded, not repeated)

| # | Analysis | Overlap |
|---|---|---|
| C1 | `RISK_REGIME_EVENT15_RESEARCH.md` / `RISK_REGIME_OI_RESEARCH.md` | OI and funding around Event #15 (09-27/28) and 123 episodes |
| C2 | k-NN/Challenger large-move accuracy | 08-01 → 10-09 |
| C3 | TimesFM reconstruction | 09-06 → 10-09 |
| C4 | EXP-002/003 momentum rows | 09-21/22 |
| C5 | Feasibility report | timestamp ranges only |
| **C6** (0.3.0) | `lookback_availability_check.py` | Sealed-period price **timestamps** checked and the 85 overlap closes compared for **equality only**. No value, return, label or metric was printed or computed. This is the D6 availability check |

Because of C1, OI and funding hypotheses rely on forward evidence only.

**Registration.** The 0.3.0 hash must be approved and recorded before any feature-versus-label work, including development work.

## 10. Sample size and duration [APPROVED D5; planning only]

**Planning input.** 23 episodes in 88 development days (about 7.75 per 30 days; 12 UP, 11 DOWN). This is EXPLORATORY: from `dev_episode_count.py` (`f1b89d12…a97a`), using pre-seal data only. It is not a result.

| Phase | Requirement | Estimate (conditional) |
|---|---|---|
| Pipeline validation | 14 consecutive post-seal days of complete partitions, one recovered late Binance publication, and the October funding month file | about 2 weeks; funding about early November 2026 |
| Price features | — | **no warm-up** (D6; §4.2 checks pass) |
| Label burn-in | First labelable hour 2026-10-10 23:00, then until the first post-seal 6-hour calm run | hours to days; data-dependent; counted |
| OI features (post-seal only) | 72 h (O2) / 7 days (O3) | about 2026-10-12 / 10-16 |
| Funding features (post-seal only) | 30 days (U3f) | about 2026-11-05, readable after the November publication of October's file |
| **Common scored start for F1–F4** | latest of the above | **about 2026-11-05** (unless U12 is approved) |
| Development screening | development period only (about 20 evaluable episodes) | available after hash approval; screening only |
| **D5 checkpoint** | ≥ 60 evaluable forward events, ≥ 25 per direction | At 7.75 per 30 days with about 10% non-evaluable: **about 260 days after the common start** (about 9 months). At 5 per 30 days: about 400 days. At 10 per 30 days: about 200 days. **A preliminary checkpoint, not proof**; no date is promised; interim reports show counts only |
| Before considering deployment | SUPPORTED at the checkpoint; an independent second forward period; a live OI and funding route (BLOCKED); stability across volatility terciles; an explicit cost of false warnings | not estimable |

**What extends these durations.** A lower event rate; collection failures; LOW_COVERAGE hours; a long burn-in; direction imbalance; low discordance between F1 and F2–F4; any change to the definition.

## 11. Limitations, blockers and open decisions

| # | Item | Status |
|---|---|---|
| U1–U3, U5, U5′ | Thresholds; reference; window and rate; pre-seal lookback; sealed-period **price** lookback | **Resolved** (D1–D4, D6) |
| **U12** | Whether sealed-period **OI and funding observations** may also be lookback-only history. If yes, the OI warm-up disappears (continuous 5-minute series, pending a re-run of the availability check). Funding would still be incomplete until October's monthly file is published (frozen data ends 09-30 16:00) | **Open decision** (default: not permitted) |
| U13 | The plan's end-point inclusivity is not stated. Resolved conservatively (the `SEAL_END` observation is sealed); confirmation requested | Confirmation requested |
| U4 | Archive vs live OI parity is unverified. OI and funding are research-only, not available live | BLOCKED (operational) |
| U6 | Hyperliquid funding has no development history and is excluded | BLOCKED |
| U7 | No label, feature or eligibility builder, nor scorer, exists. Implementation authorisation required | BLOCKED |
| U8 | Small development sample (about 10–12 episodes per direction) | Limitation |
| U9 | C1: OI was examined in the sealed period, so confirmation is forward-only | Limitation |
| U10 | Binance OI and funding vs Hyperliquid price (venue mismatch) | Limitation (features only) |
| U11 | `T_on` is a proxy | Limitation |
| U14 | Both price sources are retrospective collections (`retrieved_at` after `available_at`). Point-in-time correctness relies on `available_at`, not on what a live system saw | Limitation |

## 12. No model performance was evaluated [VERIFIED for this task]

No warning rule, detector, model, builder, scorer, feature table, label file or score was built or computed. Every §6 metric is **NOT COMPUTED**.

For 0.3.0, sealed-period price data was read only for the D6 availability check (C6): timestamps, counts and exact-equality matches. No sealed-period outcome, label, target return, episode or performance metric was read or computed.

## 13. SHA-256 of this specification

- **Rule:** SHA-256 of this file's exact bytes, in lowercase hex.
- **Sidecar:** `exp23/BTC_12H_EARLY_WARNING_SPEC_v0.3.0.sha256`. Verify with `sha256sum -c BTC_12H_EARLY_WARNING_SPEC_v0.3.0.sha256`.
- A file cannot contain its own hash, so the value lives in the sidecar and in the reply to the user.
- 0.2.0 (`BTC_12H_EARLY_WARNING_SPEC.md` + `.sha256`) and 0.1.0 (`…_v0.1.0_2fd3777e.md` + `…_v0.1.0.sha256`) are kept unchanged.
- **The hash is NOT registered.** Registration requires separate user approval. Any commit to a research branch requires separate authorisation.

## 14. Recommended next step [PROPOSED]

1. Decide U12 (sealed-period OI and funding as lookback-only: yes or no) and confirm U13.
2. Approve the resulting hash.
3. Only then, separately authorise offline implementation of L, F, E and the scorer for development screening, with the §8 leakage tests.
4. Forward collection continues unchanged.
