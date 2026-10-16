# BTC 12-hour early-warning research specification: source-specific funding variants

| Field | Value |
|---|---|
| Spec id | `btc-12h-early-warning` |
| Version | **0.5.0** (2026-10-13), an **addendum** to 0.4.0 |
| Baseline | 0.4.0, sha256 `8231568780906804663e6ba8687cc1a7bb6d867f9fbaed133a0876c5e9851ba4` (`BTC_12H_EARLY_WARNING_SPEC_v0.4.0.md`, unchanged). Everything not stated here is exactly 0.4.0 |
| Effective | From the first forward evaluation run on research code containing this file. Earlier reports are not rewritten |
| Hash registration | **Not registered.** Registration requires separate user approval of the sidecar hash |
| Implementation | `research/early_warning_12h.py` (`SOURCE_FUNDING`, `SOURCE_VARIANT_SETS`, `load_source_funding`, `FeatureBuilder.source_funding_features`, `run_source_variants`), `research/exp23_forward_eval.py`, collector `research/risk_regime_forward.py` (`okx_btc_funding`) |

## 1. What changes and what does not

**Unchanged (0.4.0 baseline):**
- the v1.0.0 price labels, the ±3% thresholds, the 12-hour horizon and the [T_on − 36 h, T_on − 12 h] window;
- the sealed boundary (`SEAL_END` = 2026-10-09T21:00:00Z, inclusive);
- walk-forward fitting (D9), pairwise scoring (D10), F0, the intervals, the D5 checkpoint (60 events, ≥ 25 per direction) and the 20-episode cell rule;
- F1 = P, F2 = P + O, F3 = P + U, F4 = P + O + U, with U = Binance funding. Their report (`report.json`) and their Holm family (F2/F3/F4 vs F1) are produced exactly as before.

**Added:** four source-specific variants, reported separately in `source_variants.json`.

| Set | Features | Funding source |
|---|---|---|
| F3_HL | P + U_HL | Hyperliquid BTC funding (`hyperliquid_btc_funding`, instrument `BTC`) only |
| F4_HL | P + O + U_HL | as above |
| F3_OKX | P + U_OKX | OKX BTC-USDT perpetual funding (`okx_btc_funding`, instrument `BTC-USDT-SWAP`) only |
| F4_OKX | P + O + U_OKX | as above |

- **No blending.** A variant never reads another source's funding, and there is no fallback.
- **Binance funding** is not an input to any v0.5.0 variant. Its absence never blocks them.
- **Comparisons:** Fk_SRC vs F1, each scored on the hours where Fk_SRC is evaluable (D10).
- **Holm:** applied over these four comparisons only, per warning type and rate. The v0.4.0 family is separate and unaffected.
- **Gating:** each pair is gated by its own checkpoint. Below it, the report has counts only.

## 2. Funding features U_SRC

The features are the same four as in 0.4.0's U:
- level;
- change vs the previous settlement;
- the 3-day mean;
- the 30-day percentile.

Windows are defined in time, and minimum counts scale with each source's nominal interval.

| Source | Nominal interval | Max staleness (t − latest usable settlement) | 3-day mean: min settlements | 30-day percentile: min settlements |
|---|---|---|---|---|
| HL | 1 h | 1 h 15 min | 65 (≥ 90% of 72) | 640 (≥ 80/90 of 720) |
| OKX | 8 h | 8 h 15 min | 9 (≥ 90% of 9) | 80 (≥ 80/90 of 90) — identical to 0.4.0 |

- **No filling.** Missing observations are never filled, interpolated or carried backward. U_SRC is NOT_EVALUABLE when freshness or the specified minimum-count requirements fail.
- **OKX interval:** OKX's interval is not assumed constant.
  - Every stored settlement records its actual spacing (`raw.spacing_ms`, `raw.spacing_irregular`).
  - Every collection run records any irregular spacing.
  - The minimum counts use the nominal interval. A shorter real interval can only add settlements; a longer one makes the window fail its count.

## 3. Availability (causality) — the rule for v0.5.0

A settlement timestamp is not, by itself, the time a value became available. For every v0.5.0 funding row:

**causal_available_at = max(settlement `available_at`, first observation by the collector)**

- **First observation** is the row's `first_observed_at`. The collector sets it after the provider's response is in hand.
- **Rows collected before 0.5.0** have no `first_observed_at`. They are bounded by `retrieved_at` + 30 min. `retrieved_at` is the collection run's start, and 30 min is the collection job's hard timeout, so the record was certainly in hand by then.
- **Rows with no observation time at all** are counted as non-causal and excluded.

At a decision time t, a v0.5.0 feature may use only rows with causal_available_at ≤ t. The `FeatureBuilder` asserts it and raises `LookAheadError` otherwise.

