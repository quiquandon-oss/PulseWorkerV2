# Gemini Market Intelligence — Implementation Plan

Status: **PLANNING ONLY.** No Gemini investigation code is implemented, wired into
any route, wired into `scheduled()`, or deployed. This document plus the
deterministic helper functions in `worker.js` (clearly banner-commented as
planning-only) are the entire scope of this PR.

Revision note: this is the **v2 design**, updated after PR #2 review rejected
the original single-threshold trigger design in favor of a ranked investigation-
priority model, and asked for an explicit three-timestamp availability contract.
The v1 estimates (call volume, thresholds) are superseded below where they
conflict; §8's call-volume analysis is retained where still relevant.

---

## 1. Inspection: existing `coin_catalyst_log`, and the three-timestamp model

Live schema (confirmed via Cloudflare D1 MCP against `sentiment-history`,
`f91ca980-b886-423a-bd6f-f3baea46d181`):

```sql
CREATE TABLE coin_catalyst_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  coin TEXT NOT NULL,
  price_move_pct REAL,
  headline_matched TEXT,
  headline_source TEXT,
  extracted_reason TEXT,
  verdict TEXT,
  category TEXT,                    -- added in PR #1
  direction TEXT,                   -- added in PR #1
  source_url TEXT,                  -- added in PR #1
  discovery_timestamp INTEGER,      -- added in PR #1
  confidence TEXT,                  -- added in PR #1
  market_classification TEXT        -- added in PR #1
)
```

