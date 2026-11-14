# CryptoPulse Sentiment V2 — End-to-End Build and Deployment Specification

**Status:** Approved design direction; implementation not started  
**Owner:** CryptoPulse project owner  
**Implementation agent:** Claude Code, with Codex delegated for independent review and bounded tasks  
**Target:** Existing CryptoPulse V1 website, with V2 computation added to the existing Worker/data platform  
**Hard constraint:** Zero additional spend. No paid APIs, GPU rental, subscriptions, paid inference fallback, or automatic plan upgrades.

---

## 1. Product objective

Build a new AI-assisted Sentiment V2 alongside the existing Sentiment V1.

V1 must remain available and continue operating. V2 must be added as a new tab in the existing CryptoPulse V1 website. The user-facing comparison must be deliberately simple:

- V1 sentiment line
- V2 sentiment line
- BTC price line
- Sentiment scale 0–100 on the left axis
- BTC USD price on the right axis
- Selectable periods: 24H, 7D, 30D, 90D, ALL
- No technical model explanations or complex research tables in the primary comparison view

The purpose is to let the owner visually compare how V1 and V2 behave against BTC over time.

Do not invent historical V2 observations. V2 history begins when V2 actually starts producing timestamped results. Any reconstructed backtest must be labelled separately and must only use information available at the historical decision timestamp.

## 2. Repositories and ownership

The implementation spans two existing repositories:

1. **`quiquandon-oss/PulseWorkerV2`** — primary backend repository.
   - V2 sentiment computation and orchestration
   - AI/model adapters
   - source-family and event-intelligence logic
   - D1 schema/migrations and persistence
   - API endpoints
   - scheduled execution and usage controls
   - backend tests, deployment and operational documentation

2. **`quiquandon-oss/CryptoPulseV2`** — existing frontend repository.
   - New Sentiment V2 tab
   - V1/V2/BTC comparison graph
   - period controls and loading/error states
   - API integration
   - frontend tests and deployment

Keep the authoritative implementation specification in `CryptoPulseV2` at:
`docs/SENTIMENT_V2_BUILD_SPEC.md`

Copy or link the same specification into `PulseWorkerV2` at:
`docs/SENTIMENT_V2_BUILD_SPEC.md`

The Worker repository owns backend implementation. The frontend repository owns presentation. Do not duplicate business logic between them.

## 3. Zero-cost policy — non-negotiable

No implementation may create a paid dependency.

Prohibited:
- OpenAI, Anthropic, Gemini or other paid inference APIs
- paid Hugging Face inference
- GPU rental or hosted GPU endpoints
- paid model subscriptions
- paid database or hosting upgrades
- paid fallback if the free model quota is exhausted

Permitted only within currently available free allocations:
- Existing Cloudflare Worker and D1 plan
- Cloudflare Workers AI free allocation, if the chosen model is actually available to this account/plan
- Open-source local statistical libraries
- GitHub Actions within the repository's actual included quota and applicable GitHub terms
- Public model weights under a licence compatible with this project's use

Before selecting a hosted model, Claude must verify the current Cloudflare Workers AI model catalogue, exact model identifier, account eligibility, free quota, and model licence. Do not assume Qwen3-30B-A3B is available under the Free allocation merely because it appears in documentation or the model catalogue.

If the chosen model is unavailable or free quota is exhausted:
- V2 must continue with the deterministic/statistical components that can run within existing free resources, or mark the AI interpretation component unavailable for that cycle.
- Never switch to a paid service.
- Never silently exceed a quota.
- Never ask for or configure a paid API key.
- Expose an understandable degraded status, not a fabricated AI result.

Add an explicit usage guard and daily budget ceiling below the actual free allowance. Keep a reserve for existing production operations. No automatic plan changes.

## 4. V2 intelligence logic to incorporate

V2 should incorporate the logic and research direction of the previous experiments, including hypotheses whose predictive value is not yet proven. Do not represent unproven hypotheses as validated facts. Include them as explicit candidate components and record their influence so the V1/V2 comparison can show what they contribute.

### 4.1 Source intelligence
Use existing V1 observations and available research to:
- group sources into information families
- identify duplicated or highly correlated information
- distinguish source availability, freshness, reliability, coverage and predictive usefulness
- record source provenance and observation timestamps
- avoid treating missing or stale observations as fresh neutral readings

### 4.2 Information families
At minimum, organize available sources into:
- market sentiment and news
- derivatives and positioning
- institutional flows and ETFs
- on-chain activity
- macroeconomic conditions
- cross-asset risk appetite / equities
- market structure and breadth
- regulation and geopolitics
- corporate treasury / crypto-specific catalysts