**Historical or backfilled funding is not read.** That covers the frozen `risk_regime_oi` / `risk_regime_raw` Hyperliquid funding history (from 2026-08-18) and any OKX history older than the collector's first observation, because their historical availability cannot be proven. A backfilled row therefore only becomes usable at decision times after the collector first observed it.

**Consequence: a blocker, not a weakening.** The forward collector runs once a day (10:40 UTC) and collects closed days only. A settlement is therefore first observed 11.7 to 34.7 hours (Hyperliquid) or 18.7 to 34.7 hours (OKX) after it happened. That is beyond both staleness limits, so with the current collection cadence the four variants are expected to be **NOT_EVALUABLE** at every decision hour. Their reports show the blocker and the measured observation lag.

Making them evaluable would require observing funding intraday, close to settlement. That is a change to the collection schedule, which needs separate approval. The causality rule is not relaxed to obtain data.

Unlike 0.4.0, whose U14 accepted retrospective collection for Binance funding, 0.5.0 does not apply settlement-time availability to the new sources.

## 4. Collection: OKX

- **Source:** `okx_btc_funding`, read from `GET https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP`. Public, no key.
- **Pagination:** newest first; `after` = the oldest `fundingTime` seen; `limit` 100.
  - Every page must be strictly older than the cursor. Anything else fails closed (`PAGINATION_ERROR`).
  - A record repeated with different content is `MALFORMED_RESPONSE`.
- **Rate limits:** requests are spaced ≥ 0.25 s. HTTP 429 and 5xx are retried with backoff, as are OKX codes `50011` / `50061` sent inside an HTTP 200. Attempts are bounded.
- **Errors:** 404 and other HTTP errors, and malformed payloads, become a recorded status (`PROVIDER_ERROR`, `MALFORMED_RESPONSE`, `BLOCKED_BY_NETWORK_POLICY`, `BLOCKED_BY_PROVIDER_GEO_RESTRICTION`). They never become a substitute value.
- **Stored per settlement:**
  - normalised: metric `funding_rate`, plus `realized_rate` when provided, UTC epoch ms, `available_at` = `fundingTime`, `first_observed_at`;
  - raw: `instId`, `instType`, `fundingTime`, `fundingRate`, `realizedRate`, `formulaType`, `method`, the spacing fields and the provenance URL.
- **Store rules:** the forward store's rules are reused unchanged: closed day partitions, atomic writes, checksums, idempotency, strict-superset updates, conflicts kept and not overwritten, missing slots listed and never filled, and the index rebuilt each run.
  - `first_observed_at` is observation metadata, like `retrieved_at`: it is excluded from content fingerprints and conflict checks, so a superset update keeps the earlier row with its earlier observation time.
  - **OPEN partitions (intraday).** With the opt-in `--open-day` mode, the current UTC day(s) of `hyperliquid_btc_funding` and `okx_btc_funding` may be written before the day ends. Such a partition is OPEN: its slots are expected only up to the run time and it is never COMPLETE. Later runs complete it under the same superset and conflict rules, and each row keeps its first `first_observed_at`. Feature definitions, staleness limits, minimum counts and the causality rule are unchanged.
- **Provider history limit:** days older than anything the provider returned are recorded as `BEYOND_PROVIDER_HISTORY`.

## 5. Collection: Hyperliquid

`hyperliquid_btc_funding` collection and normalisation are reused unchanged (`fetch_funding`, `hyperliquid_funding_obs`). The only addition is `first_observed_at` on new rows.

## 6. Reporting

`source_variants.json` holds, per source:
- settlements, conflicts dropped and non-causal rows;
- the first and last settlement, and the last causal availability;
- the median and minimum observation lag;
- the staleness limit;
- not-evaluable hours, and explicit blockers.

Per variant, it holds coverage (evaluable hours) and the pairwise comparison counts. No performance or predictive benefit is claimed below the checkpoint.

## 7. Open items

| # | Item | Status |
|---|---|---|
| V1 | Intraday funding observation, needed for any v0.5.0 variant to become evaluable | Collector mode implemented (OPEN partitions, section 4); the hourly schedule needs separate approval |
| V2 | OKX API reachability from the collection runner, which may be geo-restricted; a refusal is recorded as `BLOCKED_BY_PROVIDER_GEO_RESTRICTION` | Unverified (no live call made) |
| V3 | OKX pagination direction, taken from OKX v5 conventions and enforced at runtime | Verified against the official OKX API documentation; runtime checks retained. |
| V4 | Re-pinning the two scheduled workflows on `main` to a reviewed SHA containing this addendum | Separate, reviewable change |
