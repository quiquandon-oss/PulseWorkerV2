# Gemini Market Intelligence — Implementation Plan

Status: **PLANNING ONLY.** No Gemini investigation code is implemented, wired into
any route, wired into `scheduled()`, or deployed. This document plus the
deterministic helper functions in `worker.js` (clearly banner-commented as
planning-only, see below) are the entire scope of this PR.

Per `.ai/GEMINI_MARKET_INTELLIGENCE.md` and the instruction attached to this work.

---

## 1. Inspection: existing `coin_catalyst_log`

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

- **2 rows total**, both pre-existing V1 rows (`ts` between 2026-08-11 and
  2026-08-12), unrelated to this contract, new columns `NULL` on both — left
  untouched, not backfilled (see PR #1's `HISTORICAL_BACKFILL.md`).
- **Zero rows written by V2 code** — nothing in `worker.js` currently writes to
  this table. `recordCatalyst`/`fetchCatalystsForPeriod` (PR #1) are the first
  V2-side read/write helpers, and neither is called automatically anywhere.
- **Gap vs. the contract:** no `first_public_timestamp` column exists yet — only
  `discovery_timestamp`. The three-timestamp model
  (`event_timestamp` / `first_public_timestamp` / `discovery_timestamp`) needs a
  new column. `ts` is being informally used as "event timestamp" by convention,
  which is ambiguous with the three-timestamp model now required — **recommend a
  migration in the next (implementation) PR**: add `first_public_timestamp
  INTEGER`, and treat `ts` as `event_timestamp` explicitly (rename is riskier
  than documenting the mapping, given `ts` is already relied on by
  `fetchCatalystsForPeriod`'s period filter — recommend keep `ts` = event
  timestamp, add the missing column, don't rename).

---

## 2. Where market-event triggers should live

**Recommendation: a new function `evaluateGeminiTriggers(env)`, called from
`scheduled()`'s existing 3-hourly branch via its own independent
`ctx.waitUntil(...)`, positioned AFTER the six existing
`predictThenSelect(...)` calls.**

Why there, specifically:

- It needs that cycle's fresh predictions/outcomes to compute
  `priceMovePct`, `highConfidenceFailureConfidence`, and
  `correlatedFailureAssetCount` — so it has to run after prediction
  generation, not before or in parallel with it.
- It must be its own `ctx.waitUntil(...)`, sibling to the six existing ones,
  **not chained after them with `await`** — chaining would make a slow or
  failing Gemini call delay Cloudflare's report of cycle completion. The six
  existing calls are already independent of each other for the same reason;
  this should follow the same pattern, not introduce a new one.
- It must **never** be called from `/predict`, `/link-predict`, `/eth-predict`,
  or any other synchronous request path — those need to stay fast and Gemini's
  latency/availability must not affect them. This directly satisfies
  `.ai/GEMINI_MARKET_INTELLIGENCE.md`'s "must never become a dependency for
  making predictions."

```js
// Illustrative only -- NOT implemented in this PR.
} else {
  // ... existing predictThenSelect waitUntil calls ...
  ctx.waitUntil(evaluateGeminiTriggers(env).catch(err => console.error('Gemini trigger evaluation failed:', err)));
}
```

## 3. Where to actually call Gemini

**Recommendation: a new function `investigateMarketEvent(env, triggerContext)`,
called only from inside `evaluateGeminiTriggers` after `shouldTriggerInvestigation`
(already built, tested, this PR) returns `trigger: true` AND
`withinGeminiRateLimit` (already built, tested, this PR) returns `allowed: true`.**

Proposed internal flow (illustrative, not implemented):

1. `evaluateGeminiTriggers(env)` gathers the three signals from this cycle's
   fresh data (price move vs. previous cycle, any high-confidence miss just
   resolved, count of assets that missed together).
2. Call `shouldTriggerInvestigation(signals)`. If `trigger: false`, return early
   — no D1 write, no Gemini call, nothing logged (matches "do not investigate
   every normal daily price fluctuation").
3. If triggered, query today's/this-hour's investigation count (new query
   against a proposed `gemini_investigations` audit table — see §5) and call
   `withinGeminiRateLimit`. If not allowed, record a skipped-due-to-rate-limit
   note (still auditable) and return.
4. Call `investigateMarketEvent(env, { assets, window, reasons })` — this is
   the actual Gemini `fetch()` call, following the same request pattern
   already proven in `runGeminiDailyAnalysis`/`runLinkGeminiAnalysis`
   (`env.GEMINI_API_KEY`, `x-goog-api-key` header, `gemini-3.6-flash`) —
   **with one confirmed gap**: neither existing call uses search grounding.
   `.ai/GEMINI_MARKET_INTELLIGENCE.md` requires it ("Gemini should use
   web/search grounding when investigating current events"). The existing
   codebase has no proven pattern for this — the implementation PR will need
   to verify the current Gemini API's grounding tool schema (e.g. a
   `tools: [{ google_search: {} }]`-shaped request) and confirm it's still
   available on the same free tier before assuming it works the same way.
5. Validate the response with `validateCatalystPayload` (already built,
   tested, this PR) per-catalyst.
6. For catalysts that pass validation, check `isDuplicateCatalyst` (already
   built, tested, this PR) against recent rows from `fetchCatalystsForPeriod`
   (PR #1).
7. Compute `available_before_prediction` per affected prediction using
   `classifyCatalystTiming` (PR #1, already tested) — **not** a new function,
   reused exactly as-is, since Gemini must never compute this field itself
   (`.ai/GEMINI_MARKET_INTELLIGENCE.md`'s Critical Timestamp Rule).
8. Write validated, deduplicated catalysts via `recordCatalyst` (PR #1).
9. Record the investigation outcome (status, source count, trigger reason) to
   the proposed `gemini_investigations` audit table regardless of whether any
   catalyst was ultimately written — a "Gemini ran but found nothing credible"
   result is itself worth keeping, per the Auditability requirement.

## 4. Failure isolation

Every step above sits inside the single `ctx.waitUntil(evaluateGeminiTriggers(env).catch(...))`
call site — any exception anywhere in the chain is caught at that one point and
logged, never propagated to the cron handler itself. This mirrors the existing
pattern for `runGeminiDailyAnalysis`/`runLinkGeminiAnalysis`/calibration
refreshes in the same `scheduled()` function, so it's not a new failure-handling
idiom, just applying the existing one.

---

## 5. Required secrets / configuration

- **`GEMINI_API_KEY`** — already exists as a Worker secret (reused from the
  existing daily-analysis calls). No new secret needed for the API key itself.
- **New config, in-code (not secrets):** `GEMINI_TRIGGER_CONFIG` (this PR) —
  `MARKET_MOVE_TRIGGER_PCT`, `HIGH_CONFIDENCE_TRIGGER`,
  `MULTI_ASSET_TRIGGER_COUNT`, `MAX_INVESTIGATIONS_PER_DAY`,
  `MAX_INVESTIGATIONS_PER_HOUR`, `MAX_ASSETS_PER_INVESTIGATION`. Deliberately a
  plain in-code constant, not env-injected — these are tunable model-adjacent
  parameters a human should change via code review, not silently via
  environment config (consistent with how `MODEL_VERSIONS` was handled in
  PR #1).
- **Proposed schema addition (NOT created in this PR):** a `gemini_investigations`
  table for auditability —
  `id, investigation_id, request_ts, trigger_reasons_json, assets_json, model_identifier, response_status, source_count, validation_status`.
  This satisfies `.ai/GEMINI_MARKET_INTELLIGENCE.md`'s Auditability section
  without storing full prompts/responses (explicitly optional per that doc).
  Flagging this as a genuinely new table (unlike PR #1's catalyst-column
  additions, which extended something already there) that should go in the
  *implementation* PR, not this planning PR, since it has no purpose until
  something writes to it.
- **Proposed schema addition (NOT created in this PR):** `coin_catalyst_log.first_public_timestamp INTEGER` — see §1.

---

## 6. Estimated daily call volume

Computed from real production data (BTC core table + `btc_data`, last ~60
days), not assumed:

**Market-move trigger** (`MARKET_MOVE_TRIGGER_PCT = 3`, measured as daily
high/low range): over the last 60 days, **1 day out of 60** had a daily range
≥3% (average daily range 0.94%, max observed 3.94%). At face value this
trigger would fire roughly **once every ~2 months** — very rare, well within
budget. Caveat: this measures calendar-day range as a proxy; the actual
trigger will compare a trailing window each 3-hourly cron tick (8 evaluations/
day), so the true hit rate depends on the exact window chosen in the
implementation PR and could be somewhat higher than this daily-range estimate.

**High-confidence-failure trigger — this is the important finding.** Using the
doc's example threshold (`HIGH_CONFIDENCE_TRIGGER = 0.75`): BTC's core model
alone had **125 high-confidence (≥0.75 or ≤0.25) wrong predictions out of 221
total high-confidence predictions in that bucket — over a span of only ~15.6
days.** That is a **56.6% failure rate at supposed "high confidence"** (should
be well under 25% if well-calibrated), and works out to **roughly 8 high-
confidence misses per day for BTC's core model alone**, before counting LINK,
ETH, or the Challenger.

**This means the doc's example `HIGH_CONFIDENCE_TRIGGER = 0.75` would blow
through `MAX_INVESTIGATIONS_PER_DAY = 8` from BTC alone**, and that's before
LINK/ETH/Challenger are counted. Two implications, both reflected in this PR:

1. `GEMINI_TRIGGER_CONFIG.HIGH_CONFIDENCE_TRIGGER` is set to **0.85**, not the
   doc's example 0.75, in the code added by this PR — a starting point to
   bring the naive per-occurrence rate down, not a validated threshold (same
   caveat the doc itself gives for all its example numbers).
2. Even at 0.85, per-occurrence triggering is likely still too frequent given
   how poorly calibrated the ≥0.75 bucket currently is. **Recommend the
   implementation PR evaluate this trigger once per day (against the daily
   report's own `most_confident_mistakes`, already computed by PR #1's
   learning engine) rather than once per occurrence** — i.e., "did today
   produce an unusually bad confident miss" rather than "did this specific
   prediction miss confidently." This wasn't in the original doc and is a
   direct consequence of this data, flagged here for ChatGPT/human review
   rather than silently decided.
3. Separately, worth surfacing regardless of Gemini: a 56.6% failure rate in
   the ≥0.75 confidence bucket is a real calibration finding on its own,
   independent of anything Gemini-related — visible right now in
   `/api/learning/daily`'s `confidence_analysis` section (PR #1). Not acted on
   here — this PR's scope is Gemini planning, not a model experiment — but
   worth ChatGPT seeing directly rather than only through this document.

**Multi-asset correlated-failure trigger:** not separately estimated this pass
— would require joining BTC/LINK/ETH failure windows, deferred to the
implementation PR once the single-asset thresholds above are settled, since
this trigger is explicitly meant to be rarer/stronger evidence than either
trigger alone.

**Net estimate, once thresholds are corrected per above:** roughly **0-2
investigations/day** in a normal period, with the `MAX_INVESTIGATIONS_PER_DAY = 8`
ceiling acting as a real safety margin rather than the binding constraint it
would be under the doc's original example threshold.

---

## 7. Tests / specifications added in this PR

`tests/gemini-planning.test.js` — 25 tests covering the four pure functions
added:

- `shouldTriggerInvestigation` — each of the three trigger conditions fires
  independently, multiple reasons reported together, custom config respected.
- `withinGeminiRateLimit` — daily and hourly limits both enforced independently.
- `validateCatalystPayload` — every allowed category/classification accepted,
  invalid ones rejected (including a simulated "hallucinated category" case),
  malformed source URLs rejected, impossible timestamp ordering rejected with
  clock-skew tolerance, optional fields genuinely optional.
- `isDuplicateCatalyst` — same coin+category within the tolerance window
  flagged; different coin, different category, or outside the window not
  flagged.

All 84 tests pass (59 from PR #1 + 25 new). None of the tested functions have
any side effect, D1 access, or network call — they're pure input→output,
which is why they can be fully specified and tested before the actual
integration exists.

---

## 8. What this PR deliberately does NOT do

- No Gemini API call for market investigation (only the pre-existing daily
  analysis calls, unchanged).
- No `evaluateGeminiTriggers`, no `investigateMarketEvent` — described above,
  not written.
- No new D1 table (`gemini_investigations` proposed, not created).
- No new D1 column (`first_public_timestamp` proposed, not created).
- No wiring into `scheduled()`, `fetch()`, or any route.
- No deployment.
- No merge.

---

## 9. Recommended next steps (for the implementation PR, not this one)

1. Resolve the search-grounding question (§3 step 4) against the current
   Gemini API before writing the real prompt/request code.
2. Decide the exact trailing window for the market-move trigger (§6 caveat).
3. Decide daily-aggregate vs. per-occurrence evaluation for the
   high-confidence trigger (§6, point 2) — recommend daily-aggregate.
4. Add the `first_public_timestamp` column and `gemini_investigations` table
   as an actual migration.
5. Implement `evaluateGeminiTriggers` / `investigateMarketEvent`, wire into
   `scheduled()` exactly as described in §2-3, write tests for the parts that
   can't be pure functions (mocking the Gemini `fetch()` call, D1 writes).