Use only sources actually available in the current data. Do not invent feeds or imply coverage that does not exist.

### 4.3 Event intelligence
Integrate the existing Event Detection and evidence/research pipeline:
- detected event type and asset relevance
- event occurrence time and first-detected time
- source provenance and evidence quality
- direction, severity, novelty and persistence
- deduplication / related-event grouping
- whether the event appears to be a cause, consequence or coincident observation
- whether its information overlaps existing V1 source families
- subsequent BTC response at available horizons

Preserve the distinction between event detection and predictive usefulness. A correctly detected event is not automatically a predictive signal.

### 4.4 Macro and cross-market context
Include the existing macro, USD, yields, oil, equities, Nasdaq, S&P 500, Magnificent 9 and other actually available cross-asset context. Assess direction, persistence, regime and relevance to crypto rather than simply adding another independent weighted score.

### 4.5 Historical learning and market regimes
Include:
- historical analogues using point-in-time data
- bullish, bearish, sideways and stress/high-volatility regime classification
- source-family effectiveness by regime
- event reaction and priced-in analysis
- subsequent BTC outcomes at available horizons
- failed as well as successful prior signals
- uncertainty and sample-size awareness

### 4.6 Forecasting and statistical research
Incorporate previous research logic and candidates:
- TimesFM results and methodology
- Chronos-2 as a forecasting challenger
- TiRex-2 as a forecasting challenger
- LightGBM / XGBoost-style tabular learning where appropriate
- HMM or another lightweight regime classifier
- EXP-002 differentiation/evaluator logic
- prospective evaluation methodology such as PEV-1P
- source redundancy and incremental-value testing

Do not require all large forecasting models to run continuously in the Worker. They can remain research challengers where free compute and compatible licensing permit. V2's live result must not depend on paid compute or an unavailable GPU.

### 4.7 AI reasoning
Use a free model only if its exact current hosting route and free allowance are verified. The AI should:
- interpret a compact, timestamped evidence package
- explain contradictory evidence
- assess event relevance and source overlap
- return schema-validated structured output
- cite input observation/event identifiers
- distinguish observed facts, inference and uncertainty

The AI must not invent numerical source readings, fabricate events, or generate a sentiment result without traceable input evidence.

## 5. V2 outputs

Persist a V2 result for each successful cycle with at least:

- asset
- decision timestamp
- calculation/model version
- sentiment score, 0–100
- sentiment direction/classification
- confidence/evidence coverage
- market regime
- event intensity / event assessment
- predictive signal as a separate field from sentiment
- source-family contributions
- major supporting evidence IDs
- contradictory evidence IDs
- missing/stale input summary
- AI model identifier and status, if invoked
- free-quota/usage accounting
- execution status and error/degraded reason

Do not collapse sentiment, confidence, event severity and predictive probability into one number. They are distinct outputs.

## 6. V1 and V2 parallel operation

- V1 continues using its current production calculation and history.
- V2 writes to separate V2 tables/records.
- V2 must not overwrite V1 sentiment, weights, predictions or history.
- Both results must use aligned decision timestamps for fair comparison.
- Preserve V1 values exactly as served/stored; do not retrospectively rewrite them.
- Record gaps where either series is unavailable.
- The frontend comparison graph must use actual stored observations only.

## 7. Frontend requirements

Add a clearly named **Sentiment V2** tab to the existing CryptoPulse V1 frontend.

Primary view:
- One graph only as the central visual.
- V1 sentiment: blue.
- V2 sentiment: green.
- BTC price: orange.
- Sentiment left axis, 0–100.
- BTC price right axis, USD.
- Period selectors: 24H, 7D, 30D, 90D, ALL.
- Shared time axis and aligned timestamps.
- Tooltip showing timestamp, V1, V2 and BTC price.
- Clear indication when V2 has insufficient history.
- Clear indication when a data point is missing or a series is stale.
- Mobile responsive.
- Do not add a complicated technical explanation to the primary screen.

The tab may include a small status line for last update and V2 availability, but the graph is the main deliverable.

## 8. Backend and API requirements

Claude must inspect the existing Worker architecture and conventions before deciding exact routes or schema.