- **2 rows total**, both pre-existing V1 rows, unrelated to this contract, new
  columns `NULL` on both — left untouched (see PR #1's `HISTORICAL_BACKFILL.md`).
- **Zero rows written by V2 code.**

**Gap, and the fix this revision specifies:** the schema has `ts` (informally
"event timestamp" by convention) and `discovery_timestamp`, but is **missing
`first_public_timestamp`** — required as its own, independent column per PR #2
review. The three timestamps are NOT interchangeable and must all be
preservable where available:

| Timestamp | Meaning | Set by |
|---|---|---|
| `event_timestamp` (proposed: reuse `ts`, don't rename — see below) | When the underlying market event actually occurred | Gemini reports it; CryptoPulse stores it as-is, never inferred |
| `first_public_timestamp` (proposed: new column) | When credible public information about the event became available | Gemini reports it; CryptoPulse stores it as-is |
| `discovery_timestamp` (exists) | When Gemini itself discovered the information | Set at investigation time |

These are independent on purpose — Gemini can discover something late that
happened early and was public early (ordinary case), or discover something
whose public disclosure itself lagged the event (e.g. a delayed regulatory
filing), or, in the worst case, a source with an unreliable timestamp at all
(`first_public_timestamp = null`).

**The deterministic availability calculation — the exact contract, now
implemented as a pure function this PR (`computeAvailableBeforePrediction`,
tested, NOT wired in):**

```
available_before_prediction =
    first_public_timestamp <= prediction_timestamp     -> true
    first_public_timestamp >  prediction_timestamp     -> false
    first_public_timestamp unknown                      -> 'unknown' (the literal string, never null/undefined, never guessed)
```

Critically: **this uses `first_public_timestamp` only — never `event_timestamp`.**
An event that happened before the prediction is not automatically "available" —
if nobody could have known about it yet, it wasn't available, regardless of
when it actually occurred. `computeAvailableBeforePrediction` takes exactly two
arguments (`first_public_timestamp`, `prediction_timestamp`) — structurally
incapable of touching `event_timestamp`, which is enforced by a test asserting
its arity is 2.

Gemini never calls this function or computes this field itself — per
`.ai/GEMINI_MARKET_INTELLIGENCE.md`'s Critical Timestamp Rule, this stays
exclusively on the CryptoPulse side.

(PR #1's `classifyCatalystTiming` remains as a lower-level, generic two-
timestamp comparator used elsewhere; `computeAvailableBeforePrediction` is the
specific `available_before_prediction` contract with the three-state return
value PR #2 review specified — the two aren't meant to replace each other.)

**Proposed migration (NOT applied in this PR):** `ALTER TABLE coin_catalyst_log
ADD COLUMN first_public_timestamp INTEGER`. Deferred to the implementation PR,
same reasoning as PR #1's "additive schema, no purpose until something writes
to it" — this PR has nothing that would populate the column yet.

---

## 2. Investigation priority — replacing the single-threshold design

**PR #2 review correctly rejected the v1 design** (`confidence >= 0.85` as the
primary gate). The problem: a single OR-of-thresholds can't express that a 65%-
confidence call during a 12%-move, 3-asset-correlated failure matters far more
than a 90%-confidence call during a quiet market — exactly the distinction the
review asked for.

**v2 design: a continuous priority score, not a binary gate.**

```js
function computeInvestigationPriority(signals, weights) {
  const confidenceAdjustedError = signals.wasWrong
    ? Math.max(0, (signals.confidence - 0.5) * 2)   // 0 if correct, 0..1 scaled by how confident the wrong call was
    : 0;
  return weights.priceMovePct * Math.abs(signals.priceMovePct || 0)
       + weights.confidenceAdjustedError * confidenceAdjustedError
       + weights.correlatedAssetCount * Math.max(0, (signals.correlatedFailureAssetCount || 0) - 1)
       + weights.volatilityAnomaly * (signals.isVolatilityAnomaly ? 1 : 0)
       + weights.repeatedFailureCount * (signals.recentFailureCount || 0)
       + weights.regimeChangeFlag * (signals.isRegimeChange ? 1 : 0);
}
```

**The key design choice, directly addressing "separate PREDICTION CONFIDENCE
from INVESTIGATION PRIORITY":** confidence enters the score *only* through
`confidenceAdjustedError`, and that term is zero whenever the prediction was
correct — **a correct call contributes nothing to priority no matter how
confident it was.** Confidence only matters as a measure of "how surprising was
this miss," not as a standalone signal. Market-side signals (price move,
correlation, volatility anomaly, regime change) can independently push priority
high even at moderate confidence.

Implemented this PR (pure, tested, **not wired into anything**):
`computeInvestigationPriority`, `isHighInvestigationPriority` (threshold gate),
`rankInvestigationCandidates` (sorts a batch of candidate events highest-first),
`selectWithinBudget` (slices the ranked list by remaining budget, see §3).

`INVESTIGATION_PRIORITY_WEIGHTS` and `INVESTIGATION_PRIORITY_THRESHOLD` (=4) are
explicitly provisional — a starting point calibrated against the three worked
examples below, not a fitted or validated model. The implementation PR should
revisit both once real trigger data exists.

### Worked Example A — high confidence, small market movement -> LOW priority

| Signal | Value |
|---|---|
| Prediction confidence | 0.90 |
| Was the prediction wrong? | No |
| Price move | 0.4% |
| Correlated failures | 0 |
| Volatility anomaly | No |
| Regime change | No |

**Score: 0.5x0.4 + 3x0 + 2x0 + 1.5x0 + 0.5x0 + 2x0 = 0.2**

**Decision: skip.** Confidence is irrelevant here — the call was *correct*, so
`confidenceAdjustedError = 0` regardless of the 90% confidence. Nothing else
about the event is unusual. This is exactly the "a 90% prediction can have low
investigation priority if the market behaved normally and the error was small"
case from the review.

### Worked Example B — moderate confidence, extreme single-asset movement -> HIGH priority

| Signal | Value |
|---|---|
| Prediction confidence | 0.65 |
| Was the prediction wrong? | Yes |
| Price move | 8% (BTC) |
| Correlated failures | 0 (single asset) |
| Volatility anomaly | Yes |
| Regime change | No |

**Score: 0.5x8 + 3x0.3 + 2x0 + 1.5x1 + 0.5x0 + 2x0 = 4 + 0.9 + 1.5 = 6.4**

**Decision: investigate.** Well above the 4.0 threshold, driven almost entirely
by the price move and the volatility-anomaly flag — confidence (0.65, only
moderate) contributes a modest 0.9 via `confidenceAdjustedError`. This is the
review's "a 65% prediction can have very high investigation priority" case, in
its single-asset form.

### Worked Example C — correlated multi-asset failure -> HIGHEST priority, exceeds B

| Signal | Value |
|---|---|
| Prediction confidence | 0.65 |
| Was the prediction wrong? | Yes |
| Price move | 12% (LINK, the largest of the three) |
| Correlated failures | 3 (BTC 8%, ETH 9%, LINK 12% — several models failed together) |
| Volatility anomaly | Yes |
| Regime change | Yes |

**Score: 0.5x12 + 3x0.3 + 2x2 + 1.5x1 + 0.5x0 + 2x1 = 6 + 0.9 + 4 + 1.5 + 2 = 14.4**

**Decision: investigate — and rank above Example B (14.4 vs. 6.4).** This is the
review's exact scenario: "BTC moved 8%, ETH moved 9%, LINK moved 12%, several
models failed simultaneously" at only 65% confidence. The correlation term
(`correlatedAssetCount`) and the regime-change flag are what push this
decisively above B, not confidence — confidence is identical (0.65) in both B
and C.

All three examples are encoded as real assertions in
`tests/gemini-planning.test.js` (`describe('... worked examples from PR #2
review')`), kept in sync with this table — if the weights or threshold change,
both must be updated together.

---

## 3. Investigation budget

**Renamed per review** (was `MAX_INVESTIGATIONS_PER_*` in v1):

```js
MAX_GEMINI_INVESTIGATIONS_PER_DAY: 8
MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 2
```

**Budget-aware selection, not a simple gate:** `remainingGeminiBudget(counts,
config)` returns the *smaller* of daily-remaining and hourly-remaining (not
their sum — being at the hourly cap still blocks investigation even with
daily budget left). `selectWithinBudget(rankedCandidates, budget)` then takes
the top-`budget` candidates off the already-priority-ranked list, and
explicitly filters out anything below `INVESTIGATION_PRIORITY_THRESHOLD`
regardless of remaining budget — free budget doesn't make a LOW-priority event
worth investigating.

This directly implements "the trigger system should rank candidates by
investigation priority and process the highest-value candidates first" and "do
not assume every high-confidence failure deserves a Gemini call" — the old
`shouldTriggerInvestigation`/`withinGeminiRateLimit` pair (still present,
still tested, explicitly marked superseded in its code comment) could only
say yes/no per-event with no way to prefer one candidate over another when
several fire in the same cycle; `rankInvestigationCandidates` +
`selectWithinBudget` can.

---

## 4. Where market-event triggers should live

Unchanged from v1: a new `evaluateGeminiTriggers(env)`, called from
`scheduled()`'s existing 3-hourly branch via its own independent
`ctx.waitUntil(...)`, positioned after the six existing `predictThenSelect`
calls (needs that cycle's fresh predictions/outcomes to build each candidate's
`signals`). Never called from any synchronous request path.

**Updated for the v2 design:** each cycle, `evaluateGeminiTriggers` would build
one candidate per asset/event worth scoring (not just one boolean check),
`rankInvestigationCandidates` them, query the day's/hour's investigation counts
for `remainingGeminiBudget`, and `selectWithinBudget` to get the actual list to
investigate this cycle — versus v1's single trigger-or-not decision.

```js
// Illustrative only -- NOT implemented in this PR.
} else {
  // ... existing predictThenSelect waitUntil calls ...
  ctx.waitUntil(evaluateGeminiTriggers(env).catch(err => console.error('Gemini trigger evaluation failed:', err)));
}
```

## 5. Where to actually call Gemini

Unchanged from v1: a new `investigateMarketEvent(env, triggerContext)`, called
only for candidates that `selectWithinBudget` actually selected. Updated
9-step flow (steps 1-3 changed from v1's threshold check to the ranking flow
above; steps 4-9 unchanged):

1. `evaluateGeminiTriggers(env)` gathers per-asset signals this cycle
   (price move, wrongness/confidence, correlation, volatility anomaly, regime
   change).
2. Build one candidate per asset/event, `rankInvestigationCandidates`.
3. Query today's/this-hour's investigation counts, `remainingGeminiBudget`,
   `selectWithinBudget` — this is what decides which candidates actually get
   investigated this cycle, in priority order.
4. For each selected candidate, call `investigateMarketEvent` — the actual
   Gemini `fetch()`, following the existing pattern in
   `runGeminiDailyAnalysis`/`runLinkGeminiAnalysis` (`env.GEMINI_API_KEY`,
   `x-goog-api-key` header, `gemini-3.6-flash`). **Confirmed gap, still open:**
   neither existing call uses search grounding; `.ai/GEMINI_MARKET_INTELLIGENCE.md`
   requires it. The implementation PR needs to verify the current Gemini API's
   grounding tool schema before assuming it works like the existing calls.
5. Validate the response with `validateCatalystPayload` (PR #1's helper —
   pure, tested, still not wired in).
6. Check `isDuplicateCatalyst` against recent rows from `fetchCatalystsForPeriod`.
7. Compute `available_before_prediction` via `computeAvailableBeforePrediction`
   (this PR, §1) per affected prediction — using `first_public_timestamp`
   only, per the three-state contract.
8. Write validated, deduplicated catalysts via `recordCatalyst`.
9. Record the investigation outcome to the proposed `gemini_investigations`
   audit table (§7) regardless of whether a catalyst was ultimately written.

## 6. Failure isolation

Unchanged from v1: everything above sits inside the single
`ctx.waitUntil(evaluateGeminiTriggers(env).catch(...))` call site. Any
exception anywhere in the chain is caught there and logged, never propagated
to the cron handler. Same idiom already used for the existing
Gemini/calibration calls in `scheduled()`.

---

## 7. Required secrets / configuration

- **`GEMINI_API_KEY`** — already exists as a Worker secret, reused.
- **In-code config, this PR:** `GEMINI_TRIGGER_CONFIG` (renamed budget keys,
  see §3), `INVESTIGATION_PRIORITY_WEIGHTS`, `INVESTIGATION_PRIORITY_THRESHOLD`.
- **Proposed schema (NOT created in this PR):**
  - `coin_catalyst_log.first_public_timestamp INTEGER` (§1).
  - `gemini_investigations` audit table — unchanged from v1:
    `id, investigation_id, request_ts, trigger_reasons_json, assets_json, model_identifier, response_status, source_count, validation_status`.

---

## 8. Estimated daily call volume (retained from v1, still the grounding data)

From real production data (BTC core table + `btc_data`, last ~60 days):

- **Market-move signal** (>=3% daily range): 1 day out of 60 — rare in isolation.
- **High-confidence-failure signal, the important finding:** BTC's core model
  alone had **125 wrong predictions out of 221 at >=0.75/<=0.25 confidence — a
  56.6% failure rate — over just ~15.6 days**, roughly **8 high-confidence
  misses/day for BTC alone.**

**How the v2 design changes what this finding means:** under v1's single-
threshold design, this finding meant the trigger would immediately blow
through `MAX_GEMINI_INVESTIGATIONS_PER_DAY` from BTC alone. Under v2, this is
less immediately dangerous — `selectWithinBudget` will only ever investigate
the top-`budget` *ranked* candidates per cycle, so a flood of borderline
misses no longer forces a flood of Gemini calls. **But it's still a real
signal worth carrying forward:** with `INVESTIGATION_PRIORITY_THRESHOLD = 4`,
a wrong 0.75-confidence call in isolation (no unusual price move, no
correlation) scores `3 x 0.5 = 1.5` — below threshold, correctly skipped. Only
when confidence-adjusted error combines with a real market signal (price move,
anomaly, correlation, regime change) does it cross the bar, which is by
design. The 56.6%-wrong finding at the >=0.75 bucket remains a real calibration
issue independent of Gemini — visible in PR #1's `/api/learning/daily`
`confidence_analysis` section — not acted on here, flagged for ChatGPT.

---

## 9. Tests / specifications added in this PR

`tests/gemini-planning.test.js` — **44 tests** (up from 25 in the v1 plan),
covering:

- The 4 v1 functions (`shouldTriggerInvestigation` — now marked superseded but
  still tested, `withinGeminiRateLimit`, `validateCatalystPayload`,
  `isDuplicateCatalyst`).
- `computeInvestigationPriority` — the three worked examples above as literal
  assertions, plus: a correct call scores 0 regardless of confidence,
  confidence alone (without an error) never changes the score, a wrong
  higher-confidence call scores above a wrong lower-confidence call all else
  equal.
- `isHighInvestigationPriority`, `rankInvestigationCandidates`,
  `selectWithinBudget`, `remainingGeminiBudget` — ranking order, budget
  slicing, threshold enforcement even with free budget, budget as the min not
  the sum of daily/hourly remaining.
- `computeAvailableBeforePrediction` — all three states, the T1<=T0 boundary
  condition, and an explicit assertion that the function's arity is 2 (so it
  cannot structurally accept `event_timestamp`).

**103/103 tests passing total** (59 from PR #1 + 44 from this PR). None of the
tested functions have any side effect, D1 access, or network call.

---

## 10. What this PR deliberately does NOT do

- No Gemini API call for market investigation.
- No `evaluateGeminiTriggers`, no `investigateMarketEvent` — designed, not written.
- No new D1 table or column — both proposed, neither created.
- No wiring into `scheduled()`, `fetch()`, or any route.
- No change to `selectBestVariant`, calibration, model weights, or feature weights.
- No deployment. No merge.

---

## 11. Recommended next steps (for the implementation PR, not this one)

1. Resolve the search-grounding question (§5 step 4) against the current
   Gemini API.
2. Decide the exact trailing window for the price-move signal.
3. Add `first_public_timestamp` and `gemini_investigations` as an actual migration.
4. Revisit `INVESTIGATION_PRIORITY_WEIGHTS`/`THRESHOLD` once real trigger data
   exists — these remain provisional design choices, not validated ones.
5. Implement `evaluateGeminiTriggers`/`investigateMarketEvent`, wire into
   `scheduled()` exactly as described in §4-5, write tests for the parts that
   can't be pure functions (mocking the Gemini `fetch()` call, D1 writes).