Implement:
- separate V2 result persistence
- minimal API endpoint returning aligned V1, V2 and BTC series
- period filtering and bounded response sizes
- efficient indexed queries
- caching consistent with current Worker patterns
- scheduled execution consistent with existing production cadence, subject to free quota and measured resource use
- graceful degraded behaviour when AI is unavailable
- idempotency so retries do not create duplicate cycle results

Do not invent table names or API route names before inspecting existing code. Follow current repository conventions.

## 9. Testing and quality

Add tests for:
- V2 score and output schema
- source-family aggregation
- duplicated/redundant inputs
- stale and missing sources
- event timestamps and deduplication
- event relevance and overlap
- AI output schema validation and malformed responses
- AI quota exhaustion / unavailable model
- zero-paid-fallback guarantee
- idempotency
- V1 calculation and outputs unchanged
- V1/V2/BTC timestamp alignment
- API periods and empty history
- frontend graph rendering, legends, axes, tooltip and mobile layout
- degraded/error/loading states

Run all existing Worker and frontend test suites. Do not reduce, skip or weaken existing tests to make the build pass.

## 10. Build sequence

1. Inspect both repositories and identify current architecture, deployment workflows, D1 bindings, schemas, source APIs and frontend tab/navigation conventions.
2. Produce a concise implementation map and list any genuine blockers. Do not change production during discovery.
3. Verify free-tier eligibility, exact AI model availability, licence and quota. If any assumption fails, select a genuinely free alternative or deterministic fallback.
4. Implement backend schema and V2 computation in a feature branch of `PulseWorkerV2`.
5. Implement event/source intelligence and AI integration with strict free usage controls.
6. Add V2 persistence and API; test locally and against staging.
7. Implement the V2 tab and graph in a feature branch of `CryptoPulseV2`.
8. Run complete backend and frontend test suites.
9. Have Codex perform an independent review of the changed code, focusing on correctness, security, data leakage, cost enforcement, D1 usage, API contracts and preservation of V1.
10. Claude addresses valid findings and reruns tests. Codex verifies fixes.
11. Deploy backend first using the existing approved deployment process; verify live API and stored V2 observations.
12. Deploy frontend; verify the V2 tab and graph against real V1, V2 and BTC records.
13. Verify V1 still works and its values have not changed.
14. Deliver deployment commits, PR links, test counts, live URLs, actual model/quota usage and any remaining limitations.

## 11. Deployment and approval

- Follow the existing repository branch protection and CI requirements.
- Do not bypass required checks.
- Do not deploy from an unreviewed working branch.
- Use the existing staging/production workflow and approval mechanism.
- Claude must not claim deployment until the live Worker and website have been independently verified.
- If production approval is required, stop and request it rather than bypassing it.
- No paid plan or billing change is permitted.

## 12. Completion criteria

The work is complete only when:
- V1 remains active and unchanged.
- V2 produces and stores real timestamped results.
- V2 uses the integrated research logic listed above.
- AI inference, when available, operates within verified free quota.
- No paid service or paid fallback exists.
- The existing website has a working Sentiment V2 tab.
- The graph compares actual V1, V2 and BTC data on aligned timestamps.
- Missing history and degraded AI status are represented honestly.
- All existing and new tests pass.
- Codex has reviewed the final implementation and Claude has addressed valid findings.
- Backend and frontend are deployed and verified live.

---

# Part B — Claude Code and Codex working agreement

## Claude's role
Claude is the primary implementation lead:
- inspect and plan
- coordinate backend and frontend changes
- implement
- run tests
- deploy through existing workflows
- verify live behaviour
- maintain this specification as the source of truth

## Codex's role
Codex is the independent engineering reviewer and bounded task executor:
- read-only review of architecture and diffs
- identify bugs, edge cases, security problems, performance regressions and missing tests
- optionally implement narrowly scoped fixes only when Claude explicitly delegates a bounded task
- never independently deploy or merge
- report findings with file and line references
- re-review fixes

## Model/task routing
Use the least expensive Claude model that can reliably perform the task:
- small/fast model: repository navigation, documentation, simple tests, formatting and bounded mechanical changes
- mid-tier model: ordinary implementation, API integration and test writing
- strongest available model: architecture decisions, difficult debugging, data correctness, security, migrations and final deployment review

Do not use the strongest model for every task. Claude should state when a task needs escalation rather than silently spending high-tier capacity.

## Required handoff
For every Codex delegation, Claude supplies:
- repository and branch
- exact files/scope
- task and acceptance criteria
- read-only or edit permission
- commands/tests to run
- explicit instruction not to deploy, merge or broaden scope

Claude remains responsible for reconciling Codex feedback and the final result.
