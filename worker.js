// ---- BTC k-NN historical analog model ----
// Feature vector: the 4 headline gauges already computed and logged by the
// original CryptoPulse pipeline every cycle (sentiment composite, technical
// score, regime magnitude, market-bottom score) — the same inputs the
// Dashboard's own Conclusion banner uses, not an arbitrary new feature set.
// z-score normalized before distance so no single feature dominates purely
// from having a wider numeric range (regime_mag is roughly -1..1, the
// others are 0..100).
//
// Deliberately NOT logistic regression yet — see CryptoPulseV2 README for
// why analog matching is the honest starting point at this data volume.
const FEATURE_KEYS = ['score', 'technical_score', 'regime_mag', 'bottom_score'];
const LAG_MS = 24 * 60 * 60 * 1000; // 24h prediction horizon
const TOL_MS = LAG_MS * 0.2;        // matching tolerance for "nearest row to t+24h", same convention as the existing /analysis endpoint on the original PulseWorker
const MIN_COMPLETE_ROWS = 30;       // below this, refuse to predict rather than overfit noise
const MIN_RESOLVED_ANALOGS = 5;     // below this among the k neighbors, refuse rather than report a probability built on almost nothing

// ---- Continuous-learning ledger: model identity ----
// First formal version tags introduced alongside the prediction ledger
// (see .ai/DATA_CONTRACT.md). Prediction LOGIC is unchanged by this —
// these strings identify "the k-NN core model as it exists today", not a
// new model. Bump the relevant string only when that model's actual
// decision logic changes (feature set, K rule, calibration method, etc.),
// not for unrelated infra work.
const MODEL_VERSIONS = {
  btc_core: 'knn-core-v1',
  link_core: 'knn-core-v1',
  eth_core: 'knn-core-v1-selfcontained', // distinct tag: self-contained feature set, deliberately not sharing BTC's borrowed-context pattern
  challenger: 'regime-trend-v1',
};

// git_commit_sha is captured from an environment var set at deploy time
// (see wrangler.toml [vars] + .github/workflows/deploy.yml), not computed
// at runtime -- Workers have no git access. Falls back to 'unknown' for
// local/test environments where the var isn't injected, never fabricated.
function currentGitSha(env) {
  return (env && env.GIT_COMMIT_SHA) ? env.GIT_COMMIT_SHA : 'unknown';
}

function meanStd(vals) {
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
  const variance = vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length;
  return { mean, std: Math.sqrt(variance) || 1 };
}

// Trailing realized volatility (stddev of period-over-period % moves) over
// the last `lookback` rows ending at (and including) `endIdx` in a
// chronologically-sorted array. Used both for today's own volatility and,
// retrospectively, to build a reference distribution of "what volatility
// has looked like historically" so today's reading can be percentile-ranked
// against it rather than compared to an arbitrary hardcoded threshold.
function trailingVolatility(sortedRows, endIdx, lookback = 14, priceField = 'btc_price') {
  const start = Math.max(0, endIdx - lookback + 1);
  const window = sortedRows.slice(start, endIdx + 1);
  if (window.length < 4) return null;
  const rets = [];
  for (let i = 1; i < window.length; i++) {
    rets.push((window[i][priceField] - window[i - 1][priceField]) / window[i - 1][priceField] * 100);
  }
  return meanStd(rets).std;
}

// Percentile rank of `value` within `referenceValues` (0 = lowest ever seen,
// 1 = highest). Used for both the volatility-based K adjustment and the
// regime-anomaly distance check — same self-calibrating idea either way:
// judge today against this dataset's own history, not a fixed number picked
// in advance with no basis.
function percentileRank(value, referenceValues) {
  const valid = referenceValues.filter(v => v != null);
  if (valid.length < 10) return null; // too few points for a percentile to mean anything
  const below = valid.filter(v => v <= value).length;
  return below / valid.length;
}

// Weighted median: same idea as an unweighted median, but each observation
// counts in proportion to its weight (here, inverse distance) rather than
// counting once each. Standard weighted-quantile construction: sort by
// value, walk the accumulated weight until it crosses the target fraction.
function weightedQuantile(pairs, p) {
  const sorted = [...pairs].sort((a, b) => a.value - b.value);
  const total = sorted.reduce((s, x) => s + x.weight, 0);
  let acc = 0;
  for (const x of sorted) {
    acc += x.weight;
    if (acc / total >= p) return x.value;
  }
  return sorted[sorted.length - 1].value;
}

// Nearest row to targetTs within tolMs (defaults to the 24h-derived TOL_MS
// for every call site that isn't horizon-specific — enrichment joins, etc.).
// Horizon-specific forward-return lookups pass their own tolerance now that
// 12h predictions need a tighter matching window than 24h ones.
function nearestRow(history, targetTs, tolMs = TOL_MS) {
  let best = null, bestDiff = Infinity;
  for (const r of history) {
    const diff = Math.abs(r.ts - targetTs);
    if (diff <= tolMs && diff < bestDiff) { bestDiff = diff; best = r; }
  }
  return best;
}

// Trend-strength guardrail. MA-slope based (no external deps, Workers-safe).
// Returns -1 (strong downtrend) to +1 (strong uptrend), 0 = no clear trend.
function trendStrength(sortedRows, priceField, shortN = 8, longN = 21) {
  if (sortedRows.length < longN + 1) return 0;
  const recent = sortedRows.slice(-longN);
  const avg = (arr) => arr.reduce((a, r) => a + r[priceField], 0) / arr.length;
  const shortMA = avg(recent.slice(-shortN));
  const longMA = avg(recent);
  if (!longMA) return 0;
  const slope = (shortMA - longMA) / longMA;
  return Math.max(-1, Math.min(1, slope * 20)); // capped, bounded multiplier not a raw %
}

// Applies only when a strong trend disagrees with the model's own lean —
// dampens further (does not flip the call outright, same conservatism as
// the anomaly shrink it sits alongside).
function applyTrendGuardrail(pUp, trend) {
  if (trend == null) return pUp;
  const disagreesWithUp = pUp < 0.5 && trend > 0.5;
  const disagreesWithDown = pUp > 0.5 && trend < -0.5;
  if (disagreesWithUp || disagreesWithDown) {
    return 0.5 + (pUp - 0.5) * 0.5;
  }
  return pUp;
}

// Decile-bucket empirical recalibration (hand-rolled — no isotonic
// regression lib available in Workers). Rebuilt periodically from resolved
// predictions; buckets under 10 samples are dropped as untrustworthy.
function buildCalibrationCurve(resolvedRows) {
  const sorted = [...resolvedRows].sort((a, b) => a.p_up - b.p_up);
  const deciles = [];
  const bucketSize = Math.ceil(sorted.length / 10);
  for (let i = 0; i < 10; i++) {
    const bucket = sorted.slice(i * bucketSize, (i + 1) * bucketSize);
    if (bucket.length < 10) continue;
    const midP = bucket.reduce((s, r) => s + r.p_up, 0) / bucket.length;
    const empiricalUp = bucket.reduce((s, r) => s + r.realized_up, 0) / bucket.length;
    deciles.push({ decile: i, predicted_p_up_mid: midP, empirical_up_rate: empiricalUp, n_samples: bucket.length });
  }
  return deciles;
}

// Additive, never replaces the raw model output — falls back to rawPUp if
// no curve exists yet or the nearest bucket is too thin to trust.
function applyCalibratedProbability(rawPUp, curveRows) {
  if (!Array.isArray(curveRows) || curveRows.length === 0) return rawPUp;
  if (typeof rawPUp !== 'number' || rawPUp < 0 || rawPUp > 1) return rawPUp;
  let closest = curveRows[0], bestDiff = Infinity;
  for (const row of curveRows) {
    const diff = Math.abs(row.predicted_p_up_mid - rawPUp);
    if (diff < bestDiff) { bestDiff = diff; closest = row; }
  }
  return closest.n_samples >= 10 ? closest.empirical_up_rate : rawPUp;
}

// Pulls the most recent computed batch for this coin/horizon. Returns []
// (not null) when nothing exists yet — applyCalibratedProbability already
// treats an empty array as "no curve, use raw p_up".
async function getLatestCalibrationCurve(env, coin, horizonHours) {
  const { results } = await env.DB.prepare(
    `SELECT decile, predicted_p_up_mid, empirical_up_rate, n_samples FROM calibration_curve
     WHERE coin = ? AND horizon_hours = ? AND computed_ts = (
       SELECT MAX(computed_ts) FROM calibration_curve WHERE coin = ? AND horizon_hours = ?
     )`
  ).bind(coin, horizonHours, coin, horizonHours).all();
  return results || [];
}

// Challenger's own version of the above two functions -- direct build for
// the stated goal: the final model must adjust based on accumulated
// experience, not stay fixed on constants chosen once from a single past
// analysis. Every number in runChallengerPrediction (the 0.5 anomaly-
// shrink, the trend-guardrail threshold, the +-0.10 tilt) was exactly that
// kind of frozen, one-time-chosen constant before this. This doesn't
// replace those constants -- it adds a genuinely adaptive layer on top,
// tracked as its own new variant (see calibrated_p_up_flat below), proven
// or not over real time, never silently promoted to "the" Challenger
// prediction the way the core model's calibrated_p_up was left unproven
// and unused for months.
async function getLatestChallengerCalibrationCurve(env, coin, horizonHours) {
  const { results } = await env.DB.prepare(
    `SELECT decile, predicted_p_up_mid, empirical_up_rate, n_samples FROM challenger_calibration_curve
     WHERE coin = ? AND horizon_hours = ? AND computed_ts = (
       SELECT MAX(computed_ts) FROM challenger_calibration_curve WHERE coin = ? AND horizon_hours = ?
     )`
  ).bind(coin, horizonHours, coin, horizonHours).all();
  return results || [];
}

async function refreshChallengerCalibrationCurve(env, coin, horizonHours) {
  const { results } = await env.DB.prepare(
    `SELECT p_up_flat as p_up, realized_up FROM challenger_predictions WHERE coin=? AND horizon_hours=? AND realized_up IS NOT NULL`
  ).bind(coin, horizonHours).all();
  if (results.length < 20) {
    return { ok: true, coin, horizon_hours: horizonHours, status: 'insufficient_data', n_resolved: results.length, min_required: 20 };
  }
  const curve = buildCalibrationCurve(results); // same function, same deciles/threshold as the core model's curve
  const computedTs = Date.now();
  for (const row of curve) {
    await env.DB.prepare(
      `INSERT INTO challenger_calibration_curve (coin, horizon_hours, decile, predicted_p_up_mid, empirical_up_rate, n_samples, computed_ts)
       VALUES (?,?,?,?,?,?,?)`
    ).bind(coin, horizonHours, row.decile, row.predicted_p_up_mid, row.empirical_up_rate, row.n_samples, computedTs).run();
  }
  return { ok: true, coin, horizon_hours: horizonHours, status: 'ok', n_resolved: results.length, n_buckets: curve.length, computed_ts: computedTs };
}

const HISTORY_FRESHNESS_MS = 48 * 60 * 60 * 1000; // beyond this, a "history" match is too stale to trust as real, falls back to imputed

// Evidence-based per-feature weights for BTC's conditional calibration.
// Derived from a real audit, not chosen a priori: checked each feature's
// actual discriminative power against 376 resolved BTC 24h predictions.
// regime_mag showed the strongest split (57.4% vs 38.3% up-rate, a 19.1pt
// spread) -- nearly double technical_score's 9.7pt spread. bottom_score
// showed ZERO variance across the entire window (100% of rows fell in one
// bucket -- BTC hasn't been near a cycle bottom in this window), meaning
// it's currently pure noise in the distance computation, diluting the
// genuinely useful signals for no benefit. Not zeroed entirely -- plausible
// it becomes informative again near a real cycle bottom, and zeroing based
// on one evaluation window would be its own overcorrection.
const CONDITIONAL_CALIB_WEIGHTS = { score: 1.0, technical_score: 1.0, regime_mag: 1.5, bottom_score: 0.3 };
const CONDITIONAL_CALIB_K = 30;
const CONDITIONAL_CALIB_MIN_NEIGHBORS = 20;

// Pure function -- given today's feature vector and already-fetched
// historical resolved predictions (each with their own stored feature
// vector and realized outcome), computes a condition-matched calibrated
// probability. Direct fix for a confirmed finding, not a hypothesis:
// global decile calibration was shown to blend two populations with
// OPPOSITE calibration needs (non-anomalous bearish calls that were
// already well-calibrated raw, Brier 0.16, vs anomalous bearish calls that
// genuinely needed correcting) into one curve -- actively hurting the
// population that didn't need correcting (Brier ballooned to 0.55 after
// "correction"). This asks a locally-scoped question instead: "among
// historical moments whose CONDITIONS most resemble today's, what fraction
// actually went up" -- same underlying idea as the Condition-Matched
// Selection layer, applied to calibration instead of variant-choice.
// Extracted separately from D1 fetching specifically so this, the actual
// statistical core, is unit-testable without mocking a database.
function computeConditionalCalibration(todayFeatures, historicalRows, weights, k, minNeighbors) {
  const featureKeys = Object.keys(weights);
  const stats = {};
  for (const fk of featureKeys) {
    const vals = historicalRows.map(r => r.features[fk]).filter(v => v != null);
    if (vals.length < 5) continue;
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const std = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length) || 1;
    stats[fk] = { mean, std };
  }
  const usableKeys = featureKeys.filter(fk => stats[fk] && todayFeatures[fk] != null);
  if (!usableKeys.length) return null;

  const distances = historicalRows.map(r => {
    let d = 0;
    for (const fk of usableKeys) {
      if (r.features[fk] == null) continue;
      const z1 = (todayFeatures[fk] - stats[fk].mean) / stats[fk].std;
      const z2 = (r.features[fk] - stats[fk].mean) / stats[fk].std;
      d += weights[fk] * (z1 - z2) ** 2;
    }
    return { dist: Math.sqrt(d), realized_up: r.realized_up };
  }).sort((a, b) => a.dist - b.dist);

  const neighbors = distances.slice(0, k);
  if (neighbors.length < minNeighbors) return null;
  const empiricalUpRate = neighbors.reduce((s, n) => s + n.realized_up, 0) / neighbors.length;
  return { p_up: empiricalUpRate, n_neighbors: neighbors.length };
}

// =====================================================================
// ---- Read-only live traffic (data-fix only, no model/selection logic
// touched) ----
// Root cause traced and confirmed: GET /predict, /link-predict,
// /eth-predict each unconditionally wrote a fresh row to btc_data/
// link_data/eth_data (via logXData) AND a fresh row to predictions/
// link_predictions/eth_predictions/challenger_predictions (via each
// runXPrediction's own INSERT), on every single page view -- in addition
// to the cron already doing the same thing once per horizon per 3h
// cycle. Confirmed empirically: ~21-35% of rows in these tables are
// exact-value duplicates, and ~43% of real k-NN neighbor slots in a
// sampled dry-run were exact duplicates of another neighbor, materially
// distorting n_matched and the LCA gate's effective evidence count (see
// the temporal-dependence and LCA-gate audits).
//
// Fix: the cron is now the only path that unconditionally writes. Live
// routes default to persist:false -- they still COMPUTE a live,
// accurate prediction every time (reading whatever btc_data/predictions
// state currently exists, same as before), they just don't ALSO persist
// that computation as a new row. The one exception is the staleness
// fallback below: if the cron has genuinely stalled (no fresh row in
// over 6h, double the normal ~3h cadence), a live call is allowed to
// write once, so predictions don't silently serve stale data forever
// just because nobody visits the page during an outage.
//
// Nothing about the k-NN distance, feature weights, LCA gate,
// MIN_MATCHED, or selection thresholds is touched anywhere in this
// section -- this only ever gates WHETHER a row gets written, never HOW
// a prediction or selection is computed.
// =====================================================================
async function isRecentDataStale(env, table, maxAgeMs = 6 * 60 * 60 * 1000) {
  // table is only ever called with a hardcoded literal from within this
  // file (btc_data/link_data/eth_data), never user input -- same
  // accepted pattern already used elsewhere in this codebase for
  // table-name interpolation (see coreTableForCoin's callers).
  const row = await env.DB.prepare(`SELECT MAX(ts) as latest FROM ${table}`).first();
  if (!row || row.latest == null) return true;
  return (Date.now() - row.latest) > maxAgeMs;
}

// ---- Atomic claim for the staleness fallback ----
// Confirmed by audit (concurrency test against this exact PR branch):
// isRecentDataStale + a plain conditional write is a check-then-act race
// -- two or more concurrent live requests arriving during a genuine cron
// stall would each independently see stale data and each write their own
// fallback snapshot, reproducing the very duplicate-row bug this whole
// change was built to fix, just gated behind a rarer trigger.
//
// Fix follows the exact pattern reserveGeminiQuotaSlot already uses for
// the same underlying problem (only one concurrent caller may proceed):
// idempotent row creation (ON CONFLICT DO NOTHING -- a race here is
// harmless, the row's exact initial value doesn't matter, only that it
// exists), then a single conditional UPDATE whose WHERE clause only
// matches for whichever caller's write actually lands first at the
// database. D1/SQLite serializes writes to the same row, so at most one
// concurrent caller's UPDATE can ever see claimed_ts still outside the
// window -- every other caller's WHERE clause fails to match, RETURNING
// gives them zero rows, and they correctly do not write.
//
// claimWindowMs (60s) only needs to safely outlast one fallback write's
// own duration, not the 6h staleness threshold itself -- it prevents two
// requests arriving milliseconds apart from both winning, not requests
// arriving minutes apart (by then the fallback write already landed,
// isRecentDataStale will correctly see fresh data, and this function
// won't even be called again for that window).
async function claimStaleRefresh(env, coin, nowTs, claimWindowMs = 60 * 1000) {
  await env.DB.prepare(
    `INSERT INTO stale_refresh_claim (coin, claimed_ts) VALUES (?, 0) ON CONFLICT(coin) DO NOTHING`
  ).bind(coin).run();

  const result = await env.DB.prepare(
    `UPDATE stale_refresh_claim SET claimed_ts = ?
     WHERE coin = ? AND claimed_ts <= ? RETURNING coin`
  ).bind(nowTs, coin, nowTs - claimWindowMs).all();

  return result.results.length > 0;
}

async function runPrediction(env, horizonHours = 24, { persist = true } = {}) {
  const lagMs = horizonHours * 60 * 60 * 1000;
  const tolMs = lagMs * 0.2;

  const { results: btcRows } = await env.DB.prepare(
    'SELECT ts, btc_price, technical_score FROM btc_data ORDER BY ts ASC'
  ).all();
  if (btcRows.length < MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: btcRows.length, min_required: MIN_COMPLETE_ROWS };
  }

  // Optional enrichment from V1's history table — sentiment/regime/bottom
  // score aren't things this Worker can cheaply replicate (V1's composite
  // is a ~19-source weighted engine, not worth duplicating for a bonus
  // feature). But V1's history only updates when someone opens V1 in a
  // browser, so a stale match here must NOT silently be used as if it were
  // current — freshness-checked per row, falls back to the dataset's own
  // mean when missing or too old, same technique already proven for LINK's
  // borrowed BTC context.
  const { results: history } = await env.DB.prepare(
    'SELECT ts, score, regime_mag, bottom_score FROM history ORDER BY ts ASC'
  ).all();

  const rawRows = [];
  const realScoreVals = [], realRegimeVals = [], realBottomVals = [];
  for (const r of btcRows) {
    if (r.technical_score == null) continue;
    const nearestHist = nearestRow(history, r.ts);
    const fresh = nearestHist && Math.abs(nearestHist.ts - r.ts) <= HISTORY_FRESHNESS_MS;
    rawRows.push({
      ts: r.ts, btc_price: r.btc_price, technical_score: r.technical_score,
      score: fresh ? nearestHist.score : null,
      regime_mag: fresh ? nearestHist.regime_mag : null,
      bottom_score: fresh ? nearestHist.bottom_score : null,
    });
    if (fresh) {
      if (nearestHist.score != null) realScoreVals.push(nearestHist.score);
      if (nearestHist.regime_mag != null) realRegimeVals.push(nearestHist.regime_mag);
      if (nearestHist.bottom_score != null) realBottomVals.push(nearestHist.bottom_score);
    }
  }
  const meanOf = (arr, fallback) => arr.length ? arr.reduce((a, b) => a + b, 0) / arr.length : fallback;
  const meanScore = meanOf(realScoreVals, 50);
  const meanRegime = meanOf(realRegimeVals, 0);
  const meanBottom = meanOf(realBottomVals, 25);
  const complete = rawRows.map(r => ({
    ts: r.ts, btc_price: r.btc_price, technical_score: r.technical_score,
    score: r.score ?? meanScore,
    regime_mag: r.regime_mag ?? meanRegime,
    bottom_score: r.bottom_score ?? meanBottom,
    context_imputed: r.score == null || r.regime_mag == null || r.bottom_score == null,
  }));
  if (complete.length < MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: complete.length, min_required: MIN_COMPLETE_ROWS };
  }

  const stats = {};
  for (const k of FEATURE_KEYS) stats[k] = meanStd(complete.map(r => r[k]));

  const today = complete[complete.length - 1];
  // Exclude candidates too recent to possibly have a resolved forward
  // outcome yet (their own +lagMs point would land after "today", which
  // doesn't exist) — otherwise a dense recent cluster (heavy testing,
  // frequent live snapshots) can crowd every k-NN slot with unresolvable
  // candidates even though genuinely resolvable ones exist further back.
  // Confirmed happening for real: a live run returned 15 neighbors, 0
  // resolved, entirely because of this.
  const candidates = complete.slice(0, -1).filter(r => r.ts <= today.ts - (lagMs + tolMs));

  const distances = candidates.map(r => {
    let d = 0;
    for (const k of FEATURE_KEYS) {
      const z1 = (today[k] - stats[k].mean) / stats[k].std;
      const z2 = (r[k] - stats[k].mean) / stats[k].std;
      d += (z1 - z2) ** 2;
    }
    return { row: r, dist: Math.sqrt(d) };
  }).sort((a, b) => a.dist - b.dist);

  const K = Math.min(15, Math.max(5, Math.floor(candidates.length / 3)));
  const neighbors = distances.slice(0, K);

  const resolved = neighbors
    .map(n => {
      const fwd = nearestRow(btcRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { analog_ts: n.row.ts, dist: n.dist, return_pct: (fwd.btc_price - n.row.btc_price) / n.row.btc_price * 100 };
    })
    .filter(Boolean);

  if (resolved.length < MIN_RESOLVED_ANALOGS) {
    return { ok: true, status: 'insufficient_resolved_analogs', n_neighbors: neighbors.length, n_resolved: resolved.length, min_required: MIN_RESOLVED_ANALOGS };
  }

  const returns = resolved.map(r => r.return_pct).sort((a, b) => a - b);
  const nUp = returns.filter(r => r > 0).length;
  const pUp = nUp / returns.length;
  const pct = (p) => returns[Math.min(returns.length - 1, Math.floor(returns.length * p))];
  const median = pct(0.5);
  const p25 = pct(0.25);
  const p75 = pct(0.75);

  // ---- Experimental: adaptive K + distance-weighted aggregation ----
  // Built and logged ALONGSIDE the fixed-K/unweighted numbers above, not
  // instead of them — the headline p_up/median above stays exactly what
  // it's been, already being calibrated. This experiment runs in parallel,
  // gets its own columns, and only gets compared against the original once
  // there's enough resolved data for that comparison to mean anything (see
  // /calibration). Built now, trusted later — not the other way round.
  //
  // Adaptive K: today's trailing volatility is percentile-ranked against
  // the same measure computed retrospectively at every historical point
  // (not a hardcoded threshold) — high-volatility days use a smaller,
  // more selective K; calm days use a wider pool.
  const historicalVol = candidates.map((_, i) => trailingVolatility(candidates, i));
  const todayVol = trailingVolatility(complete, complete.length - 1);
  const volPercentile = todayVol != null ? percentileRank(todayVol, historicalVol) : null;
  let kAdaptive = K;
  if (volPercentile != null) {
    if (volPercentile >= 0.66) kAdaptive = Math.max(5, Math.floor(K * 0.6));   // volatile: fewer, closer analogs
    else if (volPercentile <= 0.33) kAdaptive = Math.min(candidates.length, Math.floor(K * 1.4)); // calm: wider pool is safer
  }
  const neighborsAdaptive = distances.slice(0, kAdaptive);
  const resolvedAdaptive = neighborsAdaptive
    .map(n => {
      const fwd = nearestRow(btcRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { dist: n.dist, return_pct: (fwd.btc_price - n.row.btc_price) / n.row.btc_price * 100 };
    })
    .filter(Boolean);

  let pUpExperimental = null, medianReturnExperimental = null;
  if (resolvedAdaptive.length >= MIN_RESOLVED_ANALOGS) {
    const EPS = 0.05; // avoids divide-by-zero for a near-exact historical match
    const weighted = resolvedAdaptive.map(r => ({ value: r.return_pct, weight: 1 / (r.dist + EPS) }));
    const totalWeight = weighted.reduce((s, w) => s + w.weight, 0);
    const upWeight = weighted.filter(w => w.value > 0).reduce((s, w) => s + w.weight, 0);
    pUpExperimental = upWeight / totalWeight;
    medianReturnExperimental = weightedQuantile(weighted, 0.5);
  }

  // Regime-anomaly tripwire: is even the single closest analog unusually
  // far away compared to every closest-match distance seen historically?
  // If so, today's setup doesn't resemble anything in history well, and
  // the prediction above is honestly built on weaker matches than usual —
  // flagged rather than silently reported with the same confidence as any
  // other day.
  const closestDist = distances[0].dist;
  const historicalClosestDists = candidates.map((_, i) => {
    if (i === 0) return null;
    let best = Infinity;
    for (let j = 0; j < i; j++) {
      let d = 0;
      for (const k of FEATURE_KEYS) {
        const z1 = (candidates[i][k] - stats[k].mean) / stats[k].std;
        const z2 = (candidates[j][k] - stats[k].mean) / stats[k].std;
        d += (z1 - z2) ** 2;
      }
      d = Math.sqrt(d);
      if (d < best) best = d;
    }
    return Number.isFinite(best) ? best : null;
  });
  const closestDistPercentile = percentileRank(closestDist, historicalClosestDists);
  const isRegimeAnomaly = closestDistPercentile != null && closestDistPercentile >= 0.9;

  // Trend-strength: MA-slope over the same price series already loaded
  // above (complete, ascending). Guardrail only — never a distance-metric
  // feature, so K/weight tuning elsewhere is untouched.
  const trend = trendStrength(complete, 'btc_price');

  // Additive decile recalibration — falls back to raw pUp if no curve
  // exists yet or the closest bucket is too thin (see applyCalibratedProbability).
  const curveRows = await getLatestCalibrationCurve(env, 'BTC', horizonHours);
  const calibratedPUp = applyCalibratedProbability(pUp, curveRows);

  const nowTs = Date.now();
  const features = Object.fromEntries(FEATURE_KEYS.map(k => [k, today[k]]));

  // Conditional calibration -- reuses the same historical-resolved-rows
  // pattern already proven in selectBestVariant's meta-neighborhood, not a
  // new data-fetching approach.
  const { results: calibHistoryRows } = await env.DB.prepare(
    `SELECT features_json, realized_up FROM predictions WHERE horizon_hours=? AND realized_up IS NOT NULL AND features_json IS NOT NULL AND ts < ? ORDER BY ts DESC LIMIT 300`
  ).bind(horizonHours, today.ts).all();
  const parsedCalibHistory = calibHistoryRows.map(r => {
    try { return { features: JSON.parse(r.features_json), realized_up: r.realized_up }; } catch { return null; }
  }).filter(Boolean);
  const conditionalResult = computeConditionalCalibration(features, parsedCalibHistory, CONDITIONAL_CALIB_WEIGHTS, CONDITIONAL_CALIB_K, CONDITIONAL_CALIB_MIN_NEIGHBORS);
  const calibratedConditionalPUp = conditionalResult ? conditionalResult.p_up : null; // null, not a raw fallback -- distinct from calibrated_p_up's own fallback semantics, makes "not enough neighbors yet" visible rather than silently indistinguishable from a real score

  let insert = null;
  if (persist) {
    insert = await env.DB.prepare(
      `INSERT INTO predictions
       (ts, target_ts, btc_price_at_prediction, p_up, n_analogs, median_analog_return, return_p25, return_p75, features_json,
        k_used, volatility_percentile, closest_analog_dist, is_regime_anomaly, p_up_experimental, median_return_experimental, horizon_hours,
        trend_strength, calibrated_p_up, calibrated_conditional_p_up, model_version, git_commit_sha)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(
      nowTs, nowTs + lagMs, today.btc_price, pUp, resolved.length, median, p25, p75, JSON.stringify(features),
      kAdaptive, volPercentile, closestDist, isRegimeAnomaly ? 1 : 0, pUpExperimental, medianReturnExperimental, horizonHours,
      trend, calibratedPUp, calibratedConditionalPUp, MODEL_VERSIONS.btc_core, currentGitSha(env)
    ).run();
  }

  return {
    ok: true,
    status: 'ok',
    prediction_id: insert ? insert.meta.last_row_id : null,
    persisted: persist,
    ts: nowTs,
    horizon_hours: horizonHours,
    p_up: Number(pUp.toFixed(3)),
    calibrated_p_up: Number(calibratedPUp.toFixed(3)),
    calibrated_conditional_p_up: calibratedConditionalPUp != null ? Number(calibratedConditionalPUp.toFixed(3)) : null,
    conditional_calibration_n_neighbors: conditionalResult ? conditionalResult.n_neighbors : null,
    trend_strength: Number(trend.toFixed(3)),
    n_analogs: resolved.length,
    median_analog_return_pct: Number(median.toFixed(2)),
    return_range_pct: [Number(p25.toFixed(2)), Number(p75.toFixed(2))],
    btc_price_now: today.btc_price,
    features,
    top_analogs: resolved.slice(0, 5).map(r => ({ date: new Date(r.analog_ts).toISOString().slice(0, 10), return_pct: Number(r.return_pct.toFixed(2)) })),
    regime_anomaly: isRegimeAnomaly,
    experimental: {
      k_used: kAdaptive,
      volatility_percentile: volPercentile != null ? Number(volPercentile.toFixed(2)) : null,
      p_up: pUpExperimental != null ? Number(pUpExperimental.toFixed(3)) : null,
      median_return_pct: medianReturnExperimental != null ? Number(medianReturnExperimental.toFixed(2)) : null,
      note: 'Adaptive-K + distance-weighted variant, logged in parallel — not yet trusted over the headline number above. See /calibration once enough of these resolve.',
    },
    note: isRegimeAnomaly
      ? `Today's setup doesn't closely resemble anything in ${candidates.length} days of history — the analogs behind this number are weaker matches than usual. Read with extra caution.`
      : `Based on the ${resolved.length} most similar days in ${candidates.length} days of history. Small sample — read as a rough lean and a plausible range, not a forecast.`,
  };
}

// Self-computed regime signal for ETH, purely from ETH's own price series —
// deliberately NOT borrowed from another asset. Direct response to what was
// found investigating LINK: its model draws 2 of its 3 features
// (btc_regime_mag AND sentiment_score) from BTC's own data, only
// technical_score is genuinely LINK's. That's a real, confirmed design
// smell, not a hypothesis — ETH's model is built to not repeat it, even
// though that means a narrower feature set than BTC (4 features, itself
// partly enriched from V1's BTC-centric history table) or LINK (3, mostly
// borrowed). Same short-MA vs long-MA deviation technique already proven
// in trendStrength, but uncapped here since it's a distance-metric input
// (z-score normalized downstream) rather than a bounded guardrail multiplier.
function computeEthRegimeMag(sortedRows, endIdx, shortN = 8, longN = 21) {
  if (endIdx + 1 < longN) return null;
  const recent = sortedRows.slice(endIdx + 1 - longN, endIdx + 1);
  const avg = (arr) => arr.reduce((a, r) => a + r.eth_price, 0) / arr.length;
  const shortMA = avg(recent.slice(-shortN));
  const longMA = avg(recent);
  if (!longMA) return null;
  return (shortMA - longMA) / longMA * 100;
}

const ETH_FEATURE_KEYS = ['technical_score', 'eth_regime_mag'];
const ETH_MIN_COMPLETE_ROWS = 30;
const ETH_MIN_RESOLVED_ANALOGS = 5;

async function runEthPrediction(env, horizonHours = 24, { persist = true } = {}) {
  const lagMs = horizonHours * 60 * 60 * 1000;
  const tolMs = lagMs * 0.2;

  const { results: ethRows } = await env.DB.prepare(
    'SELECT ts, eth_price, technical_score FROM eth_data ORDER BY ts ASC'
  ).all();
  if (ethRows.length < ETH_MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: ethRows.length, min_required: ETH_MIN_COMPLETE_ROWS };
  }

  const complete = ethRows
    .map((r, i) => ({
      ts: r.ts, eth_price: r.eth_price, technical_score: r.technical_score,
      eth_regime_mag: computeEthRegimeMag(ethRows, i),
    }))
    .filter(r => r.technical_score != null && r.eth_regime_mag != null);
  if (complete.length < ETH_MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: complete.length, min_required: ETH_MIN_COMPLETE_ROWS };
  }

  const stats = {};
  for (const k of ETH_FEATURE_KEYS) stats[k] = meanStd(complete.map(r => r[k]));

  const today = complete[complete.length - 1];
  const candidates = complete.slice(0, -1).filter(r => r.ts <= today.ts - (lagMs + tolMs));

  const distances = candidates.map(r => {
    let d = 0;
    for (const k of ETH_FEATURE_KEYS) {
      const z1 = (today[k] - stats[k].mean) / stats[k].std;
      const z2 = (r[k] - stats[k].mean) / stats[k].std;
      d += (z1 - z2) ** 2;
    }
    return { row: r, dist: Math.sqrt(d) };
  }).sort((a, b) => a.dist - b.dist);

  const K = Math.min(15, Math.max(5, Math.floor(candidates.length / 3)));
  const neighbors = distances.slice(0, K);

  const resolved = neighbors
    .map(n => {
      const fwd = nearestRow(ethRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { analog_ts: n.row.ts, dist: n.dist, return_pct: (fwd.eth_price - n.row.eth_price) / n.row.eth_price * 100 };
    })
    .filter(Boolean);

  if (resolved.length < ETH_MIN_RESOLVED_ANALOGS) {
    return { ok: true, status: 'insufficient_resolved_analogs', n_neighbors: neighbors.length, n_resolved: resolved.length, min_required: ETH_MIN_RESOLVED_ANALOGS };
  }

  const returns = resolved.map(r => r.return_pct).sort((a, b) => a - b);
  const nUp = returns.filter(r => r > 0).length;
  const pUp = nUp / returns.length;
  const pct = (p) => returns[Math.min(returns.length - 1, Math.floor(returns.length * p))];
  const median = pct(0.5);
  const p25 = pct(0.25);
  const p75 = pct(0.75);

  // Adaptive K + distance-weighted variant, same technique as BTC/LINK,
  // logged alongside the headline number, not replacing it.
  const historicalVol = candidates.map((_, i) => trailingVolatility(candidates, i, 14, 'eth_price'));
  const todayVol = trailingVolatility(complete, complete.length - 1, 14, 'eth_price');
  const volPercentile = todayVol != null ? percentileRank(todayVol, historicalVol) : null;
  let kAdaptive = K;
  if (volPercentile != null) {
    if (volPercentile >= 0.66) kAdaptive = Math.max(5, Math.floor(K * 0.6));
    else if (volPercentile <= 0.33) kAdaptive = Math.min(candidates.length, Math.floor(K * 1.4));
  }
  const neighborsAdaptive = distances.slice(0, kAdaptive);
  const resolvedAdaptive = neighborsAdaptive
    .map(n => {
      const fwd = nearestRow(ethRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { dist: n.dist, return_pct: (fwd.eth_price - n.row.eth_price) / n.row.eth_price * 100 };
    })
    .filter(Boolean);

  let pUpExperimental = null, medianReturnExperimental = null;
  if (resolvedAdaptive.length >= ETH_MIN_RESOLVED_ANALOGS) {
    const EPS = 0.05;
    const weighted = resolvedAdaptive.map(r => ({ value: r.return_pct, weight: 1 / (r.dist + EPS) }));
    const totalWeight = weighted.reduce((s, w) => s + w.weight, 0);
    const upWeight = weighted.filter(w => w.value > 0).reduce((s, w) => s + w.weight, 0);
    pUpExperimental = upWeight / totalWeight;
    medianReturnExperimental = weightedQuantile(weighted, 0.5);
  }

  const closestDist = distances[0].dist;
  const historicalClosestDists = candidates.map((_, i) => {
    if (i === 0) return null;
    let best = Infinity;
    for (let j = 0; j < i; j++) {
      let d = 0;
      for (const k of ETH_FEATURE_KEYS) {
        const z1 = (candidates[i][k] - stats[k].mean) / stats[k].std;
        const z2 = (candidates[j][k] - stats[k].mean) / stats[k].std;
        d += (z1 - z2) ** 2;
      }
      d = Math.sqrt(d);
      if (d < best) best = d;
    }
    return Number.isFinite(best) ? best : null;
  });
  const closestDistPercentile = percentileRank(closestDist, historicalClosestDists);
  const isRegimeAnomaly = closestDistPercentile != null && closestDistPercentile >= 0.9;

  const trend = trendStrength(complete, 'eth_price');

  const curveRows = await getLatestCalibrationCurve(env, 'ETH', horizonHours);
  const calibratedPUp = applyCalibratedProbability(pUp, curveRows);

  const nowTs = Date.now();
  const features = Object.fromEntries(ETH_FEATURE_KEYS.map(k => [k, today[k]]));

  let insert = null;
  if (persist) {
    insert = await env.DB.prepare(
      `INSERT INTO eth_predictions
       (ts, target_ts, eth_price_at_prediction, p_up, n_analogs, median_analog_return, return_p25, return_p75, features_json,
        k_used, volatility_percentile, closest_analog_dist, is_regime_anomaly, p_up_experimental, median_return_experimental, horizon_hours,
        trend_strength, calibrated_p_up, model_version, git_commit_sha)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(
      nowTs, nowTs + lagMs, today.eth_price, pUp, resolved.length, median, p25, p75, JSON.stringify(features),
      kAdaptive, volPercentile, closestDist, isRegimeAnomaly ? 1 : 0, pUpExperimental, medianReturnExperimental, horizonHours,
      trend, calibratedPUp, MODEL_VERSIONS.eth_core, currentGitSha(env)
    ).run();
  }

  return {
    ok: true, status: 'ok', prediction_id: insert ? insert.meta.last_row_id : null, persisted: persist, ts: nowTs, horizon_hours: horizonHours,
    p_up: Number(pUp.toFixed(3)), calibrated_p_up: Number(calibratedPUp.toFixed(3)), trend_strength: Number(trend.toFixed(3)),
    n_analogs: resolved.length, median_analog_return_pct: Number(median.toFixed(2)),
    return_range_pct: [Number(p25.toFixed(2)), Number(p75.toFixed(2))], eth_price_now: today.eth_price, features,
    top_analogs: resolved.slice(0, 5).map(r => ({ date: new Date(r.analog_ts).toISOString().slice(0, 10), return_pct: Number(r.return_pct.toFixed(2)) })),
    regime_anomaly: isRegimeAnomaly,
    experimental: {
      k_used: kAdaptive, volatility_percentile: volPercentile != null ? Number(volPercentile.toFixed(2)) : null,
      p_up: pUpExperimental != null ? Number(pUpExperimental.toFixed(3)) : null,
      median_return_pct: medianReturnExperimental != null ? Number(medianReturnExperimental.toFixed(2)) : null,
      note: 'Adaptive-K + distance-weighted variant, logged in parallel — not yet trusted over the headline number above.',
    },
    note: isRegimeAnomaly
      ? `Today's setup doesn't closely resemble anything in ${candidates.length} days of history — the analogs behind this number are weaker matches than usual. Read with extra caution.`
      : `Based on the ${resolved.length} most similar days in ${candidates.length} days of history. Small sample — read as a rough lean and a plausible range, not a forecast. ETH's model has far less history than BTC/LINK right now — treat every number here as considerably less proven.`,
  };
}

// Fills in what actually happened for any prediction whose 24h horizon has
// passed but hasn't been resolved yet. This is the calibration loop — the
// part that actually lets the model be checked against reality over time.
async function backfillPredictions(env) {
  const { results: btcRows } = await env.DB.prepare(
    'SELECT ts, btc_price FROM btc_data ORDER BY ts ASC'
  ).all();
  const { results: unresolved } = await env.DB.prepare(
    'SELECT id, target_ts, btc_price_at_prediction FROM predictions WHERE realized_up IS NULL AND target_ts <= ?'
  ).bind(Date.now()).all();

  let resolvedCount = 0;
  for (const p of unresolved) {
    const match = nearestRow(btcRows, p.target_ts);
    if (!match) continue;
    const ret = (match.btc_price - p.btc_price_at_prediction) / p.btc_price_at_prediction * 100;
    await env.DB.prepare(
      'UPDATE predictions SET realized_btc_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'
    ).bind(match.btc_price, ret, ret > 0 ? 1 : 0, Date.now(), p.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

// Mirrors backfillPredictions exactly, same reasoning: this is the loop
// that actually lets ETH's model be checked against reality over time.
async function backfillEthPredictions(env) {
  const { results: ethRows } = await env.DB.prepare(
    'SELECT ts, eth_price FROM eth_data ORDER BY ts ASC'
  ).all();
  const { results: unresolved } = await env.DB.prepare(
    'SELECT id, target_ts, eth_price_at_prediction FROM eth_predictions WHERE realized_up IS NULL AND target_ts <= ?'
  ).bind(Date.now()).all();

  let resolvedCount = 0;
  for (const p of unresolved) {
    const match = nearestRow(ethRows, p.target_ts);
    if (!match) continue;
    const ret = (match.eth_price - p.eth_price_at_prediction) / p.eth_price_at_prediction * 100;
    await env.DB.prepare(
      'UPDATE eth_predictions SET realized_eth_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'
    ).bind(match.eth_price, ret, ret > 0 ? 1 : 0, Date.now(), p.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

// Challenger added below, mirroring linkPredictAndLog exactly --
// runChallengerPrediction was already fully coin-agnostic (confirmed by
// inspection: it only ever reads generic fields off coreResult, never
// anything BTC/LINK-specific), so no changes were needed to that function
// itself, only to this call site and the coin-to-table mappings a few
// other places incorrectly assumed only BTC/LINK would ever appear
// (backfillChallengerPredictions, getChallengerCalibration, and three
// route handlers all had a real bug: coin === 'LINK' ? ... : 'BTC' would
// have silently resolved ETH's data against BTC's price table).
async function ethPredictAndLog(env, horizonHours = 24, { allowWrite = false } = {}) {
  const resolvedCount = await backfillEthPredictions(env); // resolution is unconditional, same reasoning as predictAndLog
  const shouldWrite = allowWrite || (await isRecentDataStale(env, 'eth_data') && await claimStaleRefresh(env, 'ETH', Date.now()));
  if (shouldWrite) await logEthData(env);
  const result = await runEthPrediction(env, horizonHours, { persist: shouldWrite });
  result.backfilled_this_call = resolvedCount;
  try {
    const challengerResolvedCount = await backfillChallengerPredictions(env);
    const challengerResult = await runChallengerPrediction(env, {
      coin: 'ETH', horizonHours, priceTable: 'eth_data', priceCol: 'eth_price', priceNow: result.eth_price_now, coreResult: result,
    }, { persist: shouldWrite });
    result.challenger = challengerResult;
    result.challenger_backfilled_this_call = challengerResolvedCount;
  } catch (e) {
    result.challenger = { ok: false, error: String(e) };
  }
  return result;
}

// ============================================================
// Condition-Matched Selection layer ("the council build")
// ============================================================
// Direct build from a real 4-AI consultation (Claude architecture + Gemini's
// DCS-LA algorithm + ChatGPT's schema/significance design + Grok's catalyst-
// checking pattern reused for the "why" attachment). Selects which variant's
// prediction to trust RIGHT NOW, per coin/horizon, using Dynamic Classifier
// Selection with Local Class Accuracy (LCA) scoring -- not "who has the best
// recent accuracy globally" (the naive, explicitly-rejected approach this
// session already identified as reintroducing shallow number-chasing), but
// "among historical moments that looked like THIS one, which variant was
// actually right when it made THIS SAME directional call." Gated by a
// Bonferroni-corrected significance bar scaled to how many variants are
// actually being compared (never a flat six) -- multiple-testing correction
// borrowed directly from the Deflated Sharpe Ratio literature's core insight:
// comparing many things and picking the best inflates the winner's apparent
// edge even with zero real skill anywhere in the pool.

// Registry: which variants exist for which coin.
const SELECTION_VARIANTS = {
  BTC: [
    { key: 'original', table: 'predictions', field: 'p_up', coinFilter: false },
    { key: 'experimental', table: 'predictions', field: 'p_up_experimental', coinFilter: false },
    { key: 'calibrated', table: 'predictions', field: 'calibrated_p_up', coinFilter: false },
    { key: 'challenger_flat', table: 'challenger_predictions', field: 'p_up_flat', coinFilter: true },
    { key: 'challenger_tilted', table: 'challenger_predictions', field: 'p_up_tilted', coinFilter: true },
    { key: 'challenger_calibrated', table: 'challenger_predictions', field: 'calibrated_p_up_flat', coinFilter: true },
  ],
  LINK: [
    { key: 'original', table: 'link_predictions', field: 'p_up', coinFilter: false },
    { key: 'experimental', table: 'link_predictions', field: 'p_up_experimental', coinFilter: false },
    { key: 'calibrated', table: 'link_predictions', field: 'calibrated_p_up', coinFilter: false },
    { key: 'challenger_flat', table: 'challenger_predictions', field: 'p_up_flat', coinFilter: true },
    { key: 'challenger_tilted', table: 'challenger_predictions', field: 'p_up_tilted', coinFilter: true },
    { key: 'challenger_calibrated', table: 'challenger_predictions', field: 'calibrated_p_up_flat', coinFilter: true },
  ],
  ETH: [
    { key: 'original', table: 'eth_predictions', field: 'p_up', coinFilter: false },
    { key: 'experimental', table: 'eth_predictions', field: 'p_up_experimental', coinFilter: false },
    { key: 'calibrated', table: 'eth_predictions', field: 'calibrated_p_up', coinFilter: false },
    { key: 'challenger_flat', table: 'challenger_predictions', field: 'p_up_flat', coinFilter: true },
    { key: 'challenger_tilted', table: 'challenger_predictions', field: 'p_up_tilted', coinFilter: true },
    { key: 'challenger_calibrated', table: 'challenger_predictions', field: 'calibrated_p_up_flat', coinFilter: true },
  ],
};
const SELECTION_MIN_HISTORY = 50; // matches Model Health's own recent/prior threshold, not a new number invented for this
const SELECTION_MIN_MATCHED = 3; // minimum same-direction neighborhood matches before a variant's LCA score is trusted at all
// One-sided critical z-values for alpha=0.05, Bonferroni-corrected for
// m=1..6 simultaneous comparisons. Exact standard-normal quantiles, not an
// approximation -- comparison count is bounded 1-6 by the variant registry
// above, so a small lookup table is more reliable than an inverse-CDF
// approximation function that could have its own subtle error.
const SELECTION_CRITICAL_Z = { 1: 1.6449, 2: 1.9600, 3: 2.1280, 4: 2.2414, 5: 2.3263, 6: 2.3940 };

function coreTableForCoin(coin) {
  return coin === 'BTC' ? 'predictions' : coin === 'LINK' ? 'link_predictions' : 'eth_predictions';
}

// Pure function -- takes already-fetched rows, computes LCA score for one
// variant against one neighborhood. Extracted separately from the D1-coupled
// orchestration below specifically so this, the actual statistical core, is
// unit-testable without mocking a database.
function computeLcaScore(variantRows, neighborhood, todaysCallUp, tolMs) {
  let numerator = 0, denominator = 0;
  for (const n of neighborhood) {
    const match = nearestRow(variantRows, n.ts, tolMs);
    if (!match) continue;
    const callUp = match.p_up >= 0.5;
    if (callUp !== todaysCallUp) continue; // LCA restricts to same-direction historical calls, per Gemini's formula
    denominator++;
    if ((match.p_up >= 0.5) === (match.realized_up === 1)) numerator++;
  }
  if (denominator < SELECTION_MIN_MATCHED) return null;
  return { lca: numerator / denominator, n_matched: denominator };
}

// Pure function -- given each eligible variant's LCA score, decides the
// winner and whether it clears the significance bar. Separated from the
// data-fetching orchestration for the same testability reason as above.
function decideSelection(scores) {
  if (!scores.length) return { chosen: null, clearedGate: false, winner: null, requiredMargin: null };
  const sorted = [...scores].sort((a, b) => b.lca - a.lca);
  const winner = sorted[0];
  const m = Math.min(6, Math.max(1, scores.length));
  const z = SELECTION_CRITICAL_Z[m];
  const requiredMargin = z * Math.sqrt(0.25 / winner.n_matched);
  const clearedGate = (winner.lca - 0.5) > requiredMargin;
  return { chosen: clearedGate ? winner.variant : 'original', clearedGate, winner, requiredMargin, m };
}

async function selectBestVariant(env, coin, horizonHours) {
  const variantDefs = SELECTION_VARIANTS[coin];
  if (!variantDefs) return { ok: false, error: 'unknown coin' };
  const coreTable = coreTableForCoin(coin);

  // Step 1: eligibility. A variant must have its OWN 50+ resolved
  // predictions before it's even considered -- same bar Model Health
  // already uses, not a new one invented for this.
  const eligible = [];
  for (const v of variantDefs) {
    const whereCoin = v.coinFilter ? `coin = '${coin}' AND ` : '';
    const { results } = await env.DB.prepare(
      `SELECT COUNT(*) as n FROM ${v.table} WHERE ${whereCoin}horizon_hours=? AND realized_up IS NOT NULL AND ${v.field} IS NOT NULL`
    ).bind(horizonHours).all();
    if (results[0].n >= SELECTION_MIN_HISTORY) eligible.push(v);
  }
  if (!eligible.length) {
    return { ok: true, status: 'no_eligible_variants', chosen_variant: 'original', cleared_gate: false, reason: `No variant has ${SELECTION_MIN_HISTORY}+ resolved predictions yet -- defaulting to Original k-NN.` };
  }

  // Step 2: today's query condition, from the core table's own stored
  // features_json (already computed by the underlying model, not
  // recomputed here).
  const latestCore = await env.DB.prepare(
    `SELECT ts, features_json FROM ${coreTable} WHERE horizon_hours=? ORDER BY ts DESC LIMIT 1`
  ).bind(horizonHours).first();
  if (!latestCore || !latestCore.features_json) {
    return { ok: true, status: 'no_query_features', chosen_variant: 'original', cleared_gate: false, reason: 'No recent prediction with feature data to match a condition against.' };
  }
  let queryFeatures;
  try { queryFeatures = JSON.parse(latestCore.features_json); } catch { return { ok: true, status: 'bad_features', chosen_variant: 'original', cleared_gate: false, reason: 'Could not parse the latest feature vector.' }; }
  const featureKeys = Object.keys(queryFeatures);

  // Step 3: meta-neighborhood. The core table's own resolved history
  // (up to 300 most recent, well within D1's comfort zone at this scale)
  // defines the shared "condition timeline" every variant is matched
  // against -- the condition is a property of the MOMENT, not the variant.
  const { results: coreHistory } = await env.DB.prepare(
    `SELECT ts, features_json, realized_up FROM ${coreTable} WHERE horizon_hours=? AND realized_up IS NOT NULL AND features_json IS NOT NULL AND ts < ? ORDER BY ts DESC LIMIT 300`
  ).bind(horizonHours, latestCore.ts).all();
  if (coreHistory.length < 15) {
    return { ok: true, status: 'insufficient_meta_history', chosen_variant: 'original', cleared_gate: false, reason: `Only ${coreHistory.length} historical moments with feature data -- not enough to define a neighborhood yet.` };
  }

  const stats = {};
  for (const k of featureKeys) {
    const vals = [];
    for (const r of coreHistory) { try { const f = JSON.parse(r.features_json); if (f[k] != null) vals.push(f[k]); } catch {} }
    if (vals.length < 5) continue;
    const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
    const std = Math.sqrt(vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length) || 1;
    stats[k] = { mean, std };
  }
  const usableKeys = featureKeys.filter(k => stats[k]);

  const distances = coreHistory.map(r => {
    let feat;
    try { feat = JSON.parse(r.features_json); } catch { return null; }
    let d = 0;
    for (const k of usableKeys) {
      if (feat[k] == null || queryFeatures[k] == null) continue;
      const z1 = (queryFeatures[k] - stats[k].mean) / stats[k].std;
      const z2 = (feat[k] - stats[k].mean) / stats[k].std;
      d += (z1 - z2) ** 2;
    }
    return { ts: r.ts, dist: Math.sqrt(d) };
  }).filter(Boolean).sort((a, b) => a.dist - b.dist);

  // K_SEL per Gemini's guidance: literature standard [7,15], scaled to
  // available history rather than fixed, floored/ceilinged to that range.
  const kSel = Math.min(15, Math.max(7, Math.floor(distances.length / 10)));
  const neighborhood = distances.slice(0, kSel);
  const TOL_MS_META = 6 * 3600000; // 6h tolerance matching a variant's own prediction to a neighborhood timestamp

  // Step 4: LCA score per eligible variant.
  const scores = [];
  for (const v of eligible) {
    const whereCoin = v.coinFilter ? `coin = '${coin}' AND ` : '';
    const { results: variantRows } = await env.DB.prepare(
      `SELECT ts, ${v.field} as p_up, realized_up FROM ${v.table} WHERE ${whereCoin}horizon_hours=? AND realized_up IS NOT NULL AND ${v.field} IS NOT NULL ORDER BY ts ASC`
    ).bind(horizonHours).all();
    if (!variantRows.length) continue;
    const latestVariantRow = variantRows[variantRows.length - 1];
    const todaysCallUp = latestVariantRow.p_up >= 0.5;
    const scored = computeLcaScore(variantRows, neighborhood, todaysCallUp, TOL_MS_META);
    if (scored) scores.push({ variant: v.key, p_up: latestVariantRow.p_up, ...scored });
  }
  if (!scores.length) {
    return { ok: true, status: 'no_scorable_variants', chosen_variant: 'original', cleared_gate: false, reason: `No eligible variant had ${SELECTION_MIN_MATCHED}+ same-direction matches in the neighborhood -- defaulting to Original k-NN.` };
  }

  // Step 5: significance-gated decision.
  const decision = decideSelection(scores);
  const winnerScore = scores.find(s => s.variant === decision.winner.variant);
  const chosenScore = scores.find(s => s.variant === decision.chosen) || winnerScore;

  const reason = decision.clearedGate
    ? `${decision.winner.variant} locally outperformed (${(decision.winner.lca * 100).toFixed(0)}% correct on ${decision.winner.n_matched} same-direction matches among the ${kSel} most similar historical moments), clearing the significance bar for ${decision.m} variant(s) compared.`
    : `No variant's local edge cleared the significance bar (needed >${(decision.requiredMargin * 100).toFixed(1)}pts above 50%, best was ${decision.winner.variant} at +${((decision.winner.lca - 0.5) * 100).toFixed(1)}pts on n=${decision.winner.n_matched}) -- defaulting to Original k-NN.`;

  const ts = Date.now();
  const scoresJson = JSON.stringify(scores.map(s => ({ variant: s.variant, p_up: Number(s.p_up.toFixed(3)), lca: Number(s.lca.toFixed(3)), n_matched: s.n_matched })));
  await env.DB.prepare(
    `INSERT INTO selection_decisions (ts, coin, horizon_hours, chosen_variant, chosen_p_up, lca_score, comparison_count, corrected_alpha, cleared_gate, k_sel, neighborhood_json, reason, prediction_ts, scores_json)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    ts, coin, horizonHours, decision.chosen, chosenScore ? chosenScore.p_up : null, decision.winner.lca, decision.m,
    0.05 / decision.m, decision.clearedGate ? 1 : 0, kSel,
    JSON.stringify(neighborhood.map(n => ({ ts: n.ts, dist: Number(n.dist.toFixed(3)) }))),
    reason, latestCore.ts, scoresJson
  ).run();

  return {
    ok: true, status: 'ok', coin, horizon_hours: horizonHours, chosen_variant: decision.chosen,
    chosen_p_up: chosenScore ? Number(chosenScore.p_up.toFixed(3)) : null,
    cleared_gate: decision.clearedGate, comparison_count: decision.m, k_sel: kSel,
    scores: scores.map(s => ({ variant: s.variant, p_up: Number(s.p_up.toFixed(3)), lca: Number(s.lca.toFixed(3)), n_matched: s.n_matched })),
    reason,
  };
}

// Read-only. Serves the frontend's display route (GET /select-variant)
// WITHOUT recomputing anything -- was previously calling selectBestVariant
// directly, which meant every single page view re-ran the full live k-NN
// computation AND wrote a fresh row to selection_decisions, indistinguishable
// from a real prediction-triggered selection. selectBestVariant itself is
// unchanged and still called directly (compute + write) immediately after a
// new prediction from /predict, /link-predict, /eth-predict -- that write is
// legitimate and necessary there. This function only reads what's already
// there.
async function getLatestSelection(env, coin, horizonHours) {
  const row = await env.DB.prepare(
    `SELECT * FROM selection_decisions WHERE coin=? AND horizon_hours=? ORDER BY ts DESC LIMIT 1`
  ).bind(coin, horizonHours).first();

  if (!row) {
    return { ok: true, status: 'no_selection_yet', chosen_variant: 'original', cleared_gate: false, reason: 'No selection has been computed yet for this coin/horizon -- one will be written the next time a real prediction runs.' };
  }

  let scores = [];
  try { scores = row.scores_json ? JSON.parse(row.scores_json) : []; } catch { /* older row predates scores_json -- empty, not fabricated */ }

  return {
    ok: true, status: 'ok', coin: row.coin, horizon_hours: row.horizon_hours, chosen_variant: row.chosen_variant,
    chosen_p_up: row.chosen_p_up, cleared_gate: !!row.cleared_gate, comparison_count: row.comparison_count, k_sel: row.k_sel,
    scores, reason: row.reason, ts: row.ts,
  };
}

async function getCalibration(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT p_up, realized_up, p_up_experimental FROM predictions WHERE realized_up IS NOT NULL AND horizon_hours = ?'
  ).bind(horizonHours).all();
  const n = results.length;
  if (n === 0) return { ok: true, n_resolved: 0, note: `No resolved ${horizonHours}h predictions yet.` };

  const accuracy = results.filter(r => (r.p_up >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = results.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / n;

  const upRate = results.filter(r => r.realized_up === 1).length / n;
  const brierAlways5050 = 0.25;
  const brierAlwaysBaseRate = results.reduce((s, r) => s + (upRate - r.realized_up) ** 2, 0) / n;
  const bestNaiveBrier = Math.min(brierAlways5050, brierAlwaysBaseRate);
  const beatsNaiveBaseline = brier < bestNaiveBrier;

  // The tracker for the adaptive-K/weighted-distance experiment: only
  // meaningful once enough resolved predictions actually have an
  // experimental value logged (early predictions, before this was built,
  // won't). Reports "not enough data yet" honestly rather than a number
  // built on a handful of rows.
  const withExperimental = results.filter(r => r.p_up_experimental != null);
  let experimentalComparison = { available: false, note: 'Not enough resolved predictions with the experimental variant logged yet.' };
  if (withExperimental.length >= 20) {
    const nExp = withExperimental.length;
    const accuracyExp = withExperimental.filter(r => (r.p_up_experimental >= 0.5) === (r.realized_up === 1)).length / nExp;
    const brierExp = withExperimental.reduce((s, r) => s + (r.p_up_experimental - r.realized_up) ** 2, 0) / nExp;
    const brierOrigSameSet = withExperimental.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / nExp;
    experimentalComparison = {
      available: true,
      n_resolved: nExp,
      accuracy_experimental: Number(accuracyExp.toFixed(3)),
      brier_experimental: Number(brierExp.toFixed(3)),
      brier_original_same_set: Number(brierOrigSameSet.toFixed(3)),
      experimental_wins: brierExp < brierOrigSameSet,
      note: brierExp < brierOrigSameSet
        ? 'The adaptive-K/weighted variant is currently outperforming the original on the same set of days — worth considering promoting it, but check this again once n is larger.'
        : 'The original fixed-K/unweighted approach is still doing as well or better — no reason to switch yet.',
    };
  }

  return {
    ok: true,
    n_resolved: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)),
    brier_baseline_5050: brierAlways5050,
    brier_baseline_up_rate: Number(brierAlwaysBaseRate.toFixed(3)),
    beats_naive_baseline: beatsNaiveBaseline,
    experimental_vs_original: experimentalComparison,
    note: n < 20
      ? `Only ${n} resolved predictions — this comparison is noise at this size, not a real verdict yet. Revisit past ~20-30.`
      : beatsNaiveBaseline
        ? `Beats the best naive baseline (${bestNaiveBrier.toFixed(3)}) — there's a real, if modest, edge here.`
        : `Does NOT beat the best naive baseline (${bestNaiveBrier.toFixed(3)}) — right now a constant guess would have done as well or better. Worth taking seriously, not explaining away.`,
  };
}

// Mirrors getCalibration exactly, scoped to eth_predictions.
async function getEthCalibration(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT p_up, realized_up, p_up_experimental FROM eth_predictions WHERE realized_up IS NOT NULL AND horizon_hours = ?'
  ).bind(horizonHours).all();
  const n = results.length;
  if (n === 0) return { ok: true, n_resolved: 0, note: `No resolved ETH ${horizonHours}h predictions yet — the model is brand new, this is expected.` };

  const accuracy = results.filter(r => (r.p_up >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = results.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / n;

  const upRate = results.filter(r => r.realized_up === 1).length / n;
  const brierAlways5050 = 0.25;
  const brierAlwaysBaseRate = results.reduce((s, r) => s + (upRate - r.realized_up) ** 2, 0) / n;
  const bestNaiveBrier = Math.min(brierAlways5050, brierAlwaysBaseRate);
  const beatsNaiveBaseline = brier < bestNaiveBrier;

  const withExperimental = results.filter(r => r.p_up_experimental != null);
  let experimentalComparison = { available: false, note: 'Not enough resolved predictions with the experimental variant logged yet.' };
  if (withExperimental.length >= 20) {
    const nExp = withExperimental.length;
    const accuracyExp = withExperimental.filter(r => (r.p_up_experimental >= 0.5) === (r.realized_up === 1)).length / nExp;
    const brierExp = withExperimental.reduce((s, r) => s + (r.p_up_experimental - r.realized_up) ** 2, 0) / nExp;
    const brierOrigSameSet = withExperimental.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / nExp;
    experimentalComparison = {
      available: true, n_resolved: nExp,
      accuracy_experimental: Number(accuracyExp.toFixed(3)), brier_experimental: Number(brierExp.toFixed(3)),
      brier_original_same_set: Number(brierOrigSameSet.toFixed(3)), experimental_wins: brierExp < brierOrigSameSet,
      note: brierExp < brierOrigSameSet
        ? 'The adaptive-K/weighted variant is currently outperforming the original on the same set of days.'
        : 'The original fixed-K/unweighted approach is still doing as well or better.',
    };
  }

  return {
    ok: true, n_resolved: n, accuracy: Number(accuracy.toFixed(3)), brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)), brier_baseline_5050: brierAlways5050,
    brier_baseline_up_rate: Number(brierAlwaysBaseRate.toFixed(3)), beats_naive_baseline: beatsNaiveBaseline,
    experimental_vs_original: experimentalComparison,
    note: n < 20
      ? `Only ${n} resolved predictions — this is noise at this size, not a real verdict. ETH's model is new; expect this for a while.`
      : beatsNaiveBaseline
        ? `Beats the best naive baseline (${bestNaiveBrier.toFixed(3)}) — a real, if modest, edge.`
        : `Does NOT beat the best naive baseline (${bestNaiveBrier.toFixed(3)}) — a constant guess would currently do as well or better.`,
  };
}

// Mirrors getCalibrationHistory exactly, scoped to eth_predictions.
async function getEthCalibrationHistory(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT resolved_ts, p_up, p_up_experimental, calibrated_p_up, realized_up FROM eth_predictions WHERE realized_up IS NOT NULL AND horizon_hours = ? ORDER BY resolved_ts ASC'
  ).bind(horizonHours).all();
  let sumOrig = 0, nOrig = 0, sumExp = 0, nExp = 0, sumCal = 0, nCal = 0;
  let correctOrig = 0, correctExp = 0, correctCal = 0;
  const points = results.map(r => {
    sumOrig += (r.p_up - r.realized_up) ** 2;
    nOrig++;
    if ((r.p_up > 0.5) === (r.realized_up === 1)) correctOrig++;
    if (r.p_up_experimental != null) {
      sumExp += (r.p_up_experimental - r.realized_up) ** 2;
      nExp++;
      if ((r.p_up_experimental > 0.5) === (r.realized_up === 1)) correctExp++;
    }
    if (r.calibrated_p_up != null) {
      sumCal += (r.calibrated_p_up - r.realized_up) ** 2;
      nCal++;
      if ((r.calibrated_p_up > 0.5) === (r.realized_up === 1)) correctCal++;
    }
    return {
      ts: r.resolved_ts,
      brier_original: Number((sumOrig / nOrig).toFixed(4)), n_original: nOrig, accuracy_original: Number((correctOrig / nOrig).toFixed(3)),
      brier_experimental: nExp > 0 ? Number((sumExp / nExp).toFixed(4)) : null, n_experimental: nExp, accuracy_experimental: nExp > 0 ? Number((correctExp / nExp).toFixed(3)) : null,
      brier_calibrated: nCal > 0 ? Number((sumCal / nCal).toFixed(4)) : null, n_calibrated: nCal, accuracy_calibrated: nCal > 0 ? Number((correctCal / nCal).toFixed(3)) : null,
    };
  });
  return { ok: true, points, naive_baseline_5050: 0.25 };
}

// Expanding cumulative Brier score over time — deliberately NOT another
// single snapshot number. The point is to see whether an apparent edge
// (like the experimental variant's early lead) is stable as more data
// comes in, or was a fluke from one market stretch — exactly the trap the
// V1 Track Record tile fell into (a 99% "hit rate" that just reflected one
// persistent-trend period, never actually tested against a reversal).
// Each point is "the Brier score using every resolved prediction up to and
// including this one," so early noise settles as the line matures instead
// of being judged on a single cherry-pickable snapshot.
async function getCalibrationHistory(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT resolved_ts, p_up, p_up_experimental, calibrated_p_up, calibrated_conditional_p_up, realized_up FROM predictions WHERE realized_up IS NOT NULL AND horizon_hours = ? ORDER BY resolved_ts ASC'
  ).bind(horizonHours).all();

  // Accuracy tracking added alongside the existing Brier tracking (not a
  // replacement) — for the Lab tab's combined multi-model history chart,
  // which needs the same metric (accuracy%) across k-NN and Challenger to
  // be comparable on one axis. Brier stays as the primary calibration
  // metric everywhere else that already reads this endpoint.
  //
  // calibrated_p_up scored here for the first time — it's been computed
  // and stored on every prediction for a while, but nothing ever actually
  // tracked whether it performs any differently from the raw p_up it's
  // derived from. Direct build for the "must adjust based on accumulated
  // experience, and prove it before it's trusted" goal — this is the
  // proving, not an assumption that it already helps.
  //
  // calibrated_conditional_p_up scored the same way, from its first
  // prediction on — direct build from the confirmed finding that global
  // decile calibration blends two populations with opposite calibration
  // needs. This is the actual test of whether condition-matching that
  // fixes it, not an assumption it does.
  let sumOrig = 0, nOrig = 0, sumExp = 0, nExp = 0, sumCal = 0, nCal = 0, sumCondCal = 0, nCondCal = 0;
  let correctOrig = 0, correctExp = 0, correctCal = 0, correctCondCal = 0;
  const points = results.map(r => {
    sumOrig += (r.p_up - r.realized_up) ** 2;
    nOrig++;
    if ((r.p_up > 0.5) === (r.realized_up === 1)) correctOrig++;
    if (r.p_up_experimental != null) {
      sumExp += (r.p_up_experimental - r.realized_up) ** 2;
      nExp++;
      if ((r.p_up_experimental > 0.5) === (r.realized_up === 1)) correctExp++;
    }
    if (r.calibrated_p_up != null) {
      sumCal += (r.calibrated_p_up - r.realized_up) ** 2;
      nCal++;
      if ((r.calibrated_p_up > 0.5) === (r.realized_up === 1)) correctCal++;
    }
    if (r.calibrated_conditional_p_up != null) {
      sumCondCal += (r.calibrated_conditional_p_up - r.realized_up) ** 2;
      nCondCal++;
      if ((r.calibrated_conditional_p_up > 0.5) === (r.realized_up === 1)) correctCondCal++;
    }
    return {
      ts: r.resolved_ts,
      brier_original: Number((sumOrig / nOrig).toFixed(4)),
      n_original: nOrig,
      accuracy_original: Number((correctOrig / nOrig).toFixed(3)),
      brier_experimental: nExp > 0 ? Number((sumExp / nExp).toFixed(4)) : null,
      n_experimental: nExp,
      accuracy_experimental: nExp > 0 ? Number((correctExp / nExp).toFixed(3)) : null,
      brier_calibrated: nCal > 0 ? Number((sumCal / nCal).toFixed(4)) : null,
      n_calibrated: nCal,
      accuracy_calibrated: nCal > 0 ? Number((correctCal / nCal).toFixed(3)) : null,
      brier_calibrated_conditional: nCondCal > 0 ? Number((sumCondCal / nCondCal).toFixed(4)) : null,
      n_calibrated_conditional: nCondCal,
      accuracy_calibrated_conditional: nCondCal > 0 ? Number((correctCondCal / nCondCal).toFixed(3)) : null,
    };
  });
  return { ok: true, points, naive_baseline_5050: 0.25 };
}

// Rebuilds the decile calibration curve for one coin/horizon from every
// currently-resolved prediction, replacing the previous batch for that
// coin/horizon (old rows kept under their own computed_ts for history,
// getLatestCalibrationCurve only ever reads the newest batch). Cheap and
// safe to call manually — cron runs it once daily (see scheduled()).
async function refreshCalibrationCurve(env, coin, horizonHours) {
  // Explicit per-coin mapping, not a binary ternary — a ternary defaulting
  // anything-not-LINK to 'predictions' would have silently pointed ETH at
  // BTC's own table, corrupting ETH's calibration curve with BTC's data.
  const table = coin === 'LINK' ? 'link_predictions' : coin === 'ETH' ? 'eth_predictions' : 'predictions';
  const { results } = await env.DB.prepare(
    `SELECT p_up, realized_up FROM ${table} WHERE realized_up IS NOT NULL AND horizon_hours = ?`
  ).bind(horizonHours).all();
  if (results.length < 20) {
    return { ok: true, coin, horizon_hours: horizonHours, status: 'insufficient_data', n_resolved: results.length, min_required: 20 };
  }
  const curve = buildCalibrationCurve(results);
  const computedTs = Date.now();
  for (const row of curve) {
    await env.DB.prepare(
      `INSERT INTO calibration_curve (coin, horizon_hours, decile, predicted_p_up_mid, empirical_up_rate, n_samples, computed_ts)
       VALUES (?,?,?,?,?,?,?)`
    ).bind(coin, horizonHours, row.decile, row.predicted_p_up_mid, row.empirical_up_rate, row.n_samples, computedTs).run();
  }
  return { ok: true, coin, horizon_hours: horizonHours, status: 'ok', n_resolved: results.length, n_buckets: curve.length, computed_ts: computedTs };
}

// =====================================================================
// ---- Continuous-Learning Engine: daily audit metrics ----
// See .ai/DAILY_AUDIT.md and .ai/DATA_CONTRACT.md for the contract this
// implements. Pure, unit-testable functions below; D1-coupled
// orchestration (buildDailyReport) further down. Nothing here writes to
// or alters predictions/link_predictions/eth_predictions/challenger_predictions
// — this is a read-only measurement layer over data those models already
// produced.
// =====================================================================

const LEARNING_MIN_SAMPLE = 20; // same bar refreshCalibrationCurve already uses -- not a new threshold invented for this

function computeBrierScore(rows) {
  if (!rows.length) return null;
  const sum = rows.reduce((s, r) => s + (r.p - r.realized_up) ** 2, 0);
  return sum / rows.length;
}

function computeLogLoss(rows) {
  if (!rows.length) return null;
  const EPS = 1e-9;
  const sum = rows.reduce((s, r) => {
    const p = Math.min(1 - EPS, Math.max(EPS, r.p));
    return s - (r.realized_up * Math.log(p) + (1 - r.realized_up) * Math.log(1 - p));
  }, 0);
  return sum / rows.length;
}

function computeDirectionalAccuracy(rows) {
  if (!rows.length) return null;
  const correct = rows.filter(r => (r.p >= 0.5 ? 1 : 0) === r.realized_up).length;
  return correct / rows.length;
}

// Reuses buildCalibrationCurve exactly as-is (same decile logic already
// proven for the live /calibration route) rather than a second, possibly
// inconsistent calibration-error formula.
function computeCalibrationError(rows) {
  if (rows.length < LEARNING_MIN_SAMPLE) return null;
  const curve = buildCalibrationCurve(rows.map(r => ({ p_up: r.p, realized_up: r.realized_up })));
  const totalN = curve.reduce((s, c) => s + c.n_samples, 0);
  if (!totalN) return null;
  const weighted = curve.reduce((s, c) => s + c.n_samples * Math.abs(c.predicted_p_up_mid - c.empirical_up_rate), 0);
  return weighted / totalN;
}

const CONFIDENCE_BUCKET_BOUNDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 1.001];

// Buckets by DISTANCE FROM 50/50 (i.e. how confident the call was),
// matching .ai/DAILY_AUDIT.md's literal bucket list (0.50-0.55 ... 0.80+),
// which only makes sense as a confidence scale, not a raw p_up scale.
function bucketByConfidence(rows) {
  const buckets = [];
  for (let i = 0; i < CONFIDENCE_BUCKET_BOUNDS.length - 1; i++) {
    const lo = CONFIDENCE_BUCKET_BOUNDS[i], hi = CONFIDENCE_BUCKET_BOUNDS[i + 1];
    buckets.push({ range: hi > 1 ? `${lo.toFixed(2)}+` : `${lo.toFixed(2)}-${hi.toFixed(2)}`, lo, hi, n: 0, correct: 0 });
  }
  for (const r of rows) {
    const conf = Math.max(r.p, 1 - r.p);
    const b = buckets.find(b => conf >= b.lo && conf < b.hi);
    if (b) { b.n++; if ((r.p >= 0.5 ? 1 : 0) === r.realized_up) b.correct++; }
  }
  return buckets.map(b => ({
    range: b.range,
    n: b.n,
    accuracy: b.n ? Number((b.correct / b.n).toFixed(3)) : null,
    // "predicted confidence exceeds realized accuracy by >5pts" -- a fixed,
    // documented threshold, not a statistically-derived significance test.
    // Flags direction for a human/ChatGPT to investigate, doesn't itself
    // conclude miscalibration.
    overconfident_flag: b.n ? ((b.lo + Math.min(b.hi, 1)) / 2 - (b.correct / b.n)) > 0.05 : null,
  }));
}

function mostConfidentMistakes(rows, limit = 5) {
  return rows
    .filter(r => (r.p >= 0.5 ? 1 : 0) !== r.realized_up)
    .map(r => ({ ts: r.ts, p: Number(r.p.toFixed(3)), confidence: Number(Math.max(r.p, 1 - r.p).toFixed(3)), realized_up: r.realized_up, horizon_hours: r.horizon_hours }))
    .sort((a, b) => b.confidence - a.confidence)
    .slice(0, limit);
}

// Heuristic regime tags derived ONLY from fields the core models already
// store per prediction (volatility_percentile, trend_strength,
// is_regime_anomaly) -- no new inputs, no effect on any prediction.
// Thresholds are a documented first pass for reporting purposes, not a
// statistically fitted boundary; revisit once enough regime-split evidence
// accumulates (see .ai/DAILY_AUDIT.md section 5).
function classifyRegime(row) {
  let trend_regime = 'neutral';
  if (row.trend_strength != null) {
    if (row.trend_strength > 0.15) trend_regime = 'bullish';
    else if (row.trend_strength < -0.15) trend_regime = 'bearish';
  }
  let vol_regime = 'normal_volatility';
  if (row.volatility_percentile != null) {
    if (row.volatility_percentile >= 0.85) vol_regime = 'high_volatility';
    else if (row.volatility_percentile <= 0.15) vol_regime = 'low_volatility';
  }
  return { trend_regime, vol_regime, is_anomaly: !!row.is_regime_anomaly };
}

function groupByRegime(rows) {
  const groups = {};
  for (const r of rows) {
    const { trend_regime, vol_regime, is_anomaly } = classifyRegime(r);
    for (const key of [`trend:${trend_regime}`, `volatility:${vol_regime}`, is_anomaly ? 'anomaly:yes' : 'anomaly:no']) {
      (groups[key] ||= []).push(r);
    }
  }
  const out = {};
  for (const key of Object.keys(groups)) out[key] = summarizeRows(groups[key]);
  return out;
}

// Single insufficient-sample gate reused everywhere in this engine so the
// "explicitly report insufficient evidence, never manufacture a
// conclusion" rule can't accidentally be skipped in one call site.
function summarizeRows(rows, minSample = LEARNING_MIN_SAMPLE) {
  if (rows.length < minSample) {
    return { status: 'insufficient_data', n: rows.length, min_required: minSample };
  }
  return {
    status: 'ok',
    n: rows.length,
    accuracy: Number(computeDirectionalAccuracy(rows).toFixed(4)),
    brier_score: Number(computeBrierScore(rows).toFixed(4)),
    log_loss: Number(computeLogLoss(rows).toFixed(4)),
    calibration_error: computeCalibrationError(rows) != null ? Number(computeCalibrationError(rows).toFixed(4)) : null,
    realized_up_rate: Number((rows.reduce((s, r) => s + r.realized_up, 0) / rows.length).toFixed(4)),
    avg_predicted_p_up: Number((rows.reduce((s, r) => s + r.p, 0) / rows.length).toFixed(4)),
  };
}

// ---- Catalyst timestamp integrity (.ai/MARKET_CATALYST.md) ----
// T1 <= T0 => available_before_prediction = true. Pure function, the only
// place this comparison is allowed to happen, so it can't drift between
// call sites.
function classifyCatalystTiming(catalystEventTs, predictionTs) {
  if (catalystEventTs == null || predictionTs == null) return null;
  return catalystEventTs <= predictionTs;
}

// ---- Model drift (.ai/DAILY_AUDIT.md section 10) ----
function computeDrift(rows, nowTs) {
  const DAY = 24 * 3600 * 1000;
  const windows = { last_24h: nowTs - DAY, last_7d: nowTs - 7 * DAY, last_30d: nowTs - 30 * DAY, full_history: 0 };
  const out = {};
  for (const [label, cutoff] of Object.entries(windows)) {
    out[label] = summarizeRows(rows.filter(r => r.resolved_ts != null && r.resolved_ts >= cutoff));
  }
  // Flag only when both windows have enough evidence to compare -- an
  // insufficient-data window is never silently treated as "no drift".
  let flag = null;
  if (out.last_7d.status === 'ok' && out.full_history.status === 'ok') {
    const delta = out.last_7d.accuracy - out.full_history.accuracy;
    flag = Math.abs(delta) > 0.15 ? { deviation: Number(delta.toFixed(4)), note: 'last 7d accuracy differs from full-history accuracy by >15pts' } : null;
  }
  return { windows: out, flag };
}

// ---- Market catalyst layer (.ai/MARKET_CATALYST.md) ----
// Schema + data contract only, per IMPLEMENTATION_PLAN Phase 6 -- this is
// NOT an automatic ingestion pipeline and catalysts are NOT fed into any
// prediction. recordCatalyst exists so there's a single correct way to
// write a row (human-entered, or a future explicitly-scoped ingestion job);
// nothing currently calls it automatically. available_before_prediction is
// deliberately NOT computed here -- it depends on WHICH prediction a
// catalyst is being compared against (classifyCatalystTiming above), so a
// catalyst row itself only stores its own timestamps, not a precomputed
// verdict that would silently go stale as it's reused against different predictions.
async function recordCatalyst(env, opts) {
  const { coin, ts, category, direction, priceMovePct, headlineSource, sourceUrl, extractedReason, discoveryTimestamp, confidence, marketClassification, firstPublicTimestamp, investigationId, sourceGrounded, timestampSource, timestampConfidence } = opts;
  const insert = await env.DB.prepare(
    `INSERT INTO coin_catalyst_log
     (ts, coin, price_move_pct, headline_source, extracted_reason, category, direction, source_url, discovery_timestamp, confidence, market_classification, first_public_timestamp, investigation_id, source_grounded, timestamp_source, timestamp_confidence)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    ts, coin, priceMovePct ?? null, headlineSource ?? null, extractedReason ?? null,
    category ?? null, direction ?? null, sourceUrl ?? null, discoveryTimestamp ?? null,
    confidence ?? null, marketClassification ?? null, firstPublicTimestamp ?? null, investigationId ?? null,
    sourceGrounded == null ? null : (sourceGrounded ? 1 : 0), timestampSource ?? null, timestampConfidence ?? null
  ).run();
  return { ok: true, id: insert.meta.last_row_id };
}

// Read-only. `ts` is used as the period filter (when the catalyst was
// logged) since it's the one column guaranteed populated on every row,
// including the 2 pre-existing V1 rows that predate this contract and
// were left untouched by the migration (their new columns are NULL,
// reported as-is, never backfilled with guessed values).
async function fetchCatalystsForPeriod(env, sinceTs, untilTs) {
  let sql = `SELECT id, ts, coin, category, direction, price_move_pct, source_url, headline_source,
                    discovery_timestamp, confidence, market_classification
             FROM coin_catalyst_log`;
  const params = [];
  if (sinceTs != null && untilTs != null) {
    sql += ' WHERE ts >= ? AND ts < ?';
    params.push(sinceTs, untilTs);
  }
  sql += ' ORDER BY ts DESC LIMIT 100';
  const { results } = await env.DB.prepare(sql).bind(...params).all();
  return results;
}

// =====================================================================
// ---- Gemini Market Intelligence: PLANNING-ONLY building blocks ----
// See .ai/GEMINI_MARKET_INTELLIGENCE.md. NONE of the functions below are
// called from any HTTP route or from scheduled() -- they exist so the
// deterministic, side-effect-free parts of the eventual integration
// (trigger thresholds, payload validation, deduplication) can be designed,
// reviewed, and unit-tested BEFORE any Gemini API call, D1 write from this
// path, or Worker route is added. See learning/GEMINI_IMPLEMENTATION_PLAN.md
// for where these are intended to be wired in, once that plan is reviewed
// and a separate PR implements the actual integration.
// =====================================================================

const GEMINI_TRIGGER_CONFIG = {
  MARKET_MOVE_TRIGGER_PCT: 3,          // trailing-window price move %, see plan doc for which window
  HIGH_CONFIDENCE_TRIGGER: 0.85,       // retained for shouldTriggerInvestigation (superseded design, see below) -- NOT the doc's example 0.75, see plan doc's call-volume analysis for why
  MULTI_ASSET_TRIGGER_COUNT: 3,
  // ---- CANARY CONFIG -- deliberately 1/1, not the design's 8/2 ----
  // Set for a single controlled production canary call per explicit
  // human go-ahead. The objective of this canary is NOT to gather useful
  // catalyst data yet -- it's to verify the full chain (scheduled() ->
  // candidate -> ranking -> budget -> Gemini -> grounding -> validation ->
  // D1) actually works end-to-end against the real API. Do NOT raise these
  // back to the design values (8/2) without a fresh, explicit go-ahead
  // after the canary result has been independently audited.
  MAX_GEMINI_INVESTIGATIONS_PER_DAY: 1,
  MAX_GEMINI_INVESTIGATIONS_PER_HOUR: 1,
  MAX_ASSETS_PER_INVESTIGATION: 3,
};

// Provisional weights for computeInvestigationPriority below -- a starting
// point for design review, explicitly NOT a fitted or validated scoring
// model. See learning/GEMINI_IMPLEMENTATION_PLAN.md section 2 for the
// worked examples these were chosen to make sense against, and the
// implementation PR should revisit these once real trigger data exists.
const INVESTIGATION_PRIORITY_WEIGHTS = {
  priceMovePct: 0.5,            // per absolute percentage point of the largest affected asset's move
  confidenceAdjustedError: 3,   // 0..1 range (see computeInvestigationPriority) -- contributes 0..3
  correlatedAssetCount: 2,      // per asset beyond the first that failed together
  volatilityAnomaly: 1.5,       // flat bonus if today's setup was flagged is_regime_anomaly
  repeatedFailureCount: 0.5,    // per additional recent failure in the trailing window
  regimeChangeFlag: 2,          // flat bonus if a regime-change signal fired
};

// Below this score, an event is LOW priority and should be skipped without
// calling Gemini -- matches ChatGPT's "Normal error -> skip" branch.
// Provisional, same caveat as the weights above.
const INVESTIGATION_PRIORITY_THRESHOLD = 4;

const ALLOWED_CATALYST_CATEGORIES = [
  'MACRO', 'FED_RATES', 'INFLATION', 'EMPLOYMENT', 'USD', 'ETF_FLOWS', 'REGULATION',
  'EXCHANGE', 'STABLECOIN', 'LIQUIDATION', 'LEVERAGE', 'TECHNICAL', 'ON_CHAIN',
  'GEOPOLITICAL', 'SECURITY', 'PROTOCOL', 'OTHER',
];

const ALLOWED_MARKET_CLASSIFICATIONS = ['MARKET_WIDE', 'SECTOR_SPECIFIC', 'ASSET_SPECIFIC', 'NO_CLEAR_CATALYST'];

// SUPERSEDED by computeInvestigationPriority/rankInvestigationCandidates
// below -- kept only for the tests already written against it and as a
// simple reference implementation of the design this PR's review rejected
// (a single OR-of-thresholds gate can't express "a 65% call during a
// 12%-correlated-multi-asset move matters more than a 90% call during a
// quiet market", which is exactly the distinction the review asked for).
// Do not wire this one in -- see the plan doc.
function shouldTriggerInvestigation(signals, config = GEMINI_TRIGGER_CONFIG) {
  const reasons = [];
  if (signals.priceMovePct != null && Math.abs(signals.priceMovePct) >= config.MARKET_MOVE_TRIGGER_PCT) {
    reasons.push(`price_move_${signals.priceMovePct >= 0 ? 'up' : 'down'}_${Math.abs(signals.priceMovePct).toFixed(1)}pct`);
  }
  if (signals.highConfidenceFailureConfidence != null && signals.highConfidenceFailureConfidence >= config.HIGH_CONFIDENCE_TRIGGER) {
    reasons.push(`high_confidence_failure_${signals.highConfidenceFailureConfidence.toFixed(2)}`);
  }
  if (signals.correlatedFailureAssetCount != null && signals.correlatedFailureAssetCount >= config.MULTI_ASSET_TRIGGER_COUNT) {
    reasons.push(`correlated_failures_${signals.correlatedFailureAssetCount}_assets`);
  }
  return { trigger: reasons.length > 0, reasons };
}

// Pure. Replaces shouldTriggerInvestigation's single-threshold-OR design
// with a continuous priority score, per PR #2 review. Deliberately keeps
// PREDICTION CONFIDENCE and INVESTIGATION PRIORITY separate: confidence
// only enters via confidenceAdjustedError, and even then it's gated on the
// prediction having actually been wrong -- a correct 90%-confidence call
// contributes nothing to priority no matter how confident it was, while a
// wrong 65%-confidence call during a large correlated multi-asset move can
// score far higher. See learning/GEMINI_IMPLEMENTATION_PLAN.md section 2
// for three fully worked examples this was calibrated against.
//
// signals:
//   priceMovePct              abs % move of the largest-moving affected asset
//   wasWrong                  boolean -- did the prediction miss?
//   confidence                0.5-1.0, max(p, 1-p) at prediction time
//   correlatedFailureAssetCount  how many assets failed together (0/1 = not correlated)
//   isVolatilityAnomaly       boolean, e.g. today's is_regime_anomaly flag
//   recentFailureCount        count of recent failures in a trailing window
//   isRegimeChange            boolean
function computeInvestigationPriority(signals, weights = INVESTIGATION_PRIORITY_WEIGHTS) {
  const confidenceAdjustedError = signals.wasWrong && signals.confidence != null
    ? Math.max(0, (signals.confidence - 0.5) * 2)
    : 0;
  const score =
    weights.priceMovePct * Math.abs(signals.priceMovePct || 0) +
    weights.confidenceAdjustedError * confidenceAdjustedError +
    weights.correlatedAssetCount * Math.max(0, (signals.correlatedFailureAssetCount || 0) - 1) +
    weights.volatilityAnomaly * (signals.isVolatilityAnomaly ? 1 : 0) +
    weights.repeatedFailureCount * (signals.recentFailureCount || 0) +
    weights.regimeChangeFlag * (signals.isRegimeChange ? 1 : 0);
  return score;
}

// Pure. LOW/HIGH classification per ChatGPT's proposed
// "Normal error -> skip / Anomaly-error -> Investigation Score -> HIGH -> Gemini"
// branch. threshold is provisional -- see INVESTIGATION_PRIORITY_THRESHOLD.
function isHighInvestigationPriority(score, threshold = INVESTIGATION_PRIORITY_THRESHOLD) {
  return score >= threshold;
}

// Pure. Ranks candidate events by investigation priority, highest first --
// "process the highest-value candidates first" rather than investigating
// every high-confidence failure. Each candidate must carry an `id` (or
// similar caller-defined identifier) and a `signals` object as consumed by
// computeInvestigationPriority.
function rankInvestigationCandidates(candidates, weights = INVESTIGATION_PRIORITY_WEIGHTS) {
  return candidates
    .map(c => ({ ...c, priority: Number(computeInvestigationPriority(c.signals, weights).toFixed(3)) }))
    .sort((a, b) => b.priority - a.priority);
}

// Pure. How many more investigations can run right now, the smaller of the
// daily and hourly remaining allowance. Separate from withinGeminiRateLimit
// (a simple yes/no gate, still useful on its own) because ranking needs a
// COUNT to slice the ranked list by, not just a boolean.
function remainingGeminiBudget({ investigationsToday, investigationsThisHour }, config = GEMINI_TRIGGER_CONFIG) {
  const dailyRemaining = Math.max(0, config.MAX_GEMINI_INVESTIGATIONS_PER_DAY - (investigationsToday || 0));
  const hourlyRemaining = Math.max(0, config.MAX_GEMINI_INVESTIGATIONS_PER_HOUR - (investigationsThisHour || 0));
  return Math.min(dailyRemaining, hourlyRemaining);
}

// Pure. Takes a ranked candidate list (highest priority first, from
// rankInvestigationCandidates) and a budget count, and splits it into what
// gets investigated now vs. deferred. Candidates below
// INVESTIGATION_PRIORITY_THRESHOLD are deferred regardless of remaining
// budget -- a LOW-priority event doesn't become worth investigating just
// because the budget happens to be free that hour.
function selectWithinBudget(rankedCandidates, budget, threshold = INVESTIGATION_PRIORITY_THRESHOLD) {
  const eligible = rankedCandidates.filter(c => c.priority >= threshold);
  return {
    selected: eligible.slice(0, Math.max(0, budget)),
    deferred: eligible.slice(Math.max(0, budget)).concat(rankedCandidates.filter(c => c.priority < threshold)),
  };
}

// Pure, given already-known counts (caller queries D1 for today's/this-hour's
// investigation count -- not done here, keeps this testable without a
// database). Bounds Gemini usage per .ai/GEMINI_MARKET_INTELLIGENCE.md's
// Rate Limiting section.
function withinGeminiRateLimit({ investigationsToday, investigationsThisHour }, config = GEMINI_TRIGGER_CONFIG) {
  if (investigationsToday >= config.MAX_GEMINI_INVESTIGATIONS_PER_DAY) return { allowed: false, reason: 'daily_limit_reached' };
  if (investigationsThisHour >= config.MAX_GEMINI_INVESTIGATIONS_PER_HOUR) return { allowed: false, reason: 'hourly_limit_reached' };
  return { allowed: true, reason: null };
}

// Pure. The exact three-state contract from PR #2 review: CryptoPulse (not
// Gemini) computes this, from first_public_timestamp vs prediction_timestamp
// ONLY -- never from event_timestamp, per .ai/MARKET_CATALYST.md's
// Deterministic Availability section. Returns the string 'unknown' (not
// null/undefined) when first_public_timestamp isn't established, matching
// the review's explicit spec, distinct from classifyCatalystTiming (PR #1)
// which is a generic two-timestamp comparator used elsewhere and returns
// null for "don't know" -- this function is specifically the
// available_before_prediction contract and always returns one of exactly
// three values.
function computeAvailableBeforePrediction(firstPublicTimestamp, predictionTimestamp) {
  if (firstPublicTimestamp == null) return 'unknown';
  return firstPublicTimestamp <= predictionTimestamp;
}

// Pure. Validates a candidate catalyst payload BEFORE it's ever written to
// D1 -- per .ai/GEMINI_MARKET_INTELLIGENCE.md's "Catalyst Validation"
// checklist (items 1-6; items 7-9, duplicate/fabrication detection, are
// separate concerns -- see isDuplicateCatalyst below; fabrication can't be
// mechanically detected, only guarded against by never inventing a value
// when Gemini returns null, which is a call-site discipline, not something
// this function can enforce).
function validateCatalystPayload(catalyst) {
  const errors = [];
  if (!catalyst.coin) errors.push('missing coin');
  if (!catalyst.category) errors.push('missing category');
  else if (!ALLOWED_CATALYST_CATEGORIES.includes(catalyst.category)) errors.push(`invalid category: ${catalyst.category}`);
  if (catalyst.marketClassification && !ALLOWED_MARKET_CLASSIFICATIONS.includes(catalyst.marketClassification)) {
    errors.push(`invalid market_classification: ${catalyst.marketClassification}`);
  }
  if (catalyst.sourceUrl != null && !/^https?:\/\/\S+$/.test(catalyst.sourceUrl)) {
    errors.push('source_url is not a well-formed http(s) URL');
  }
  // Timestamp ordering sanity checks -- generous tolerance for clock skew
  // (1h) since these come from an external LLM's own timestamp parsing,
  // not a system clock.
  const TOLERANCE_MS = 60 * 60 * 1000;
  if (catalyst.eventTimestamp != null && catalyst.firstPublicTimestamp != null) {
    if (catalyst.firstPublicTimestamp < catalyst.eventTimestamp - TOLERANCE_MS) {
      errors.push('first_public_timestamp is implausibly before event_timestamp');
    }
  }
  if (catalyst.firstPublicTimestamp != null && catalyst.discoveryTimestamp != null) {
    if (catalyst.discoveryTimestamp < catalyst.firstPublicTimestamp - TOLERANCE_MS) {
      errors.push('discovery_timestamp is implausibly before first_public_timestamp');
    }
  }
  return { valid: errors.length === 0, errors };
}

// Pure. "One market event + multiple affected assets" preferred over
// duplicate rows, per .ai/MARKET_CATALYST.md's Duplicate Detection section.
// Same coin + same category within toleranceMs of an existing catalyst's
// `ts` counts as a duplicate candidate.
function isDuplicateCatalyst(candidate, existingCatalysts, toleranceMs = 6 * 60 * 60 * 1000) {
  return existingCatalysts.some(existing =>
    existing.coin === candidate.coin &&
    existing.category === candidate.category &&
    Math.abs(existing.ts - candidate.ts) <= toleranceMs
  );
}

// =====================================================================
// ---- Market Intelligence: Analyst Relay is now the ONLY investigation
// mechanism ----
// The automated grounded API investigation (investigateMarketEvent,
// evaluateGeminiTriggers, a dedicated cron dispatch) was removed entirely
// -- see the git history for the full root-cause story: Google Search
// grounding requires a billing-enabled, funded Cloud project, and the
// prepayment balance kept depleting (RESOURCE_EXHAUSTED), regardless of
// how tightly the application-side budget was scoped. That's a cost this
// project no longer pays. Candidate detection and the rich shared
// investigation context are UNCHANGED and still real -- they just now
// feed a human copy-pasting into the free Gemini consumer app
// (/analyst-relay/prompt, /analyst-relay/submit) instead of a paid API
// call this Worker makes itself.
//
// Flow:
//   predictions already stored (by the six predictThenSelect calls)
//     -> buildInvestigationCandidates (read-only, this cycle's fresh signals)
//     -> rankInvestigationCandidates / selectWithinBudget (pure)
//     -> buildInvestigationContext (real multi-asset evidence, unchanged)
//     -> GET /analyst-relay/prompt shows it to a human
//     -> POST /analyst-relay/submit -> recordAnalystRelay
//          -> validateGeminiInvestigationResponse / validateCatalystPayload (pure)
//          -> isDuplicateCatalyst (pure)
//          -> recordCatalyst (D1 write)
//          -> analyst_relay_log (audit row, always written, success or failure)
//
// gemini_investigations (the old table) and its historical rows are left
// alone -- real record of the API-grounding attempts that were made and
// why they stopped. Nothing writes new rows to it anymore.
// =====================================================================


// ---- Candidate generation (read-only D1 queries) ----
// Builds one candidate per core asset from THIS cycle's freshly-resolved
// predictions (a ~3.5h trailing window -- slightly wider than one 3-hourly
// cron tick so a resolution that lands just before/after this invocation
// still gets picked up next cycle rather than silently skipped).
// Module-level, shared between buildInvestigationCandidates and the new
// buildInvestigationContext below -- was previously a local const inside
// buildInvestigationCandidates only, duplicated here would have meant two
// asset lists to keep in sync.
const INVESTIGATION_ASSETS = [
  { coin: 'BTC', table: 'predictions', dataTable: 'btc_data', priceCol: 'btc_price' },
  { coin: 'LINK', table: 'link_predictions', dataTable: 'link_data', priceCol: 'link_price' },
  { coin: 'ETH', table: 'eth_predictions', dataTable: 'eth_data', priceCol: 'eth_price' },
];
// 3.5h trailing, last 5 resolved cycles per asset -- the SAME window
// buildInvestigationCandidates already queried before this change (just
// discarded down to a single recentFailureCount integer). Deliberately not
// a new/larger window -- per the build spec, prefer existing resolved
// cycles over introducing something arbitrary.
const INVESTIGATION_WINDOW_MS = 3.5 * 3600 * 1000;

// =====================================================================
// ---- Shared investigation context builder ----
// The ONE place that gathers factual evidence for an investigation. Used
// by Analyst Relay (getAnalystRelayCandidate) -- kept as a genuinely
// shared builder (not folded back into that function) because it used to
// also serve the automated Gemini investigation, and keeping it separate
// means that division of labor is trivial to restore if the cost picture
// ever changes. formatContextForGemini/formatContextForAnalyst below
// still format the SAME context two ways for presentation, but only one
// consumer reads either of them now.
//
// Never fabricates a missing observation -- an asset with no resolved
// prediction in the window is marked available:false, not silently
// dropped or guessed.
// =====================================================================
async function buildInvestigationContext(env, candidate, nowTs) {
  const observations = {};

  for (const a of INVESTIGATION_ASSETS) {
    const { results } = await env.DB.prepare(
      `SELECT ts, resolved_ts, p_up, calibrated_p_up, realized_up, is_regime_anomaly
       FROM ${a.table} WHERE resolved_ts IS NOT NULL AND resolved_ts >= ? ORDER BY resolved_ts DESC LIMIT 5`
    ).bind(nowTs - INVESTIGATION_WINDOW_MS).all();

    if (!results.length) {
      observations[a.coin] = { available: false };
      continue;
    }

    const oldest = await env.DB.prepare(
      `SELECT ${a.priceCol} as price FROM ${a.dataTable} WHERE ts >= ? ORDER BY ts ASC LIMIT 1`
    ).bind(nowTs - INVESTIGATION_WINDOW_MS).first();
    const newest = await env.DB.prepare(
      `SELECT ${a.priceCol} as price FROM ${a.dataTable} ORDER BY ts DESC LIMIT 1`
    ).first();
    const actualMovePct = (oldest?.price && newest?.price) ? ((newest.price - oldest.price) / oldest.price) * 100 : null;

    const toCycle = (row) => {
      const p = row.calibrated_p_up ?? row.p_up;
      return {
        predictedDirection: p >= 0.5 ? 'UP' : 'DOWN',
        confidencePct: Math.round(Math.max(p, 1 - p) * 1000) / 10,
        actualDirection: row.realized_up === 1 ? 'UP' : 'DOWN',
        wasWrong: (p >= 0.5 ? 1 : 0) !== row.realized_up,
        isRegimeAnomaly: !!row.is_regime_anomaly,
        resolvedTs: row.resolved_ts,
      };
    };

    const latestCycle = toCycle(results[0]);
    observations[a.coin] = {
      available: true,
      ...latestCycle,
      actualMovePct, // trailing-window aggregate price delta, null if price data unavailable
      recentCycles: results.map(toCycle), // bounded to the same LIMIT 5 above, oldest-to-newest not required -- consumers sort if needed
    };
  }

  const correlatedFailureAssets = INVESTIGATION_ASSETS
    .map(a => a.coin)
    .filter(coin => observations[coin].available && observations[coin].wasWrong);

  const context = {
    candidateId: candidate.id,
    primaryAsset: candidate.assets[0],
    generatedTs: nowTs,
    windowMs: INVESTIGATION_WINDOW_MS,
    windowLabel: '3.5h trailing, last 5 resolved cycles per asset',
    observations, // always all three assets, available:false where no data exists -- never fabricated
    correlatedFailureAssetCount: correlatedFailureAssets.length,
    correlatedFailureAssets, // the actual list, not just a count
  };
  context.contextHash = await computeContextHash(context);
  return context;
}

// Pure-ish (crypto.subtle is available in the Workers runtime, no network,
// no secrets touched). SHA-256 over a canonical (sorted-key) JSON
// serialization of the DETERMINISTIC factual fields only -- deliberately
// excludes generatedTs, so two builds of the identical underlying D1 state
// hash the same even if built moments apart, and comparing hashes across a
// Gemini investigation and a later Analyst Relay submission is meaningful
// evidence they saw the same facts, not just that they ran at different
// times.
async function computeContextHash(context) {
  const canonical = {
    candidateId: context.candidateId,
    primaryAsset: context.primaryAsset,
    windowMs: context.windowMs,
    observations: context.observations,
    correlatedFailureAssetCount: context.correlatedFailureAssetCount,
    correlatedFailureAssets: context.correlatedFailureAssets,
  };
  const json = JSON.stringify(canonical, Object.keys(canonical).sort());
  const bytes = new TextEncoder().encode(json);
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// Pure. Formats the shared context for Gemini's grounded-search
// instruction. Same underlying observations as formatContextForAnalyst
// below -- only the framing instruction differs, never the facts.
function formatContextForGemini(context) {
  const lines = [];
  for (const coin of ['BTC', 'ETH', 'LINK']) {
    const o = context.observations[coin];
    if (!o.available) { lines.push(`${coin}: no resolved prediction in this window`); continue; }
    lines.push(`${coin}: predicted ${o.predictedDirection} (confidence ${o.confidencePct}%), actual ${o.actualDirection}${o.actualMovePct != null ? ` (${o.actualMovePct.toFixed(2)}% move)` : ''}, ${o.wasWrong ? 'WRONG' : 'correct'}${o.isRegimeAnomaly ? ', regime anomaly flagged' : ''}`);
  }
  const historyLines = [];
  for (const coin of ['BTC', 'ETH', 'LINK']) {
    const o = context.observations[coin];
    if (!o.available || o.recentCycles.length <= 1) continue;
    historyLines.push(`${coin} recent cycles: ` + o.recentCycles.map(c => `${c.predictedDirection}/${c.actualDirection}${c.wasWrong ? '(wrong)' : ''}`).join(', '));
  }

  return `You are a market intelligence analyst investigating a specific crypto market event. Use web search to find real, current, verifiable sources -- do not rely on general knowledge alone for this task.

EVENT CONTEXT (already computed by CryptoPulse, not your job to re-derive):
Primary asset under investigation: ${context.primaryAsset}
Window: ${context.windowLabel}

Cross-asset observations this cycle:
${lines.join('\n')}

Correlated failures: ${context.correlatedFailureAssetCount} asset(s) wrong this cycle${context.correlatedFailureAssetCount ? ` (${context.correlatedFailureAssets.join(', ')})` : ''}.
${historyLines.length ? '\nRecent history:\n' + historyLines.join('\n') : ''}

TASK: Investigate what likely caused this price movement, considering both the primary asset and whether the cross-asset pattern above suggests a market-wide event rather than an asset-specific one. Search for real news, announcements, or events from credible sources (official announcements, regulatory sources, established financial/crypto news outlets) published around this time window.

RULES:
- Only report a catalyst if you find credible, verifiable evidence. If you cannot find a credible catalyst, return an empty catalysts array -- do not invent one.
- Never fabricate a source URL, publisher name, or timestamp. If a timestamp cannot be verified, omit that field (do not guess).
- For each catalyst, report event_timestamp (when it happened) and first_public_timestamp (when credible public information became available) as SEPARATE fields if you can establish them -- they are often different.
- Report first_public_timestamp_confidence as exactly one of HIGH, MEDIUM, LOW, or UNKNOWN, reflecting how confident you are in the first_public_timestamp value specifically (not your confidence in the catalyst overall). If first_public_timestamp is null, this must be UNKNOWN.
- Category must be exactly one of: MACRO, FED_RATES, INFLATION, EMPLOYMENT, USD, ETF_FLOWS, REGULATION, EXCHANGE, STABLECOIN, LIQUIDATION, LEVERAGE, TECHNICAL, ON_CHAIN, GEOPOLITICAL, SECURITY, PROTOCOL, OTHER.
- market_classification must be exactly one of: MARKET_WIDE, SECTOR_SPECIFIC, ASSET_SPECIFIC, NO_CLEAR_CATALYST.

Respond with ONLY valid JSON, no other text, in exactly this shape:
{"investigation_id":"","assets":${JSON.stringify([context.primaryAsset])},"market_classification":"","catalysts":[{"category":"","event_timestamp":null,"first_public_timestamp":null,"first_public_timestamp_confidence":"HIGH|MEDIUM|LOW|UNKNOWN","direction":"","confidence":"HIGH|MEDIUM|LOW","description":"","assets":[],"sources":[{"title":"","publisher":"","url":"","published_at":null}]}]}`;
}

// Pure. Same factual content as formatContextForGemini -- section headers
// differ (human-readable "Review this evidence" vs Gemini's grounded-
// search instruction) but every observation, every number, is identical.
// Returns structured pieces (not one flat string) so the frontend can
// render FACTUAL DATA and the copy-paste prompt as visually separate
// blocks, per the build spec's explicit UI requirement.
function formatContextForAnalyst(context) {
  const factualLines = [];
  for (const coin of ['BTC', 'ETH', 'LINK']) {
    const o = context.observations[coin];
    if (!o.available) { factualLines.push(`${coin}: no resolved prediction in this window`); continue; }
    factualLines.push(`${coin}: predicted ${o.predictedDirection} (${o.confidencePct}% confidence) -> actual ${o.actualDirection}${o.actualMovePct != null ? ` (${o.actualMovePct.toFixed(2)}%)` : ''} -- ${o.wasWrong ? 'WRONG' : 'correct'}${o.isRegimeAnomaly ? ' [regime anomaly]' : ''}`);
  }
  const factualSummary = `Primary asset: ${context.primaryAsset}\nWindow: ${context.windowLabel}\n\n${factualLines.join('\n')}\n\nCorrelated failures: ${context.correlatedFailureAssetCount} asset(s)${context.correlatedFailureAssetCount ? ` (${context.correlatedFailureAssets.join(', ')})` : ''}`;

  return {
    factualSummary, // for the UI's separate "FACTUAL DATA" block
    prompt: formatContextForGemini(context), // same underlying facts, Gemini's own instruction framing -- still what gets copy-pasted, since the human is relaying TO Gemini
  };
}


async function buildInvestigationCandidates(env, nowTs) {
  const resolvedByAsset = {};
  for (const a of INVESTIGATION_ASSETS) {
    const { results } = await env.DB.prepare(
      `SELECT ts, p_up, calibrated_p_up, realized_up, is_regime_anomaly
       FROM ${a.table} WHERE resolved_ts IS NOT NULL AND resolved_ts >= ? ORDER BY resolved_ts DESC LIMIT 5`
    ).bind(nowTs - INVESTIGATION_WINDOW_MS).all();
    resolvedByAsset[a.coin] = results;
  }

  const wasAssetWrong = (row) => {
    const p = row.calibrated_p_up ?? row.p_up;
    return (p >= 0.5 ? 1 : 0) !== row.realized_up;
  };
  const failedAssetCount = INVESTIGATION_ASSETS.filter(a => resolvedByAsset[a.coin].some(wasAssetWrong)).length;

  const candidates = [];
  for (const a of INVESTIGATION_ASSETS) {
    const rows = resolvedByAsset[a.coin];
    if (!rows.length) continue; // nothing resolved this window for this asset -- no candidate, not an error
    const latest = rows[0];
    const p = latest.calibrated_p_up ?? latest.p_up;
    const wasWrong = wasAssetWrong(latest);
    const confidence = Math.max(p, 1 - p);

    const oldest = await env.DB.prepare(
      `SELECT ${a.priceCol} as price FROM ${a.dataTable} WHERE ts >= ? ORDER BY ts ASC LIMIT 1`
    ).bind(nowTs - INVESTIGATION_WINDOW_MS).first();
    const newest = await env.DB.prepare(
      `SELECT ${a.priceCol} as price FROM ${a.dataTable} ORDER BY ts DESC LIMIT 1`
    ).first();
    const priceMovePct = (oldest?.price && newest?.price)
      ? ((newest.price - oldest.price) / oldest.price) * 100
      : 0;

    candidates.push({
      id: a.coin,
      assets: [a.coin],
      signals: {
        priceMovePct,
        wasWrong,
        confidence,
        correlatedFailureAssetCount: failedAssetCount,
        isVolatilityAnomaly: !!latest.is_regime_anomaly,
        recentFailureCount: rows.filter(wasAssetWrong).length,
        // No clean existing regime-change signal yet (see plan doc's
        // "Recommended next steps" #4) -- defaulting to false rather than
        // fabricating a heuristic. Documented, not silently guessed.
        isRegimeChange: false,
      },
    });
  }
  return candidates;
}

// ---- Budget accounting (read-only D1 query). SUPERSEDED for the live
// path by peekGeminiQuotaRemaining/reserveGeminiQuotaSlot (shared-quota
// gate, see below) -- kept only because remainingGeminiBudget is still
// unit-tested against it and it's a reasonable rolling-window reference
// implementation. Do not wire this back in without also reconciling it
// with the shared ledger, or the two budgets would silently disagree. ----
async function getGeminiInvestigationCounts(env, nowTs) {
  const DAY_MS = 24 * 3600 * 1000, HOUR_MS = 3600 * 1000;
  const dayRow = await env.DB.prepare('SELECT COUNT(*) as n FROM gemini_investigations WHERE request_ts >= ?').bind(nowTs - DAY_MS).first();
  const hourRow = await env.DB.prepare('SELECT COUNT(*) as n FROM gemini_investigations WHERE request_ts >= ?').bind(nowTs - HOUR_MS).first();
  return { investigationsToday: dayRow?.n || 0, investigationsThisHour: hourRow?.n || 0 };
}

// =====================================================================
// ---- Shared Gemini quota gate ----
// All THREE Gemini consumers in this Worker (market-intelligence
// investigation, BTC daily narrative, LINK daily narrative) call the same
// model under the same env.GEMINI_API_KEY. Per Google's own docs, rate
// limits are enforced PER PROJECT, NOT PER API KEY -- so these three
// consumers silently share one provider-side quota pool even though, before
// this change, only the investigation consumer had any application-level
// budget at all. Root cause write-up: the investigation's own 1/day+1/hour
// budget was never exceeded, but /run-analysis and /run-link-analysis had
// NO budget whatsoever and fire on every app boot, so they could exhaust
// the shared pool before the investigation's tightly-budgeted call ever got
// its turn.
//
// Design: one shared ledger table (gemini_quota_ledger), with a separate
// row per (consumer, bucket_type, bucket_key). "consumer" is not just the
// three features -- each of the two narrative features gets a CRON lane
// and a separate MANUAL lane (see GEMINI_SHARED_QUOTA_CONFIG), so the
// 07:00 UTC daily cron always has its own guaranteed slot that manual
// "Run Analysis" clicks / app-boot calls can never exhaust -- directly
// satisfying "don't let routine narrative generation starve the daily
// analyses OR crowd out investigation capacity" without needing a real
// priority queue.
//
// Buckets are FIXED UTC calendar day/hour windows (e.g. 'day:2026-08-21',
// 'hour:2026-08-21T09'), not rolling windows -- deliberately different
// from the old investigation consumer's rolling-24h COUNT(*) check
// (getGeminiInvestigationCounts/remainingGeminiBudget below, kept only as
// tested reference functions -- nothing live calls them anymore since the
// automated investigation was removed). Fixed buckets are what make the
// reservation atomic with a single UPDATE ... WHERE ... RETURNING statement: SQLite/D1
// serializes writes to a given row, so two concurrent reservations against
// the same bucket can never both read "under cap" and both proceed -- a
// rolling-window COUNT-then-INSERT can't offer that guarantee without a
// separate lock. The numeric caps (1/day, 1/hour) for the investigation
// consumer are UNCHANGED from GEMINI_TRIGGER_CONFIG -- only the mechanism
// enforcing them is now shared infrastructure.
// =====================================================================

// NOT derived from a verified Google AI Studio project quota number -- see
// the root-cause audit's Phase 10 finding (GOOGLE LIVE QUOTA NUMBERS NOT
// VERIFIED; the actual live RPM/RPD for this project is only visible at
// https://aistudio.google.com/rate-limit, which this environment has no
// access to). These are conservative, clearly-provisional internal safety
// ceilings: 1 guaranteed cron slot/day for each narrative feature, plus a
// small amount of headroom for manual/app-boot triggers, each still capped
// at 1/hour so a burst of page loads can't consume a day's allowance in a
// few minutes. Revisit once the real project quota is confirmed.
const GEMINI_SHARED_QUOTA_CONFIG = {
  btc_narrative_cron: { day: 1, hour: 1 },
  btc_narrative_manual: { day: 3, hour: 1 },
  link_narrative_cron: { day: 1, hour: 1 },
  link_narrative_manual: { day: 3, hour: 1 },
};

// Pure. UTC calendar-day key, e.g. 1787302854816 -> '2026-08-21'.
function utcDayBucket(ts) {
  return new Date(ts).toISOString().slice(0, 10);
}

// Pure. UTC calendar-hour key, e.g. 1787302854816 -> '2026-08-21T09'.
function utcHourBucket(ts) {
  return new Date(ts).toISOString().slice(0, 13);
}

// Pure. Deterministic ledger row keys for a given consumer + instant.
// Namespaced by consumer so every lane in GEMINI_SHARED_QUOTA_CONFIG gets
// its own independent counters in the same shared table.
function buildQuotaBucketKeys(consumer, nowTs) {
  return {
    dayKey: `${consumer}:day:${utcDayBucket(nowTs)}`,
    hourKey: `${consumer}:hour:${utcHourBucket(nowTs)}`,
  };
}

// ---- Atomic reservation against the shared ledger (impure, D1). ----
// Reserves ONE slot for `consumer` from BOTH its day and hour bucket, or
// neither. Each bucket check is a single `UPDATE ... WHERE reserved < cap
// RETURNING reserved` statement -- SQLite/D1 serializes writes to a given
// row, so if two isolates race for the last slot, only one UPDATE's WHERE
// clause can still see `reserved < cap` by the time it actually runs; the
// other sees the already-incremented value and matches zero rows. That is
// the entire concurrency guarantee this function relies on -- it does not
// use or need an application-level lock.
//
// The day and hour checks are two separate statements (D1 Workers don't
// have interactive multi-statement transactions), so a day-reservation
// that succeeds followed by an hour-reservation that fails is explicitly
// compensated by decrementing the day bucket back down -- see the
// `dayResult` handling below. This makes the overall reservation
// effectively all-or-nothing even though it isn't a single atomic
// statement.
//
// Counts EVERY reservation, regardless of whether the Gemini call that
// follows succeeds, times out, or gets a provider 429 -- a provider request
// was attempted either way, per the root-cause audit's Phase 2 requirement.
// A rejected RESERVATION (this function returning admitted:false) is
// different: no network call is made at all, so nothing was attempted, and
// callers must record that distinctly (quota_decision, not a provider
// response_status) -- see recordGeminiProviderCall.
async function reserveGeminiQuotaSlot(env, consumer, config, nowTs) {
  const { dayKey, hourKey } = buildQuotaBucketKeys(consumer, nowTs);

  // Idempotent bucket creation. ON CONFLICT DO NOTHING means a race here is
  // harmless -- whichever insert loses just no-ops, the row already exists
  // with the same cap either way (this shared table is not repurposed for
  // per-request dynamic caps, so a stale cap on an existing row is not a
  // concern).
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO gemini_quota_ledger (bucket_key, consumer, bucket_type, reserved, cap, updated_ts)
       VALUES (?, ?, 'day', 0, ?, ?) ON CONFLICT(bucket_key) DO NOTHING`
    ).bind(dayKey, consumer, config.day, nowTs),
    env.DB.prepare(
      `INSERT INTO gemini_quota_ledger (bucket_key, consumer, bucket_type, reserved, cap, updated_ts)
       VALUES (?, ?, 'hour', 0, ?, ?) ON CONFLICT(bucket_key) DO NOTHING`
    ).bind(hourKey, consumer, config.hour, nowTs),
  ]);

  const dayResult = await env.DB.prepare(
    `UPDATE gemini_quota_ledger SET reserved = reserved + 1, updated_ts = ?
     WHERE bucket_key = ? AND reserved < cap RETURNING reserved`
  ).bind(nowTs, dayKey).all();
  if (!dayResult.results.length) {
    return { admitted: false, reason: 'daily_limit_reached' };
  }

  const hourResult = await env.DB.prepare(
    `UPDATE gemini_quota_ledger SET reserved = reserved + 1, updated_ts = ?
     WHERE bucket_key = ? AND reserved < cap RETURNING reserved`
  ).bind(nowTs, hourKey).all();
  if (!hourResult.results.length) {
    // Compensate: release the day slot this call just reserved, since the
    // overall reservation is being rejected. Floor at 0 defensively, even
    // though this call is the one that just incremented it, in case a
    // concurrent compensation from a different failed reservation is
    // racing on the same bucket.
    await env.DB.prepare(
      `UPDATE gemini_quota_ledger SET reserved = MAX(0, reserved - 1), updated_ts = ? WHERE bucket_key = ?`
    ).bind(nowTs, dayKey).run();
    return { admitted: false, reason: 'hourly_limit_reached' };
  }

  return { admitted: true, reason: null };
}

// ---- Cross-consumer observability (impure, D1). Never throws -- callers
// wrap this the same defensive way recordGeminiInvestigation already is.
// This is the table that answers "how many Gemini requests did the
// application actually make" (root-cause audit Phase 6/15), independent of
// and in addition to each consumer's own richer table
// (gemini_investigations / gemini_daily_analysis / link_gemini_analysis,
// all of which are UNCHANGED by this PR and still hold their
// success-specific content). quotaDecision is 'admitted' when a real
// network call was attempted, or 'deferred_daily' / 'deferred_hourly' when
// reserveGeminiQuotaSlot rejected the reservation and NO network call was
// made. httpStatus is null whenever no network call happened. ----
async function recordGeminiProviderCall(env, { correlationId, consumer, asset, requestTs, model, quotaDecision, httpStatus, responseStatus, errorCategory }) {
  await env.DB.prepare(
    `INSERT INTO gemini_provider_calls
     (correlation_id, consumer, asset, request_ts, provider, model, quota_decision, http_status, response_status, error_category, created_ts)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    correlationId, consumer, asset ?? null, requestTs, 'google_generative_language', model,
    quotaDecision, httpStatus ?? null, responseStatus, errorCategory ?? null, Date.now()
  ).run();
}

// ---- Shared low-level Gemini caller (impure, the only place either
// narrative consumer calls fetch() against Google). Consolidates what were
// originally two duplicated fetch blocks (no timeout, no consistent error
// classification) into one {ok, status, text, errorCategory} shape.
// Grounding (Google Search) support was removed entirely along with the
// automated Market Intelligence investigation that used it -- see the
// comment above INVESTIGATION_ASSETS for the full story. Nothing in this
// codebase requests it anymore. ----
const GEMINI_CALL_TIMEOUT_MS = 20000;

async function callGeminiGenerateContent(env, { model, prompt }) {
  if (!env.GEMINI_API_KEY) {
    return { ok: false, status: null, text: null, errorCategory: 'error', errorMessage: 'GEMINI_API_KEY not configured on this Worker' };
  }
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), GEMINI_CALL_TIMEOUT_MS);
  try {
    const body = { contents: [{ parts: [{ text: prompt }] }] };
    const res = await fetch(
      `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent`,
      { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-goog-api-key': env.GEMINI_API_KEY }, body: JSON.stringify(body), signal: controller.signal }
    );
    if (res.status === 429) {
      // Read the real body -- confirmed via V1's PulseWorker investigation
      // that a bare 429 can mean genuinely different things ("rate limited"
      // vs "RESOURCE_EXHAUSTED: prepayment credits depleted", which needs a
      // completely different fix). Previously hardcoded to a generic
      // string here, discarding exactly the detail that mattered.
      const errBody = await res.text().catch(() => '');
      return { ok: false, status: 429, text: null, errorCategory: 'rate_limited', errorMessage: `Gemini API 429: ${errBody.slice(0, 500)}` };
    }
    if (!res.ok) {
      const errBody = await res.text();
      return { ok: false, status: res.status, text: null, errorCategory: 'error', errorMessage: `Gemini API returned ${res.status}: ${errBody.slice(0, 500)}` };
    }
    const data = await res.json();
    const text = data?.candidates?.[0]?.content?.parts?.map(p => p.text).join('') ?? '';
    if (!text) {
      return { ok: false, status: res.status, text: null, errorCategory: 'malformed_response', errorMessage: 'Empty or unexpected Gemini response shape' };
    }
    return { ok: true, status: res.status, text, errorCategory: null, errorMessage: null };
  } catch (err) {
    const errorCategory = err.name === 'AbortError' ? 'timeout' : 'error';
    return { ok: false, status: null, text: null, errorCategory, errorMessage: String(err?.message || err).slice(0, 1000) };
  } finally {
    clearTimeout(timeout);
  }
}

// Pure. Maps a runGeminiDailyAnalysis/runLinkGeminiAnalysis result status to
// an honest HTTP status code -- the frontend (or anything reading this
// route directly) must be able to tell "we chose not to call Gemini right
// now" (429, quota_deferred) apart from "Gemini/the network genuinely
// failed" (502/504) apart from a real success (200). Never collapses these
// into a single generic 500 the way the old catch-all did.
function geminiStatusToHttpCode(status) {
  if (status === 'ok') return 200;
  if (status === 'quota_deferred' || status === 'rate_limited') return 429;
  if (status === 'timeout') return 504;
  if (status === 'malformed_response' || status === 'error') return 502;
  return 500;
}


// Pure. PR #2 review, BLOCKER 3: never invents a value. timestamp_source is
// 'gemini_reported' whenever a first_public_timestamp was actually provided
// (currently the only source of this data), or 'unknown' when it wasn't --
// there is no third case, since nothing else in this system populates
// first_public_timestamp yet. timestamp_confidence trusts Gemini's own
// reported confidence ONLY if it's one of the four allowed values;
// anything else (missing, malformed, hallucinated) is downgraded to
// 'UNKNOWN' rather than passed through or guessed. discovery_timestamp is
// never substituted for first_public_timestamp anywhere in this function or
// its callers -- see the dedicated regression test.
function deriveTimestampProvenance(firstPublicTimestamp, reportedConfidence) {
  if (firstPublicTimestamp == null) {
    return { timestampSource: 'unknown', timestampConfidence: 'UNKNOWN' };
  }
  const ALLOWED = ['HIGH', 'MEDIUM', 'LOW', 'UNKNOWN'];
  const timestampConfidence = ALLOWED.includes(reportedConfidence) ? reportedConfidence : 'UNKNOWN';
  return { timestampSource: 'gemini_reported', timestampConfidence };
}

// Pure. Strips markdown code fences if Gemini wraps its JSON despite
// instructions, then parses. Throws (caller catches) on genuinely malformed
// JSON -- never silently returns a partial/guessed structure.
function parseGeminiInvestigationResponse(rawText) {
  const cleaned = rawText.trim().replace(/^```json\s*/i, '').replace(/^```\s*/i, '').replace(/```\s*$/i, '').trim();
  return JSON.parse(cleaned);
}

// Pure. Structural validation of the parsed response, BEFORE any per-
// catalyst field validation (validateCatalystPayload handles that, per
// catalyst). Checks the shape Gemini must return, not the content quality.
function validateGeminiInvestigationResponse(parsed) {
  const errors = [];
  if (parsed == null || typeof parsed !== 'object') { return { valid: false, errors: ['response is not a JSON object'] }; }
  if (!Array.isArray(parsed.assets)) errors.push('missing or invalid assets array');
  if (parsed.market_classification != null && !ALLOWED_MARKET_CLASSIFICATIONS.includes(parsed.market_classification)) {
    errors.push(`invalid market_classification: ${parsed.market_classification}`);
  }
  if (parsed.catalysts != null && !Array.isArray(parsed.catalysts)) errors.push('catalysts must be an array');
  for (const catalyst of parsed.catalysts || []) {
    const sourceCheck = validateCatalystSources(catalyst.sources);
    if (!sourceCheck.valid) errors.push(...sourceCheck.errors);
  }
  return { valid: errors.length === 0, errors };
}

// Pure. Source validation per .ai/GEMINI_MARKET_INTELLIGENCE.md's "Source
// Requirements" -- every source needs at minimum a URL, and if a source
// array is present it must not be empty (a catalyst with zero sources is
// not evidence, per "Gemini must not invent URLs" -- an empty/missing
// source list should have produced NO_CLEAR_CATALYST instead of a catalyst
// entry at all).
function validateCatalystSources(sources) {
  const errors = [];
  if (!Array.isArray(sources) || sources.length === 0) {
    errors.push('catalyst has no sources -- a catalyst without at least one source is not valid evidence');
    return { valid: false, errors };
  }
  for (const [i, source] of sources.entries()) {
    if (!source.url) errors.push(`source[${i}] missing url`);
    else if (!/^https?:\/\/\S+$/.test(source.url)) errors.push(`source[${i}] url is not well-formed: ${source.url}`);
  }
  return { valid: errors.length === 0, errors };
}

// ---- Orchestrates one investigation end-to-end. NEVER throws -- every
// failure path (timeout, malformed response, rate limit, network error,
// validation failure, no credible catalyst) is caught and recorded as an
// audit row, never propagated. Callers (evaluateGeminiTriggers) rely on
// this never throwing so one candidate's failure can't stop the others in
// the same cycle. ----
// ---- Analyst Relay: the investigation mechanism. ----
// Was originally built as a fallback for when the paid grounded API path
// was rate-limited; the automated investigateMarketEvent/
// evaluateGeminiTriggers it was a fallback FOR has since been removed
// entirely (Google Search grounding requires a funded, billing-enabled
// Cloud project -- a recurring cost this project stopped paying). This is
// now the only investigation mechanism, not a fallback for anything.
//
// - Writes to its own table (analyst_relay_log) only -- NEVER to
//   gemini_investigations. That table and its historical rows are left
//   alone as a record of the grounded-API attempts that were made, but
//   nothing writes new rows to it anymore.
// - Uses a distinct ID prefix (AR-) on the catalysts it writes to
//   coin_catalyst_log, so real catalyst data stays usable by the rest of
//   the app (tiles, other reads) while remaining traceable to its source.
// - sourceGrounded is unconditionally false -- a pasted chat response has
//   no verifiable {searchQueries, groundedSources} the way a real API
//   response would, regardless of what the model may actually have done.
// - Never touches reserveGeminiQuotaSlot / GEMINI_SHARED_QUOTA_CONFIG --
//   this path was never budgeted, since it was never the thing that could
//   run up a bill.
// =====================================================================

// Read-only. Surfaces the same candidate the automated investigation would
// have picked up next, using the exact same ranking/threshold logic --
// "worth pasting into Gemini yourself" means the same thing "worth
// spending the automated budget on" does, deliberately not a separate,
// looser bar. selectWithinBudget's budget of 1 here just means "return at
// most one candidate for the single prompt slot the UI shows" -- it has
// nothing to do with any Gemini API quota.
async function getAnalystRelayCandidate(env) {
  const nowTs = Date.now();
  const candidates = await buildInvestigationCandidates(env, nowTs);
  if (candidates.length === 0) return { ok: true, hasCandidate: false };
  const ranked = rankInvestigationCandidates(candidates);
  const { selected } = selectWithinBudget(ranked, 1);
  if (selected.length === 0) return { ok: true, hasCandidate: false };
  const candidate = selected[0];
  // Same shared builder investigateMarketEvent uses -- see
  // buildInvestigationContext's own comment for why that's the point.
  const context = await buildInvestigationContext(env, candidate, nowTs);
  const { factualSummary, prompt } = formatContextForAnalyst(context);
  return {
    ok: true, hasCandidate: true,
    candidateId: candidate.id, assets: candidate.assets,
    promptRequestedTs: nowTs,
    factualSummary, // for the UI's separate FACTUAL DATA block
    prompt, // the copy-paste text -- same underlying facts as Gemini receives
    contextHash: context.contextHash,
    context, // full structured context, so recordAnalystRelay can persist exactly what was shown
  };
}

// Parses and records a human-pasted Gemini-app response. Reuses the exact
// same parse/validate/catalyst-write pipeline investigateMarketEvent uses
// for a real API response -- no separate parsing logic to maintain or
// drift out of sync.
async function recordAnalystRelay(env, { candidateId, assets, promptRequestedTs, rawResponseText, context }) {
  const relayId = `AR-${Date.now()}-${candidateId}`;
  const submittedTs = Date.now();
  let validationStatus = 'error', errorMessage = null, catalystsWritten = 0, parsed = null;

  try {
    parsed = parseGeminiInvestigationResponse(rawResponseText);
    const structureCheck = validateGeminiInvestigationResponse(parsed);

    if (!structureCheck.valid) {
      validationStatus = 'invalid_response';
      errorMessage = structureCheck.errors.join('; ');
    } else if (!parsed.catalysts || parsed.catalysts.length === 0) {
      validationStatus = 'no_catalyst_found';
    } else {
      validationStatus = 'ok';
      const existing = await fetchCatalystsForPeriod(env, submittedTs - 24 * 3600 * 1000, submittedTs + 1);
      for (const catalyst of parsed.catalysts) {
        const affectedAssets = Array.isArray(catalyst.assets) && catalyst.assets.length ? catalyst.assets : (assets || []);
        for (const asset of affectedAssets) {
          const eventTimestamp = catalyst.event_timestamp ? Date.parse(catalyst.event_timestamp) : null;
          const firstPublicTimestamp = catalyst.first_public_timestamp ? Date.parse(catalyst.first_public_timestamp) : null;
          const payload = {
            coin: asset, category: catalyst.category, marketClassification: parsed.market_classification,
            sourceUrl: catalyst.sources?.[0]?.url,
            eventTimestamp: Number.isFinite(eventTimestamp) ? eventTimestamp : null,
            firstPublicTimestamp: Number.isFinite(firstPublicTimestamp) ? firstPublicTimestamp : null,
            discoveryTimestamp: submittedTs,
          };
          const payloadCheck = validateCatalystPayload(payload);
          if (!payloadCheck.valid) continue;

          const dedupeCandidate = { coin: asset, category: catalyst.category, ts: payload.eventTimestamp ?? submittedTs };
          if (isDuplicateCatalyst(dedupeCandidate, existing)) continue;

          const { timestampSource, timestampConfidence } = deriveTimestampProvenance(payload.firstPublicTimestamp, catalyst.first_public_timestamp_confidence);

          await recordCatalyst(env, {
            coin: asset, ts: payload.eventTimestamp ?? submittedTs, category: catalyst.category,
            direction: catalyst.direction ?? null, marketClassification: parsed.market_classification ?? null,
            sourceUrl: payload.sourceUrl ?? null, discoveryTimestamp: submittedTs,
            firstPublicTimestamp: payload.firstPublicTimestamp, confidence: catalyst.confidence ?? null,
            investigationId: relayId,
            sourceGrounded: false,
            timestampSource, timestampConfidence,
          });
          catalystsWritten++;
        }
      }
    }
  } catch (err) {
    validationStatus = err instanceof SyntaxError ? 'malformed_response' : 'error';
    errorMessage = String(err?.message || err).slice(0, 1000);
  }

  await env.DB.prepare(
    `INSERT INTO analyst_relay_log
     (relay_id, candidate_id, assets_json, prompt_requested_ts, submitted_ts, raw_response_text, parsed_json, validation_status, error_message, catalysts_written, source, context_json, context_hash)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    relayId, candidateId ?? null, JSON.stringify(assets || []), promptRequestedTs ?? null, submittedTs,
    String(rawResponseText || '').slice(0, 20000), parsed ? JSON.stringify(parsed) : null,
    validationStatus, errorMessage, catalystsWritten, 'human_relay',
    context ? JSON.stringify(context) : null, context?.contextHash ?? null
  ).run();

  return { ok: true, relayId, validationStatus, errorMessage, catalystsWritten, contextHash: context?.contextHash ?? null };
}


// ---- D1 orchestration: gathers rows, never mutates anything ----
async function fetchResolvedRows(env, table, { coin, horizonHours, sinceResolvedTs, probColumn = 'p_up', calibratedColumn = 'calibrated_p_up' } = {}) {
  const conditions = ['realized_up IS NOT NULL'];
  const params = [];
  if (coin) { conditions.push('coin = ?'); params.push(coin); }
  if (horizonHours) { conditions.push('horizon_hours = ?'); params.push(horizonHours); }
  if (sinceResolvedTs) { conditions.push('resolved_ts >= ?'); params.push(sinceResolvedTs); }
  // challenger_predictions has no volatility_percentile column (only
  // predictions/link_predictions/eth_predictions do) -- selecting it
  // unconditionally threw a real SQLITE_ERROR ("no such column") on every
  // call for that table, which buildDailyReport's Challenger-vs-Production
  // comparison makes unconditionally for BTC and LINK. That's why
  // /api/learning/daily (and /api/learning/chatgpt, same underlying call)
  // 500'd on every single request -- not a connectivity issue, a genuine
  // column mismatch. NULL AS keeps the shape identical for every caller
  // (mostConfidentMistakes/groupByRegime read volatility_percentile off
  // the mapped row either way) rather than branching the row-mapping logic
  // per table.
  const volatilityExpr = table === 'challenger_predictions' ? 'NULL' : 'volatility_percentile';
  const sql = `SELECT ts, resolved_ts, horizon_hours, realized_up, ${volatilityExpr} as volatility_percentile, trend_strength, is_regime_anomaly,
                      ${probColumn} as raw_p, ${calibratedColumn} as calibrated_p
               FROM ${table} WHERE ${conditions.join(' AND ')} ORDER BY ts ASC`;
  const { results } = await env.DB.prepare(sql).bind(...params).all();
  return results.map(r => ({
    ts: r.ts, resolved_ts: r.resolved_ts, horizon_hours: r.horizon_hours, realized_up: r.realized_up,
    volatility_percentile: r.volatility_percentile, trend_strength: r.trend_strength, is_regime_anomaly: r.is_regime_anomaly,
    // Prefer the calibrated probability when present -- same fallback
    // semantics runPrediction itself uses (calibrated_p_up falls back to
    // raw pUp when no curve exists yet).
    p: r.calibrated_p != null ? r.calibrated_p : r.raw_p,
  }));
}

const LEARNING_ASSETS = [
  { key: 'BTC', table: 'predictions', coinFilter: false, probColumn: 'p_up', calibratedColumn: 'calibrated_p_up' },
  { key: 'LINK', table: 'link_predictions', coinFilter: false, probColumn: 'p_up', calibratedColumn: 'calibrated_p_up' },
  { key: 'ETH', table: 'eth_predictions', coinFilter: false, probColumn: 'p_up', calibratedColumn: 'calibrated_p_up' },
];

// =====================================================================
// ---- Daily report caching ----
// buildDailyReport (below) scans every resolved row across predictions/
// link_predictions/eth_predictions/challenger_predictions -- ~3,000+ rows
// and growing every cron cycle -- to compute calibration curves, regime
// grouping, drift, confidence buckets. GET /api/learning/daily and
// /api/learning/chatgpt were recomputing this FRESH on every single page
// view, with no caching at all, unconditionally, even though the
// underlying data only actually changes once per 3-hourly cron cycle.
//
// Only caches the all-time report (dateStr == null) -- the case the
// frontend actually calls on every page load. A specific ?date= filter is
// a rarer, smaller-scoped historical-browsing path and still computes
// live; not worth the complexity of a per-date cache for something this
// infrequent.
//
// Single-row table (id=1, enforced by a CHECK constraint) -- there's only
// ever one "current" cached report, upserted via INSERT OR REPLACE.
// =====================================================================
async function getCachedDailyReport(env) {
  const row = await env.DB.prepare('SELECT report_json, generated_ts FROM daily_report_cache WHERE id = 1').first();
  if (!row) return null;
  try {
    return { report: JSON.parse(row.report_json), generatedTs: row.generated_ts };
  } catch {
    return null; // corrupt cache row (should never happen) -- treated as a miss, not a crash
  }
}

async function refreshDailyReportCache(env) {
  const report = await buildDailyReport(env, {});
  await env.DB.prepare(
    'INSERT INTO daily_report_cache (id, report_json, generated_ts) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET report_json = excluded.report_json, generated_ts = excluded.generated_ts'
  ).bind(JSON.stringify(report), Date.now()).run();
  return report;
}

async function buildDailyReport(env, { dateStr } = {}) {
  const nowTs = Date.now();
  const DAY = 24 * 3600 * 1000;
  let sinceResolvedTs = null, untilResolvedTs = null;
  if (dateStr) {
    const dayStart = Date.parse(`${dateStr}T00:00:00Z`);
    if (Number.isNaN(dayStart)) return { ok: false, error: 'invalid date, expected YYYY-MM-DD' };
    sinceResolvedTs = dayStart;
    untilResolvedTs = dayStart + DAY;
  }

  const report = {
    ok: true,
    generated_at: nowTs,
    date: dateStr || 'all_time',
    dataset_health: {},
    overall_performance: {},
    confidence_analysis: {},
    model_comparison: {},
    regime_analysis: {},
    error_analysis: {},
    market_catalysts: { status: 'no_catalysts_logged_for_period', catalysts: [] },
    model_drift: {},
    candidate_experiments: [],
    status: 'GREEN',
  };

  const perAsset = {};
  for (const asset of LEARNING_ASSETS) {
    let rows = await fetchResolvedRows(env, asset.table, { horizonHours: null, probColumn: asset.probColumn, calibratedColumn: asset.calibratedColumn });
    if (untilResolvedTs) rows = rows.filter(r => r.resolved_ts >= sinceResolvedTs && r.resolved_ts < untilResolvedTs);
    perAsset[asset.key] = rows;
  }

  // ---- Dataset health ----
  for (const asset of LEARNING_ASSETS) {
    const { results } = await env.DB.prepare(`SELECT COUNT(*) as n, SUM(realized_up IS NULL) as unresolved FROM ${asset.table}`).all();
    report.dataset_health[asset.key] = { total_rows: results[0].n, unresolved: results[0].unresolved, resolved_in_period: perAsset[asset.key].length };
  }

  // ---- Overall performance + confidence + regime + error + drift, per asset+horizon ----
  for (const asset of LEARNING_ASSETS) {
    const rows = perAsset[asset.key];
    report.overall_performance[asset.key] = summarizeRows(rows);
    report.confidence_analysis[asset.key] = rows.length >= LEARNING_MIN_SAMPLE
      ? { buckets: bucketByConfidence(rows), most_confident_mistakes: mostConfidentMistakes(rows) }
      : { status: 'insufficient_data', n: rows.length };
    report.regime_analysis[asset.key] = groupByRegime(rows);
    report.error_analysis[asset.key] = mostConfidentMistakes(rows, 10);
    report.model_drift[asset.key] = computeDrift(rows, nowTs);

    for (const h of [12, 24]) {
      const hRows = rows.filter(r => r.horizon_hours === h);
      report.overall_performance[`${asset.key}_${h}h`] = summarizeRows(hRows);
    }
  }

  // ---- Challenger vs Production comparison (all three coins now that ETH has a challenger too) ----
  for (const coin of ['BTC', 'LINK', 'ETH']) {
    const coreRows = perAsset[coin];
    const challengerRows = await fetchResolvedRows(env, 'challenger_predictions', { coin, probColumn: 'p_up_tilted', calibratedColumn: 'calibrated_p_up_flat' });
    const filteredChallenger = untilResolvedTs ? challengerRows.filter(r => r.resolved_ts >= sinceResolvedTs && r.resolved_ts < untilResolvedTs) : challengerRows;
    report.model_comparison[coin] = {
      production: summarizeRows(coreRows),
      challenger: summarizeRows(filteredChallenger),
    };
  }

  // ---- Market catalysts (real query, see fetchCatalystsForPeriod) ----
  const catalystRows = await fetchCatalystsForPeriod(env, sinceResolvedTs, untilResolvedTs);
  report.market_catalysts = catalystRows.length
    ? { status: 'ok', catalysts: catalystRows }
    : { status: 'no_catalysts_logged_for_period', catalysts: [] };

  // ---- Status rollup ----
  const anyInsufficient = Object.values(report.overall_performance).every(v => v.status === 'insufficient_data');
  const anyDriftFlag = LEARNING_ASSETS.some(a => report.model_drift[a.key]?.flag);
  if (anyInsufficient) report.status = 'YELLOW';
  if (anyDriftFlag) report.status = report.status === 'GREEN' ? 'YELLOW' : report.status;

  return report;
}

// Compact, AI-analysis-optimized projection of the same report -- smaller
// payload, no secrets, no raw DB access, read-only (per .ai/ARCHITECTURE.md
// Security section).
function compactForChatGpt(report) {
  return {
    generated_at: report.generated_at,
    date: report.date,
    status: report.status,
    dataset_health: report.dataset_health,
    key_metrics: report.overall_performance,
    model_comparison: report.model_comparison,
    regime_changes: Object.fromEntries(LEARNING_ASSETS.map(a => [a.key, report.model_drift[a.key]?.flag || null])),
    important_errors: Object.fromEntries(LEARNING_ASSETS.map(a => [a.key, report.error_analysis[a.key]])),
    market_catalysts: report.market_catalysts,
    candidate_experiments: report.candidate_experiments,
  };
}

//
// Deliberately separate from the original PulseWorker (sentiment-ff75) so
// prediction-model experimentation here can never destabilize the working
// V1 sentiment/alert pipeline. Shares the same D1 database (sentiment-history)
// by design — see wrangler.toml — so the model has real history to learn
// from immediately instead of starting from zero.
//
// Architecture principle carried over from V1: this Worker computes
// deterministically and/or calls Gemini for narration. It never asks an LLM
// to invent a price or a probability — those come from the model itself.

// Shared by the /predict HTTP route and the cron handler below, so both
// paths run identical logic rather than the schedule quietly drifting from
// what a manual visit does.
// ---- Challenger model: regime-conditional trend/reversion ----
// Deliberately NOT a k-NN variant, and deliberately NOT wired into the
// original model in any way — this is a genuinely different approach,
// logged in parallel for comparison, same "log but don't replace"
// discipline already proven with adaptive-K. Motivated by a specific,
// confirmed finding: the 35 resolved is_regime_anomaly=1 predictions
// scored 22.9% accuracy (worse than the original model's own opposite-of-
// coinflip miss rate) against a naive "trend continues" baseline that
// would have scored 97.1% in that same window. The tripwire correctly
// flags novel conditions; the k-NN's own read during those conditions has
// now been empirically bad twice (once in the offline fear/greed regime
// split, once here) — this tests whether a simple, transparent,
// deterministic trend-persistence read does better specifically in the
// conditions the tripwire flags, while deferring to the existing model
// when today's setup DOES resemble history well.
//
// Isolation is deliberate: this function does NOT import or recompute any
// part of runPrediction's k-NN distance matrix — it reads the SAME
// is_regime_anomaly value runPrediction already computed for today in
// this same cycle (passed in via coreResult), rather than an independent
// approximation that could quietly drift from the original definition.
// Everything else here — trailing return, the Foufi driver tilt — is
// fully separate logic that cannot affect the original model even if it
// has a bug.
async function runChallengerPrediction(env, { coin, horizonHours, priceTable, priceCol, priceNow, coreResult }, { persist = true } = {}) {
  if (!coreResult || coreResult.status !== 'ok') {
    return { ok: true, status: 'skipped_core_not_ok' };
  }
  const lagMs = horizonHours * 60 * 60 * 1000;
  const nowTs = coreResult.ts;
  const isAnomalous = !!coreResult.regime_anomaly;

  // Trailing return over the SAME window as the horizon being predicted
  // (24h trailing to inform a 24h-forward guess, 12h to inform 12h) —
  // symmetric and easy to audit, not tuned for best fit.
  const { results: priceRows } = await env.DB.prepare(
    `SELECT ts, ${priceCol} as price FROM ${priceTable} WHERE ts <= ? ORDER BY ts DESC LIMIT 200`
  ).bind(nowTs).all();
  const trailingTarget = nowTs - lagMs;
  const trailingRow = priceRows.find(r => r.ts <= trailingTarget) || priceRows[priceRows.length - 1];
  const trailingReturnPct = trailingRow && trailingRow.price
    ? (priceNow - trailingRow.price) / trailingRow.price * 100
    : null;

  // Flat variant: pure regime gate. Anomalous -> shrink the core k-NN's
  // own p_up toward 0.5 (same direction, less confidence) instead of
  // betting on trailing-return continuation. Calibration check (2026-08)
  // showed the old trend-persistence bet was backwards: of 29 resolved
  // BTC anomaly cases, 18 had a negative trailing return, and all 18
  // resolved up -- dips inside an anomaly got bought back within the
  // horizon essentially every time in this dataset. Flipping the sign
  // would just overfit to that same bull-market pattern. "Anomalous"
  // only means no good historical analog exists; the honest response to
  // that is less confidence, not a punchier directional call either way.
  // Not anomalous -> defer to the original model's own p_up, on the
  // theory that when today's setup DOES resemble history well, the
  // k-NN's own logic is the more trustworthy read.
  const ANOMALY_SHRINK = 0.5; // halve the distance from 0.5 when no good analog exists
  let pUpFlat;
  if (isAnomalous) {
    pUpFlat = 0.5 + (coreResult.p_up - 0.5) * ANOMALY_SHRINK;
  } else {
    pUpFlat = coreResult.p_up;
  }
  pUpFlat = Math.max(0.05, Math.min(0.95, pUpFlat));

  // Trend-strength guardrail: if a strong trend disagrees with pUpFlat's
  // own lean (in either direction), dampen further. Reuses the core
  // model's trend_strength (computed once in runPrediction/runLinkPrediction)
  // rather than recomputing here.
  pUpFlat = applyTrendGuardrail(pUpFlat, coreResult.trend_strength);
  pUpFlat = Math.max(0.05, Math.min(0.95, pUpFlat));

  // Genuinely adaptive layer: decile-bucket recalibration built from
  // CHALLENGER'S OWN resolved track record (not the core model's), same
  // technique already proven safe for the core model (buildCalibrationCurve/
  // applyCalibratedProbability, unchanged, reused as-is). Additive -- never
  // replaces pUpFlat, which stays exactly what it's always been. Tracked as
  // its own separate, scored variant (see getChallengerCalibrationHistory)
  // so whether this actually helps gets decided by real accumulated
  // evidence, not assumed on the way in.
  const challengerCurveRows = await getLatestChallengerCalibrationCurve(env, coin, horizonHours);
  const calibratedPUpFlat = applyCalibratedProbability(pUpFlat, challengerCurveRows);

  // Tilted variant: identical to flat UNLESS today is anomalous AND a
  // fresh (<=30h, same convention as V1's srcFoufi) Foufi digest names a
  // driver this Worker has a mapped lean for (macro/tradfi only - micro
  // has no mapped lean, logged as uncovered rather than faked). When
  // available, Foufi's stated lean for that category either reinforces
  // (agrees with trailing-return direction -> push further from 0.5) or
  // dampens (disagrees -> pull toward 0.5) the flat read. Fixed +-0.10,
  // same "modest, not an override" conservatism as V1's own driver boost.
  let pUpTilted = pUpFlat, driverUsed = null, driverAgreement = null, foufiVideoId = null;
  if (isAnomalous && trailingReturnPct != null) {
    try {
      const digest = await env.DB.prepare(
        'SELECT video_id, published_ts, transcript_status, summary_json FROM foufi_digest ORDER BY published_ts DESC LIMIT 1'
      ).first();
      if (digest && digest.transcript_status === 'ok' && digest.published_ts && (nowTs - digest.published_ts) / 3600000 <= 30) {
        const summary = JSON.parse(digest.summary_json || '{}');
        const drivers = [summary.dominant_driver, summary.secondary_driver].filter(d => d && d !== 'none');
        const driverLeanMap = { macro: summary.macro?.lean, tradfi: summary.tradfi?.lean };
        const mappedDriver = drivers.find(d => driverLeanMap[d]);
        if (mappedDriver) {
          const lean = driverLeanMap[mappedDriver];
          const trailingDir = trailingReturnPct > 0 ? 'bullish' : trailingReturnPct < 0 ? 'bearish' : 'neutral';
          driverUsed = mappedDriver;
          foufiVideoId = digest.video_id;
          if (lean === trailingDir) {
            driverAgreement = 'agree';
            pUpTilted = pUpFlat + (pUpFlat >= 0.5 ? 0.10 : -0.10);
          } else if (lean !== 'neutral' && trailingDir !== 'neutral') {
            driverAgreement = 'disagree';
            pUpTilted = 0.5 + (pUpFlat - 0.5) * 0.5; // dampen toward neutral, don't flip
          } else {
            driverAgreement = 'na';
          }
        } else if (drivers.includes('micro')) {
          driverUsed = 'micro'; driverAgreement = 'uncovered'; // no mapped lean - logged, not faked
        }
      }
    } catch (e) {
      // Foufi lookup failing must never break the challenger prediction itself
      driverAgreement = 'lookup_error';
    }
  }
  pUpTilted = Math.max(0.05, Math.min(0.95, pUpTilted));

  let insert = null;
  if (persist) {
    insert = await env.DB.prepare(
      `INSERT INTO challenger_predictions
       (coin, ts, target_ts, horizon_hours, price_at_prediction, is_regime_anomaly, trailing_return_pct,
        p_up_flat, p_up_tilted, driver_used, driver_agreement, foufi_digest_video_id, trend_strength, calibrated_p_up_flat,
        model_version, git_commit_sha)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(
      coin, nowTs, nowTs + lagMs, horizonHours, priceNow, isAnomalous ? 1 : 0, trailingReturnPct,
      pUpFlat, pUpTilted, driverUsed, driverAgreement, foufiVideoId, coreResult.trend_strength ?? null,
      Number(calibratedPUpFlat.toFixed(3)), MODEL_VERSIONS.challenger, currentGitSha(env)
    ).run();
  }

  return {
    ok: true, status: 'ok', id: insert ? insert.meta.last_row_id : null, persisted: persist, coin, horizon_hours: horizonHours,
    is_regime_anomaly: isAnomalous, trailing_return_pct: trailingReturnPct != null ? Number(trailingReturnPct.toFixed(2)) : null,
    p_up_flat: Number(pUpFlat.toFixed(3)), p_up_tilted: Number(pUpTilted.toFixed(3)),
    calibrated_p_up_flat: Number(calibratedPUpFlat.toFixed(3)),
    driver_used: driverUsed, driver_agreement: driverAgreement, trend_strength: coreResult.trend_strength ?? null,
  };
}

// Resolves any challenger prediction whose horizon has passed. Fully
// separate from backfillPredictions (the original model's resolver) -
// same isolation principle as the prediction function itself.
async function backfillChallengerPredictions(env) {
  const nowTs = Date.now();
  const { results: pending } = await env.DB.prepare(
    'SELECT * FROM challenger_predictions WHERE resolved_ts IS NULL AND target_ts <= ?'
  ).bind(nowTs).all();
  let resolvedCount = 0;
  for (const p of pending) {
    const priceTable = p.coin === 'LINK' ? 'link_data' : p.coin === 'ETH' ? 'eth_data' : 'btc_data';
    const priceCol = p.coin === 'LINK' ? 'link_price' : p.coin === 'ETH' ? 'eth_price' : 'btc_price';
    const tolMs = p.horizon_hours * 60 * 60 * 1000 * 0.2;
    const { results: rows } = await env.DB.prepare(
      `SELECT ts, ${priceCol} as price FROM ${priceTable} WHERE ts BETWEEN ? AND ? ORDER BY ABS(ts - ?) ASC LIMIT 1`
    ).bind(p.target_ts - tolMs, p.target_ts + tolMs, p.target_ts).all();
    if (!rows.length) continue;
    const realizedPrice = rows[0].price;
    const realizedReturn = (realizedPrice - p.price_at_prediction) / p.price_at_prediction * 100;
    await env.DB.prepare(
      'UPDATE challenger_predictions SET realized_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'
    ).bind(realizedPrice, realizedReturn, realizedReturn > 0 ? 1 : 0, nowTs, p.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

// =====================================================================
// ---- Variant-aware calibration summary ----
// Answers one specific question for whichever variant selectBestVariant
// currently has chosen: "is this number trustworthy, and how much so" --
// in ONE consistent shape regardless of which of the 6 possible variants
// it is. Deliberately NOT reusing getCalibration's or
// getChallengerCalibration's full output shapes below -- each has fields
// specific to itself (driver_usage, ma_crossover_baseline, the
// experimental sub-object) that don't generalize across all 6 variants.
// This exists specifically to back the Dashboard tile, which needs to
// show a trust indicator for whatever's currently headlined, not the
// full diagnostic detail those richer functions provide elsewhere (Lab).
// =====================================================================
async function getVariantCalibrationSummary(env, coin, horizonHours, variant) {
  const coreTable = coreTableForCoin(coin);
  let rows;

  if (variant === 'original' || variant === 'calibrated' || variant === 'experimental') {
    const probField = variant === 'original' ? 'p_up' : variant === 'calibrated' ? 'calibrated_p_up' : 'p_up_experimental';
    const { results } = await env.DB.prepare(
      `SELECT ${probField} as p, realized_up FROM ${coreTable} WHERE horizon_hours=? AND realized_up IS NOT NULL AND ${probField} IS NOT NULL`
    ).bind(horizonHours).all();
    rows = results;
  } else if (variant === 'challenger_flat' || variant === 'challenger_tilted' || variant === 'challenger_calibrated') {
    const probField = variant === 'challenger_flat' ? 'p_up_flat' : variant === 'challenger_tilted' ? 'p_up_tilted' : 'calibrated_p_up_flat';
    const { results } = await env.DB.prepare(
      `SELECT ${probField} as p, realized_up FROM challenger_predictions WHERE coin=? AND horizon_hours=? AND realized_up IS NOT NULL AND ${probField} IS NOT NULL`
    ).bind(coin, horizonHours).all();
    rows = results;
  } else {
    return { ok: false, error: `unknown variant: ${variant}` };
  }

  const n = rows.length;
  if (n === 0) return { ok: true, variant, n_resolved: 0, note: 'No resolved predictions for this variant yet.' };

  const accuracy = rows.filter(r => (r.p >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = rows.reduce((s, r) => s + (r.p - r.realized_up) ** 2, 0) / n;
  const upRate = rows.filter(r => r.realized_up === 1).length / n;
  const bestNaiveBrier = Math.min(0.25, upRate * (1 - upRate));
  const beatsNaiveBaseline = brier < bestNaiveBrier;

  return {
    ok: true, variant, n_resolved: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier: Number(brier.toFixed(3)),
    naive_baseline_brier: Number(bestNaiveBrier.toFixed(3)),
    beats_naive_baseline: beatsNaiveBaseline,
    note: beatsNaiveBaseline
      ? 'Currently beats the best naive baseline.'
      : 'Does NOT beat the best naive baseline right now — a constant guess would have done as well or better.',
  };
}

// =====================================================================
// ---- Regime-directional research report ----
// READ-ONLY instrumentation. No writes, no effect on selectBestVariant,
// runPrediction, or any production behavior whatsoever -- a research tool,
// not a model input. Exists to let the regime-anomaly-directional-split
// audit (is_regime_anomaly conflating bullish/bearish conditions) be
// re-checked periodically as more independent episodes accumulate,
// without re-deriving the analysis by hand each time. The original
// ad-hoc version of this exact analysis found the hypothesis
// INCONCLUSIVE on ~3 real episodes over 24 days -- recommendation was to
// accumulate more data (weeks, not days) before any OOS test, let alone
// a production change. This function is that accumulation mechanism.
// =====================================================================

// Pure. Same bucket definitions used throughout the audit: normal (not
// anomalous), or anomalous split by trend_strength's sign at the moment
// the anomaly was flagged. trend_strength can be null for older rows
// (the column was added partway through this system's history) --
// those get their own bucket rather than being silently dropped or
// miscategorized.
function classifyRegimeBucket(isRegimeAnomaly, trendStrength) {
  if (isRegimeAnomaly !== 1) return 'normal';
  if (trendStrength == null) return 'no_trend_data';
  if (trendStrength > 0) return 'anomaly_pos_trend';
  if (trendStrength < 0) return 'anomaly_neg_trend';
  return 'anomaly_zero_trend';
}

async function computeRegimeDirectionalReport(env, coin, horizonHours) {
  const table = coreTableForCoin(coin);
  const { results: rows } = await env.DB.prepare(
    `SELECT ts, p_up, realized_up, is_regime_anomaly, trend_strength FROM ${table} WHERE resolved_ts IS NOT NULL AND horizon_hours=? ORDER BY ts ASC`
  ).bind(horizonHours).all();

  // ---- Prediction-level bucket table -- same shape as the ad-hoc audit
  // SQL, so results are directly comparable to that report. ----
  const buckets = {};
  for (const r of rows) {
    const b = classifyRegimeBucket(r.is_regime_anomaly, r.trend_strength);
    if (!buckets[b]) buckets[b] = { n: 0, correct: 0, up: 0 };
    buckets[b].n++;
    if ((r.p_up >= 0.5) === (r.realized_up === 1)) buckets[b].correct++;
    if (r.realized_up === 1) buckets[b].up++;
  }
  const bucketSummary = {};
  for (const [name, s] of Object.entries(buckets)) {
    bucketSummary[name] = {
      n: s.n,
      original_accuracy: Number((100 * s.correct / s.n).toFixed(1)),
      pct_actually_up: Number((100 * s.up / s.n).toFixed(1)),
      always_up_baseline_accuracy: Number((100 * s.up / s.n).toFixed(1)),
      always_down_baseline_accuracy: Number((100 * (s.n - s.up) / s.n).toFixed(1)),
    };
  }

  // ---- Challenger comparison -- BTC/LINK only. Challenger does not run
  // for ETH (ethPredictAndLog never calls runChallengerPrediction, same
  // fact already confirmed and handled in /challenger-calibration). ----
  let challengerBucketSummary = null;
  if (coin === 'BTC' || coin === 'LINK') {
    const { results: chRows } = await env.DB.prepare(
      `SELECT p_up_flat, p_up_tilted, realized_up, is_regime_anomaly, trend_strength FROM challenger_predictions WHERE coin=? AND horizon_hours=? AND realized_up IS NOT NULL`
    ).bind(coin, horizonHours).all();
    const chBuckets = {};
    for (const r of chRows) {
      const b = classifyRegimeBucket(r.is_regime_anomaly, r.trend_strength);
      if (!chBuckets[b]) chBuckets[b] = { n: 0, correctFlat: 0, correctTilted: 0 };
      chBuckets[b].n++;
      if ((r.p_up_flat >= 0.5) === (r.realized_up === 1)) chBuckets[b].correctFlat++;
      if ((r.p_up_tilted >= 0.5) === (r.realized_up === 1)) chBuckets[b].correctTilted++;
    }
    challengerBucketSummary = {};
    for (const [name, s] of Object.entries(chBuckets)) {
      challengerBucketSummary[name] = {
        n: s.n,
        challenger_flat_accuracy: Number((100 * s.correctFlat / s.n).toFixed(1)),
        challenger_tilted_accuracy: Number((100 * s.correctTilted / s.n).toFixed(1)),
      };
    }
  }

  // ---- Episode detection: day-level majority-vote bucket, then group
  // consecutive days sharing the same bucket into one episode. The whole
  // point -- 345 anomaly-flagged predictions in the original audit were
  // ~3 real episodes, not 345 independent observations, and treating
  // them as independent would manufacture false statistical confidence. ----
  const dayBuckets = {};
  for (const r of rows) {
    const date = new Date(r.ts).toISOString().slice(0, 10);
    const b = classifyRegimeBucket(r.is_regime_anomaly, r.trend_strength);
    if (!dayBuckets[date]) dayBuckets[date] = {};
    dayBuckets[date][b] = (dayBuckets[date][b] || 0) + 1;
  }
  const sortedDates = Object.keys(dayBuckets).sort();
  const dayBucketSeq = sortedDates.map(date => {
    const counts = dayBuckets[date];
    const dominant = Object.entries(counts).sort((a, b) => b[1] - a[1])[0][0];
    return { date, bucket: dominant };
  });
  const episodes = [];
  for (const { date, bucket } of dayBucketSeq) {
    const last = episodes[episodes.length - 1];
    if (last && last.bucket === bucket) {
      last.end_date = date;
      last.n_days++;
    } else {
      episodes.push({ bucket, start_date: date, end_date: date, n_days: 1 });
    }
  }
  const episodeCountByBucket = {};
  for (const ep of episodes) {
    episodeCountByBucket[ep.bucket] = (episodeCountByBucket[ep.bucket] || 0) + 1;
  }

  return {
    ok: true, coin, horizon_hours: horizonHours, generated_at: Date.now(),
    raw_prediction_count: rows.length,
    bucket_summary: bucketSummary,
    challenger_bucket_summary: challengerBucketSummary,
    episode_count_by_bucket: episodeCountByBucket,
    episodes,
    note: 'Read-only research report -- does not feed into any prediction, selection, or calibration logic. See the regime-anomaly-directional-split audit for full methodology. Episode counts, not raw prediction counts, are what matter for statistical significance here.',
  };
}

// Calibration for the challenger: both variants (flat/tilted) against BOTH
// naive-baseline (low bar — always guess the historically-more-common
// direction) AND a real MA-crossover momentum strategy (higher bar — price
// vs. its own trailing 20-point average at prediction time). Beating naive
// baseline alone isn't a meaningful claim; beating a real momentum strategy
// is closer to one.
async function getChallengerCalibration(env, coin, horizonHours) {
  const { results: rows } = await env.DB.prepare(
    'SELECT * FROM challenger_predictions WHERE coin=? AND horizon_hours=? AND resolved_ts IS NOT NULL ORDER BY ts ASC'
  ).bind(coin, horizonHours).all();
  const n = rows.length;
  if (n < 5) return { ok: true, coin, horizon_hours: horizonHours, n_resolved: n, note: 'Not enough resolved challenger predictions yet — check back once more have accumulated.' };

  const priceTable = coin === 'LINK' ? 'link_data' : coin === 'ETH' ? 'eth_data' : 'btc_data';
  const priceCol = coin === 'LINK' ? 'link_price' : coin === 'ETH' ? 'eth_price' : 'btc_price';
  const { results: allPrices } = await env.DB.prepare(
    `SELECT ts, ${priceCol} as price FROM ${priceTable} ORDER BY ts ASC`
  ).all();

  function maCrossoverPrediction(predTs, priceAtPred) {
    const priorRows = allPrices.filter(r => r.ts <= predTs).slice(-20);
    if (priorRows.length < 10 || !priceAtPred) return null;
    const ma = priorRows.reduce((s, r) => s + r.price, 0) / priorRows.length;
    return priceAtPred > ma ? 1 : 0;
  }

  let accFlat = 0, accTilted = 0, accMa = 0, upCount = 0;
  let brierFlat = 0, brierTilted = 0;
  let maCount = 0;
  for (const r of rows) {
    const actual = r.realized_up;
    if (actual === 1) upCount++;
    if ((r.p_up_flat > 0.5) === (actual === 1)) accFlat++;
    if ((r.p_up_tilted > 0.5) === (actual === 1)) accTilted++;
    brierFlat += (r.p_up_flat - actual) ** 2;
    brierTilted += (r.p_up_tilted - actual) ** 2;
    const maPred = maCrossoverPrediction(r.ts, r.price_at_prediction);
    if (maPred != null) { maCount++; if (maPred === actual) accMa++; }
  }
  const upRate = upCount / n;
  const naiveBest = Math.max(upRate, 1 - upRate);
  const maAcc = maCount >= 5 ? accMa / maCount : null;

  return {
    ok: true, coin, horizon_hours: horizonHours, n_resolved: n,
    historical_up_rate: Number(upRate.toFixed(3)),
    accuracy_flat: Number((accFlat / n).toFixed(3)),
    accuracy_tilted: Number((accTilted / n).toFixed(3)),
    brier_flat: Number((brierFlat / n).toFixed(3)),
    brier_tilted: Number((brierTilted / n).toFixed(3)),
    naive_baseline_accuracy: Number(naiveBest.toFixed(3)),
    brier_baseline_5050: 0.25,
    brier_baseline_up_rate: Number((upRate * (1 - upRate)).toFixed(3)),
    ma_crossover_baseline: maAcc != null ? { n: maCount, accuracy: Number(maAcc.toFixed(3)) } : { n: maCount, note: 'not enough trailing price history yet' },
    beats_naive_flat: (accFlat / n) > naiveBest,
    beats_naive_tilted: (accTilted / n) > naiveBest,
    beats_ma_crossover_flat: maAcc != null ? (accFlat / n) > maAcc : null,
    beats_ma_crossover_tilted: maAcc != null ? (accTilted / n) > maAcc : null,
    driver_usage: {
      agree: rows.filter(r => r.driver_agreement === 'agree').length,
      disagree: rows.filter(r => r.driver_agreement === 'disagree').length,
      uncovered: rows.filter(r => r.driver_agreement === 'uncovered').length,
      no_driver_applied: rows.filter(r => !r.driver_agreement).length,
    },
    note: 'beats_naive alone is a low bar (matches this app\'s own established convention elsewhere) — beats_ma_crossover is the more meaningful claim.',
  };
}

// Expanding-window accuracy + Brier over time for Challenger's flat/tilted
// variants — same "not a single cherry-pickable snapshot" principle as the
// core model's getCalibrationHistory, applied here for the Lab tab's
// precision-over-time graph. Unlike the core model (split across
// predictions/link_predictions tables), challenger_predictions already
// has a coin column, so one function covers both coins.
async function getChallengerCalibrationHistory(env, coin, horizonHours) {
  const { results } = await env.DB.prepare(
    'SELECT resolved_ts, p_up_flat, p_up_tilted, calibrated_p_up_flat, realized_up FROM challenger_predictions WHERE coin=? AND horizon_hours=? AND resolved_ts IS NOT NULL ORDER BY resolved_ts ASC'
  ).bind(coin, horizonHours).all();

  let correctFlat = 0, correctTilted = 0, correctCal = 0, n = 0, nCal = 0;
  let sumBrierFlat = 0, sumBrierTilted = 0, sumBrierCal = 0;
  const points = results.map(r => {
    n++;
    if ((r.p_up_flat > 0.5) === (r.realized_up === 1)) correctFlat++;
    if ((r.p_up_tilted > 0.5) === (r.realized_up === 1)) correctTilted++;
    sumBrierFlat += (r.p_up_flat - r.realized_up) ** 2;
    sumBrierTilted += (r.p_up_tilted - r.realized_up) ** 2;
    const point = {
      ts: r.resolved_ts,
      n,
      accuracy_flat: Number((correctFlat / n).toFixed(3)),
      accuracy_tilted: Number((correctTilted / n).toFixed(3)),
      brier_flat: Number((sumBrierFlat / n).toFixed(4)),
      brier_tilted: Number((sumBrierTilted / n).toFixed(4)),
    };
    // Calibrated-flat scored here for the first time — genuinely new
    // variant, not previously computed at all for Challenger. n_calibrated
    // lags n (the curve needs 20+ resolved rows before it exists, see
    // refreshChallengerCalibrationCurve) — early points will correctly show
    // null rather than a misleading number.
    if (r.calibrated_p_up_flat != null) {
      nCal++;
      if ((r.calibrated_p_up_flat > 0.5) === (r.realized_up === 1)) correctCal++;
      sumBrierCal += (r.calibrated_p_up_flat - r.realized_up) ** 2;
      point.accuracy_calibrated_flat = Number((correctCal / nCal).toFixed(3));
      point.brier_calibrated_flat = Number((sumBrierCal / nCal).toFixed(4));
      point.n_calibrated = nCal;
    } else {
      point.accuracy_calibrated_flat = null;
      point.brier_calibrated_flat = null;
      point.n_calibrated = nCal;
    }
    return point;
  });
  return { ok: true, coin, horizon_hours: horizonHours, points };
}

async function getChallengerRecent(env, limit = 20) {
  const { results } = await env.DB.prepare(
    'SELECT * FROM challenger_predictions ORDER BY ts DESC LIMIT ?'
  ).bind(limit).all();
  return { ok: true, predictions: results };
}

async function predictAndLog(env, horizonHours = 24, { allowWrite = false } = {}) {
  // Resolving already-existing pending predictions is NOT "creating new
  // training data" -- it fills in the real outcome on a row that already
  // exists and is due, and doing this promptly regardless of trigger
  // source was the whole point of building self-sufficient logging in
  // the first place. Left unconditional on purpose.
  const resolvedCount = await backfillPredictions(env);
  const geminiResolvedCount = await backfillGeminiBiasShort(env);

  // allowWrite:true (cron) always writes, matching prior behavior exactly
  // -- the OR short-circuits before ever touching D1 for the staleness
  // check. allowWrite:false (live, the default) writes only if the cron
  // has genuinely stalled (see isRecentDataStale's own comment for why
  // 6h). This is the entire fix: a live page view no longer creates a new
  // btc_data/predictions row just because someone looked at the page.
  const shouldWrite = allowWrite || (await isRecentDataStale(env, 'btc_data') && await claimStaleRefresh(env, 'BTC', Date.now()));
  if (shouldWrite) await logBtcData(env);
  const result = await runPrediction(env, horizonHours, { persist: shouldWrite });
  result.backfilled_this_call = resolvedCount;
  result.gemini_bias_backfilled_this_call = geminiResolvedCount;
  try {
    const challengerResolvedCount = await backfillChallengerPredictions(env);
    const challengerResult = await runChallengerPrediction(env, {
      coin: 'BTC', horizonHours, priceTable: 'btc_data', priceCol: 'btc_price', priceNow: result.btc_price_now, coreResult: result,
    }, { persist: shouldWrite });
    result.challenger = challengerResult;
    result.challenger_backfilled_this_call = challengerResolvedCount;
  } catch (e) {
    // Challenger failing must never break the original model's own cron cycle.
    result.challenger = { ok: false, error: String(e) };
  }
  return result;
}

// ---- Chart data: BTC price series + the full predictions log in one call,
// so the frontend can filter to 1D/1W/1M/ALL client-side without refetching
// on every range-tab click. ----
async function getChartData(env, horizonHours = 24) {
  const { results: prices } = await env.DB.prepare(
    'SELECT ts, btc_price FROM btc_data ORDER BY ts ASC'
  ).all();
  const { results: predictions } = await env.DB.prepare(
    'SELECT id, ts, target_ts, btc_price_at_prediction, p_up, median_analog_return, realized_up, realized_return FROM predictions WHERE horizon_hours = ? ORDER BY ts ASC'
  ).bind(horizonHours).all();
  return { ok: true, prices, predictions };
}

// Mirrors getChartData exactly, scoped to ETH.
async function getEthChartData(env, horizonHours = 24) {
  const { results: prices } = await env.DB.prepare(
    'SELECT ts, eth_price FROM eth_data ORDER BY ts ASC'
  ).all();
  const { results: predictions } = await env.DB.prepare(
    'SELECT id, ts, target_ts, eth_price_at_prediction, p_up, median_analog_return, realized_up, realized_return FROM eth_predictions WHERE horizon_hours = ? ORDER BY ts ASC'
  ).bind(horizonHours).all();
  return { ok: true, prices, predictions };
}

// ---- Daily comprehensive Gemini analysis ----
// Runs once/day (see cron below), independent of anyone opening any page —
// closes the "history only updates when V1 is visited" gap for THIS
// specific signal, since it's generated entirely server-side.
//
// Same principle as V1's Outlook tab: Gemini writes the narrative and a few
// structured reads; it never invents the model's actual prediction number.
// The structured fields here are stored for now but deliberately NOT fed
// into the k-NN model yet — this data source starts at zero and needs its
// own maturation period before being trusted as a feature, same discipline
// as everything else built so far.
//
// Lessons carried over from V1's Outlook tab, applied from the start
// instead of hitting them the same way twice:
// - case-insensitive matching on bullish/neutral/bearish (V1 hit a real bug
//   where exact-case matching silently defaulted everything to neutral)
// - don't rely on Gemini's own line breaks for the narrative body — insert
//   them deterministically before known section labels (V1's prompt-only
//   approach failed twice in real testing)
// - extract the trailing JSON block by regex, tolerate it being missing or
//   malformed without losing the narrative
const ANALYSIS_SECTIONS = [
  'TRADITIONAL MARKETS', 'MACRO ECONOMY', 'GEOPOLITICS', 'CRYPTO MICRO-FACTORS',
  'TECHNICAL — SHORT TERM', 'TECHNICAL — MEDIUM TERM', 'TECHNICAL — LONG TERM',
  'CROSS-ASSET', 'SENTIMENT', 'ADOPTION', 'SYNTHESIS',
];

function normalizeBias(v) {
  if (typeof v !== 'string') return null;
  const s = v.trim().toLowerCase();
  if (s.startsWith('bull')) return 'bullish';
  if (s.startsWith('bear')) return 'bearish';
  if (s.startsWith('neutral')) return 'neutral';
  return null;
}
function normalizeRisk(v) {
  if (typeof v !== 'string') return null;
  const s = v.trim().toLowerCase();
  if (['low', 'medium', 'high'].includes(s)) return s;
  return null;
}

// trigger: 'cron' (the 07:00 UTC daily job) or 'manual' (the /run-analysis
// route -- covers both the explicit "Run Analysis" button and the
// frontend's per-app-boot call, per the root-cause audit). Each gets its
// own lane in GEMINI_SHARED_QUOTA_CONFIG so the daily cron always has a
// guaranteed slot manual/app-boot traffic can't exhaust -- see the shared
// quota gate's design comment above getGeminiInvestigationCounts.
//
// NEVER throws (changed from the previous version, which threw on any
// Gemini/network failure and relied on the route handler's try/catch or
// the cron's own .catch() to turn that into a 500 / a console.error).
// Every path -- quota deferred, provider error, or success -- returns an
// { ok, status, ... } object instead, so callers can give an honest
// response rather than a generic failure, per the root-cause audit's
// Phase 4 requirement.
async function runGeminiDailyAnalysis(env, trigger = 'cron') {
  const consumer = trigger === 'manual' ? 'btc_narrative_manual' : 'btc_narrative_cron';
  const requestTs = Date.now();
  const correlationId = `GA-${requestTs}-BTC-${trigger}`;
  const model = 'gemini-3.6-flash';

  const reservation = await reserveGeminiQuotaSlot(env, consumer, GEMINI_SHARED_QUOTA_CONFIG[consumer], requestTs);
  if (!reservation.admitted) {
    await recordGeminiProviderCall(env, {
      correlationId, consumer, asset: 'BTC', requestTs, model,
      quotaDecision: reservation.reason === 'daily_limit_reached' ? 'deferred_daily' : 'deferred_hourly',
      httpStatus: null, responseStatus: 'quota_deferred', errorCategory: null,
    }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));
    return { ok: false, status: 'quota_deferred', reason: reservation.reason, correlationId };
  }

  // Ground-truth context, same pattern as V1: give Gemini real numbers to
  // reconcile with rather than let its technical read float free of what
  // the deterministic engine already computes. Price/technical come from
  // the self-sufficient btc_data source (doesn't depend on V1 being
  // opened); sentiment/regime are optional bonus context from V1's
  // history when available — already handled gracefully as "N/A" below
  // when it isn't.
  const latestBtc = await env.DB.prepare(
    'SELECT ts, btc_price, technical_score FROM btc_data ORDER BY ts DESC LIMIT 1'
  ).first();
  const latestHistory = await env.DB.prepare(
    'SELECT ts, score, regime_mag FROM history ORDER BY ts DESC LIMIT 1'
  ).first();
  const btcPrice = latestBtc?.btc_price ?? null;
  const latest = { btc_price: btcPrice, technical_score: latestBtc?.technical_score, score: latestHistory?.score, regime_mag: latestHistory?.regime_mag };

  const prompt = `You are a professional Bitcoin market analyst writing a comprehensive daily briefing. Use your own broad knowledge of current events, markets, and Bitcoin's chart alongside the ground-truth context below.

GROUND TRUTH (real data, reconcile your read with this rather than contradicting it):
Current BTC price: ${btcPrice != null ? '$' + btcPrice.toLocaleString() : 'unknown'}
Current sentiment composite score (0-100): ${latest?.score ?? 'N/A'}
Current technical score (0-100): ${latest?.technical_score ?? 'N/A'}
Current cycle/regime magnitude (-1 bearish to +1 bullish): ${latest?.regime_mag ?? 'N/A'}

RULES:
- Never state a guaranteed outcome or specific future price as fact — this is a positioning read, not a forecast.
- If your knowledge of very recent events may be incomplete, say so plainly.
- Plain text only, no markdown symbols (no #, **, |, >).
- Follow the exact section order below, each section header on its own line in capitals exactly as shown, blank line between sections.

SECTIONS (cover all of these, 2-4 sentences each unless noted):
TRADITIONAL MARKETS: equities, USD strength, what risk appetite looks like right now.
MACRO ECONOMY: inflation, rates, Fed/central bank posture.
GEOPOLITICS: any current tensions or events affecting risk appetite.
CRYPTO MICRO-FACTORS: on-chain activity, ETF flows, whale activity, funding rates, leverage.
TECHNICAL — SHORT TERM (1-7 days): chart structure, momentum, key levels.
TECHNICAL — MEDIUM TERM (2-8 weeks): trend structure.
TECHNICAL — LONG TERM (halving-cycle position): where we likely sit in the cycle.
CROSS-ASSET: Gold (competing-haven vs shared-narrative mode), US 10Y yield, oil.
SENTIMENT: a Fear & Greed style read of crowd psychology right now.
ADOPTION: institutional, regulatory, retail adoption trends.
SYNTHESIS: 2-3 sentences tying it together.

After all sections, on its own final line with nothing else on that line, output exactly this (valid JSON, one line, nothing after it):
ANALYSIS_JSON: {"bias_short":"bullish|neutral|bearish","bias_medium":"bullish|neutral|bearish","bias_long":"bullish|neutral|bearish","support_pct_below":<number, % below current price>,"resistance_pct_above":<number, % above current price>,"macro_risk":"low|medium|high","geopolitical_risk":"low|medium|high","macro_score":<number from -1.0 (very restrictive/bearish macro backdrop) to 1.0 (very supportive/bullish)>,"liquidity_bias":<number from -1.0 (tightening, liquidity draining from risk assets) to 1.0 (loosening, liquidity flowing into risk assets)>,"cross_asset_stress":<number from 0.0 (calm, gold/bonds/oil moving normally) to 1.0 (high dislocation/stress across those markets)>}`;

  const geminiResult = await callGeminiGenerateContent(env, { model, prompt });

  if (!geminiResult.ok) {
    await recordGeminiProviderCall(env, {
      correlationId, consumer, asset: 'BTC', requestTs, model, quotaDecision: 'admitted',
      httpStatus: geminiResult.status, responseStatus: geminiResult.errorCategory, errorCategory: geminiResult.errorCategory,
    }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));
    return { ok: false, status: geminiResult.errorCategory, reason: geminiResult.errorMessage, correlationId };
  }

  const text = geminiResult.text;
  let narrative = text.trim();
  let parsed = {};
  const jsonMatch = narrative.match(/ANALYSIS_JSON:\s*(\{[\s\S]*\})\s*$/);
  if (jsonMatch) {
    try { parsed = JSON.parse(jsonMatch[1]); } catch (e) { /* keep narrative, drop structured fields */ }
    narrative = narrative.slice(0, jsonMatch.index).trim();
  }

  // Deterministic line breaks before each section label, independent of
  // whether Gemini's own formatting cooperated.
  for (const label of ANALYSIS_SECTIONS) {
    narrative = narrative.replace(new RegExp(`\\s*(${label}:)`, 'gi'), `\n\n$1`);
  }
  narrative = narrative.replace(/^\n+/, '').trim();

  const record = {
    ts: Date.now(),
    btc_price_at_analysis: btcPrice,
    bias_short: normalizeBias(parsed.bias_short),
    bias_medium: normalizeBias(parsed.bias_medium),
    bias_long: normalizeBias(parsed.bias_long),
    support_pct_below: Number.isFinite(parsed.support_pct_below) ? parsed.support_pct_below : null,
    resistance_pct_above: Number.isFinite(parsed.resistance_pct_above) ? parsed.resistance_pct_above : null,
    macro_risk: normalizeRisk(parsed.macro_risk),
    geopolitical_risk: normalizeRisk(parsed.geopolitical_risk),
    // Continuous, storage-only for now (see README) — clamped to their
    // documented ranges rather than trusting Gemini to always stay in
    // bounds, same defensiveness as normalizeBias/normalizeRisk above.
    macro_score: Number.isFinite(parsed.macro_score) ? Math.max(-1, Math.min(1, parsed.macro_score)) : null,
    liquidity_bias: Number.isFinite(parsed.liquidity_bias) ? Math.max(-1, Math.min(1, parsed.liquidity_bias)) : null,
    cross_asset_stress: Number.isFinite(parsed.cross_asset_stress) ? Math.max(0, Math.min(1, parsed.cross_asset_stress)) : null,
    narrative,
    raw_json: JSON.stringify(parsed),
  };

  await env.DB.prepare(
    `INSERT INTO gemini_daily_analysis
     (ts, btc_price_at_analysis, bias_short, bias_medium, bias_long, support_pct_below, resistance_pct_above, macro_risk, geopolitical_risk, macro_score, liquidity_bias, cross_asset_stress, narrative, raw_json)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    record.ts, record.btc_price_at_analysis, record.bias_short, record.bias_medium, record.bias_long,
    record.support_pct_below, record.resistance_pct_above, record.macro_risk, record.geopolitical_risk,
    record.macro_score, record.liquidity_bias, record.cross_asset_stress,
    record.narrative, record.raw_json
  ).run();

  await recordGeminiProviderCall(env, {
    correlationId, consumer, asset: 'BTC', requestTs, model, quotaDecision: 'admitted',
    httpStatus: geminiResult.status, responseStatus: 'ok', errorCategory: null,
  }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));

  return { ok: true, status: 'ok', ...record, correlationId };
}

async function getGeminiAnalysisHistory(env, limit) {
  const { results } = await env.DB.prepare(
    'SELECT id, ts, btc_price_at_analysis, bias_short, bias_medium, bias_long, support_pct_below, resistance_pct_above, macro_risk, geopolitical_risk, narrative, realized_return, bias_short_correct FROM gemini_daily_analysis ORDER BY ts DESC LIMIT ?'
  ).bind(limit).all();
  return { ok: true, analyses: results };
}

// Resolves bias_short (the only one of the three horizons that fits a 24h
// check) against what BTC actually did — same nearest-timestamp-match
// technique as backfillPredictions, just applied to this table. bias_medium
// needs 4-6+ weeks to mean anything and bias_long can't be resolved on any
// reasonable timescale at all, so neither is touched here — this is
// deliberately scoped to the one horizon that's actually checkable now.
// "Neutral" is scored correct if the realized move stayed small (<1%, i.e.
// genuinely no big move either way), not graded on direction at all.
async function backfillGeminiBiasShort(env) {
  const { results: btcRows } = await env.DB.prepare(
    'SELECT ts, btc_price FROM btc_data ORDER BY ts ASC'
  ).all();
  const { results: unresolved } = await env.DB.prepare(
    'SELECT id, ts, btc_price_at_analysis, bias_short FROM gemini_daily_analysis WHERE bias_short_correct IS NULL AND ts <= ?'
  ).bind(Date.now() - LAG_MS).all();

  let resolvedCount = 0;
  for (const a of unresolved) {
    if (!a.btc_price_at_analysis || !a.bias_short) continue;
    const match = nearestRow(btcRows, a.ts + LAG_MS);
    if (!match) continue;
    const ret = (match.btc_price - a.btc_price_at_analysis) / a.btc_price_at_analysis * 100;
    const correct =
      a.bias_short === 'bullish' ? ret > 0 :
      a.bias_short === 'bearish' ? ret < 0 :
      Math.abs(ret) < 1.0; // neutral
    await env.DB.prepare(
      'UPDATE gemini_daily_analysis SET realized_btc_price=?, realized_return=?, bias_short_correct=?, resolved_ts=? WHERE id=?'
    ).bind(match.btc_price, ret, correct ? 1 : 0, Date.now(), a.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

// ==================================================================
// LINK module — second coin, added deliberately as a harder test of
// whether a coin-specific model is worth building at all (LINK has its
// own narrative — oracle infra, CCIP/SWIFT — rather than being pure
// BTC-beta), not because it's the safest/easiest choice.
//
// Data source: Hyperliquid ONLY (permissionless, already proven for BTC
// funding, and metaAndAssetCtxs covers LINK in the same call). Deliberately
// NOT using CoinGecko for this coin — avoids a new API-key dependency for
// what would only be a one-time historical backfill. That means LINK's
// technical_score self-bootstraps from its OWN accumulating price log,
// starting at neutral (50) and becoming informative over the following
// weeks — same organic cold-start the BTC model itself went through, no
// artificial acceleration.
//
// Feature vector: LINK's own technical_score + funding rate, PLUS BTC's
// regime_mag and the shared sentiment composite BORROWED via nearest-time
// join to the existing `history` table — those two aren't really
// coin-specific to begin with (crypto-wide macro context and Fear&Greed
// aren't LINK-specific either way), so recomputing them per-coin would
// just be duplicated work for no real gain.
// ==================================================================

const LINK_FUNDING_FLOOR_HOURLY = 0.0000125; // same constant already proven correct for BTC in V1

async function fetchHyperliquidPrice(coinName) {
  const res = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'metaAndAssetCtxs' }),
  });
  if (!res.ok) throw new Error('Hyperliquid info ' + res.status);
  const [meta, ctxs] = await res.json();
  const idx = (meta.universe || []).findIndex(u => u.name === coinName);
  if (idx < 0) throw new Error(coinName + ' not found in Hyperliquid universe');
  const ctx = ctxs[idx];
  const price = parseFloat(ctx.markPx);
  if (!Number.isFinite(price)) throw new Error(coinName + ' markPx not parseable');
  return { price, funding: parseFloat(ctx.funding) };
}
async function fetchLinkSnapshot() {
  const { price, funding } = await fetchHyperliquidPrice('LINK');
  const fundingAdj = funding - LINK_FUNDING_FLOOR_HOURLY;
  return { price, fundingAdj: Number.isFinite(fundingAdj) ? fundingAdj : null };
}
async function fetchBtcSnapshot() {
  const { price } = await fetchHyperliquidPrice('BTC');
  return { price };
}
async function fetchEthSnapshot() {
  const { price } = await fetchHyperliquidPrice('ETH');
  return { price };
}

// Self-bootstrapping technical score (0-100): a simple RSI-style momentum
// read over whatever's accumulated in link_data so far. Explicitly NOT a
// replica of V1's full MACD/Bollinger/OBV/Kumo-twist system — that's a much
// larger build for a second coin; this is an honest, simpler stand-in with
// the same 0-100 direction (higher = more upward momentum).
// Generic self-bootstrapping technical score (0-100, RSI-style), reused by
// both LINK and BTC's self-sufficient pipelines — see the LINK module notes
// above for why this is an honest simpler stand-in, not a replica of V1's
// full indicator system. Takes a plain array of prices, chronological order.
function computeSimpleTechnicalScore(prices) {
  if (prices.length < 6) return 50;
  const changes = [];
  for (let i = 1; i < prices.length; i++) changes.push(prices[i] - prices[i - 1]);
  const gains = changes.filter(c => c > 0);
  const losses = changes.filter(c => c < 0).map(c => -c);
  const avgGain = gains.length ? gains.reduce((a, b) => a + b, 0) / changes.length : 0;
  const avgLoss = losses.length ? losses.reduce((a, b) => a + b, 0) / changes.length : 0;
  if (avgGain + avgLoss === 0) return 50;
  const rs = avgLoss === 0 ? 100 : avgGain / avgLoss;
  const rsi = avgLoss === 0 ? 100 : 100 - (100 / (1 + rs));
  return Math.round(rsi);
}

// One-time (safe to re-run) historical backfill using Hyperliquid's
// candleSnapshot endpoint — the exact same pattern V1 already proved works
// for gold/USD history. Gives the chart and the model real history
// immediately instead of only accumulating from live snapshots going
// forward, which would otherwise take weeks. No funding rate available
// from candles (price only) — that's exactly why funding_adj was dropped
// from the required feature set above rather than kept and left null
// forever. Technical score is computed retroactively using only each
// day's own trailing window of PRIOR candles — no lookahead bias, same as
// any real technical indicator. Generic over coin/table so BTC reuses this
// unchanged.
async function backfillCoinHistory(env, { coin, table, priceCol, days = 90, interval = '1d', dedupToleranceMs = 12 * 60 * 60 * 1000 }) {
  const endTime = Date.now();
  const startTime = endTime - days * 24 * 60 * 60 * 1000;
  const res = await fetch('https://api.hyperliquid.xyz/info', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'candleSnapshot', req: { coin, interval, startTime, endTime } }),
  });
  if (!res.ok) throw new Error(coin + ' candleSnapshot ' + res.status);
  const candles = await res.json();
  if (!Array.isArray(candles) || !candles.length) throw new Error('No candle data returned');

  const sorted = candles
    .map(c => ({ ts: c.t, price: parseFloat(c.c) }))
    .filter(c => Number.isFinite(c.price))
    .sort((a, b) => a.ts - b.ts);

  const { results: existing } = await env.DB.prepare(`SELECT ts FROM ${table} ORDER BY ts ASC`).all();
  const existingTs = existing.map(r => r.ts);

  let inserted = 0;
  const window = [];
  for (const c of sorted) {
    window.push(c.price);
    if (window.length > 30) window.shift();
    const nearDup = existingTs.some(ts => Math.abs(ts - c.ts) < dedupToleranceMs);
    if (nearDup) continue;
    const techScore = computeSimpleTechnicalScore(window.slice());
    await env.DB.prepare(
      `INSERT INTO ${table} (ts, ${priceCol}, technical_score) VALUES (?,?,?)`
    ).bind(c.ts, c.price, techScore).run();
    inserted++;
  }
  return { ok: true, candles_received: sorted.length, rows_inserted: inserted };
}
async function backfillLinkHistory(env, days = 90) {
  // LINK's table also has a funding_adj column (unused by backfill, always
  // null here) — reuse the generic backfill, which only ever inserts the
  // 3 shared columns, so this is fully compatible with link_data's schema.
  return backfillCoinHistory(env, { coin: 'LINK', table: 'link_data', priceCol: 'link_price', days });
}
async function backfillBtcHistory(env, days = 90) {
  return backfillCoinHistory(env, { coin: 'BTC', table: 'btc_data', priceCol: 'btc_price', days });
}
async function backfillEthHistory(env, days = 90) {
  return backfillCoinHistory(env, { coin: 'ETH', table: 'eth_data', priceCol: 'eth_price', days });
}
// Hourly, shorter-window backfill — the daily backfill above is 24h-spaced
// by construction, so it can never satisfy a 12h-forward lookup for any
// historical candidate (confirmed directly: zero BTC 12h predictions could
// resolve until this existed). 30 min dedup tolerance instead of 12h, or
// every hourly candle near an existing daily one would get wrongly skipped
// as a "duplicate."
async function backfillLinkHistoryHourly(env, days = 20) {
  return backfillCoinHistory(env, { coin: 'LINK', table: 'link_data', priceCol: 'link_price', days, interval: '1h', dedupToleranceMs: 30 * 60 * 1000 });
}
async function backfillBtcHistoryHourly(env, days = 20) {
  return backfillCoinHistory(env, { coin: 'BTC', table: 'btc_data', priceCol: 'btc_price', days, interval: '1h', dedupToleranceMs: 30 * 60 * 1000 });
}
async function backfillEthHistoryHourly(env, days = 20) {
  return backfillCoinHistory(env, { coin: 'ETH', table: 'eth_data', priceCol: 'eth_price', days, interval: '1h', dedupToleranceMs: 30 * 60 * 1000 });
}

// ==================================================================
// Offline backtest — a genuinely different test than the live model's
// calibration loop. The live loop needs weeks to accumulate enough
// resolved predictions to say anything real; this replays YEARS of real
// BTC price history in one call, using the same Yahoo v8/chart endpoint
// V1's "9 Magnificent" tile already uses (same URL pattern, same
// User-Agent, proven working — not a new integration).
//
// Deliberately a SIMPLIFIED version of the live model: technical_score is
// the only feature, because it's the only one computable from price alone
// — sentiment/regime_mag/bottom_score don't exist for years back, they
// only started being logged in July 2026. This tests whether the CORE
// analog-matching idea has any merit at all, not the full live feature
// set. With one feature, z-scoring is a no-op (a monotonic transform of a
// single dimension doesn't change nearest-neighbor ordering), so it's
// skipped entirely here — genuine simplification, not an oversight.
//
// Walk-forward, not just historical: predicting day i only ever uses
// candidate days STRICTLY BEFORE day i (never future days, even though
// the whole series is already known) — the same no-lookahead discipline
// as the live model, replayed against history instead of real time.
// ==================================================================

async function fetchYahooDailyHistory(symbol, range) {
  const res = await fetch(
    `https://query1.finance.yahoo.com/v8/finance/chart/${symbol}?interval=1d&range=${range}`,
    { headers: { 'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36' } }
  );
  if (!res.ok) throw new Error(`Yahoo ${symbol} ${res.status}`);
  const json = await res.json();
  const result = json?.chart?.result?.[0];
  if (!result) throw new Error(`Yahoo ${symbol}: no result in response`);
  const timestamps = result.timestamp || [];
  const closes = result.indicators?.quote?.[0]?.close || [];
  return timestamps
    .map((t, i) => ({ ts: t * 1000, price: closes[i] }))
    .filter(p => p.price != null)
    .sort((a, b) => a.ts - b.ts);
}

function runOfflineBacktest(priceSeries, K = 15) {
  // technical_score per day, walk-forward (only prior days feed each day's
  // own score) — reuses the exact same function the live pipeline uses.
  const scored = priceSeries.map((p, i) => {
    const window = priceSeries.slice(Math.max(0, i - 29), i + 1).map(r => r.price);
    return { ts: p.ts, price: p.price, technical_score: computeSimpleTechnicalScore(window) };
  });

  const predictions = [];
  // Start once there's a reasonable amount of walk-forward history behind
  // us (60 days — arbitrary but modest warmup), stop one day early so
  // there's always a real "tomorrow" to grade against.
  for (let i = 60; i < scored.length - 1; i++) {
    const today = scored[i];
    const candidates = scored.slice(0, i); // strictly before today — no lookahead
    const withReturns = candidates
      .map((c, ci) => {
        const next = scored[ci + 1]; // the day immediately after this candidate — already-known history, since ci+1 <= i (today), never a future leak
        if (!next) return null;
        return { dist: Math.abs(c.technical_score - today.technical_score), return_pct: (next.price - c.price) / c.price * 100 };
      })
      .filter(Boolean)
      .sort((a, b) => a.dist - b.dist);

    if (withReturns.length < K) continue; // not enough history yet for this K
    const neighbors = withReturns.slice(0, K);
    const pUp = neighbors.filter(n => n.return_pct > 0).length / neighbors.length;

    const tomorrow = scored[i + 1];
    const realizedReturn = (tomorrow.price - today.price) / today.price * 100;
    const realizedUp = realizedReturn > 0 ? 1 : 0;

    predictions.push({ ts: today.ts, p_up: pUp, realized_up: realizedUp });
  }

  const n = predictions.length;
  if (n === 0) return { ok: true, n_predictions: 0, note: 'Not enough history in the fetched range to run any predictions.' };

  const accuracy = predictions.filter(p => (p.p_up >= 0.5) === (p.realized_up === 1)).length / n;
  const brier = predictions.reduce((s, p) => s + (p.p_up - p.realized_up) ** 2, 0) / n;
  const upRate = predictions.filter(p => p.realized_up === 1).length / n;
  const brierBaseRate = predictions.reduce((s, p) => s + (upRate - p.realized_up) ** 2, 0) / n;
  const bestNaive = Math.min(0.25, brierBaseRate);

  return {
    ok: true,
    n_predictions: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)),
    naive_baseline_brier: Number(bestNaive.toFixed(3)),
    beats_naive_baseline: brier < bestNaive,
    date_range_start: predictions[0].ts,
    date_range_end: predictions[n - 1].ts,
    note: 'Technical_score only (single feature) — sentiment/regime don\'t exist this far back. Tests the core analog-matching idea, not the full live model.',
  };
}

async function runBtcOfflineBacktest(env, years = 3) {
  const range = `${Math.min(10, Math.max(1, Math.round(years)))}y`;
  const priceSeries = await fetchYahooDailyHistory('BTC-USD', range);
  if (priceSeries.length < 100) throw new Error(`Yahoo returned only ${priceSeries.length} days — too little to backtest`);

  const result = runOfflineBacktest(priceSeries);

  await env.DB.prepare(
    `INSERT INTO backtest_results (run_ts, coin, years_tested, n_days_in_source, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline, date_range_start, date_range_end)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    Date.now(), 'BTC', years, priceSeries.length, result.n_predictions,
    result.accuracy ?? null, result.brier_score ?? null, result.naive_baseline_brier ?? null,
    result.beats_naive_baseline ? 1 : 0, result.date_range_start ?? null, result.date_range_end ?? null
  ).run();

  return result;
}

// Same walk-forward single-feature backtest, generalized for regime_mag /
// sentiment — UNLIKE technical_score, neither has a multi-year external
// source (Yahoo has price, not V1's own composite calculations), so this
// is necessarily limited to whatever D1 has logged since V1 started
// tracking these (~July 25 2026 onward) — a much smaller, honestly
// weaker test than the 1035-day technical_score backtest. Built anyway,
// on the same "verify before trusting" discipline as everything else —
// small-sample results reported as directional, not conclusive.
const HISTORY_BACKTEST_FEATURES = { regime_mag: 'regime_mag', sentiment: 'score' };

function runGenericSingleFeatureBacktest(rows, K) {
  const predictions = [];
  const warmup = Math.max(10, Math.floor(rows.length * 0.15));
  for (let i = warmup; i < rows.length - 1; i++) {
    const today = rows[i];
    const candidates = rows.slice(0, i);
    const withReturns = candidates
      .map((c, ci) => {
        const next = rows[ci + 1];
        if (!next) return null;
        return { dist: Math.abs(c.feat - today.feat), return_pct: (next.price - c.price) / c.price * 100 };
      })
      .filter(Boolean)
      .sort((a, b) => a.dist - b.dist);
    if (withReturns.length < K) continue;
    const neighbors = withReturns.slice(0, K);
    const pUp = neighbors.filter(n => n.return_pct > 0).length / neighbors.length;
    const tomorrow = rows[i + 1];
    const realizedReturn = (tomorrow.price - today.price) / today.price * 100;
    predictions.push({ ts: today.ts, p_up: pUp, realized_up: realizedReturn > 0 ? 1 : 0 });
  }

  const n = predictions.length;
  if (n === 0) return { ok: true, n_predictions: 0, note: 'Not enough history in D1 to run any predictions yet — this feature is still too young.' };

  const accuracy = predictions.filter(p => (p.p_up >= 0.5) === (p.realized_up === 1)).length / n;
  const brier = predictions.reduce((s, p) => s + (p.p_up - p.realized_up) ** 2, 0) / n;
  const upRate = predictions.filter(p => p.realized_up === 1).length / n;
  const brierBaseRate = predictions.reduce((s, p) => s + (upRate - p.realized_up) ** 2, 0) / n;
  const bestNaive = Math.min(0.25, brierBaseRate);

  return {
    ok: true,
    n_predictions: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)),
    naive_baseline_brier: Number(bestNaive.toFixed(3)),
    beats_naive_baseline: brier < bestNaive,
    date_range_start: predictions[0].ts,
    date_range_end: predictions[n - 1].ts,
    note: n < 50
      ? `Only ${n} predictions — this feature has been logged for under 2 weeks, treat as directional only, not conclusive.`
      : 'Single-feature test, same discipline as the technical_score backtest.',
  };
}

async function runHistoryFeatureBacktest(env, featureName, K = 10) {
  const col = HISTORY_BACKTEST_FEATURES[featureName];
  if (!col) throw new Error(`Unknown feature "${featureName}" — must be one of: ${Object.keys(HISTORY_BACKTEST_FEATURES).join(', ')}`);

  const { results } = await env.DB.prepare(
    `SELECT ts, btc_price as price, ${col} as feat FROM history WHERE btc_price IS NOT NULL AND ${col} IS NOT NULL ORDER BY ts ASC`
  ).all();

  const result = runGenericSingleFeatureBacktest(results, K);

  await env.DB.prepare(
    `INSERT INTO backtest_results (run_ts, coin, years_tested, n_days_in_source, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline, date_range_start, date_range_end)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    Date.now(), `BTC-${featureName}`, 0, results.length, result.n_predictions,
    result.accuracy ?? null, result.brier_score ?? null, result.naive_baseline_brier ?? null,
    result.beats_naive_baseline ? 1 : 0, result.date_range_start ?? null, result.date_range_end ?? null
  ).run();

  return result;
}

// Combined 4-feature backtest — the closest thing to an honest backtest of
// the ACTUAL live model, over the shared window where score/technical_score/
// regime_mag/bottom_score all coexist (~9 days right now, limited by
// regime_mag being the newest field). Unlike the single-feature tests,
// z-scoring genuinely matters here (4 differently-scaled features), and is
// computed fresh from ONLY prior data at each step — walk-forward honest,
// actually stricter than the live model itself (which reuses one set of
// whole-history stats per prediction cycle rather than recomputing them
// day by day). Answers the real question the single-feature results raised:
// does combining four individually-non-predictive features do any better
// than each alone, or does averaging noise just produce more noise.
const COMBINED_BACKTEST_FEATURES = ['score', 'technical_score', 'regime_mag', 'bottom_score'];

function runCombinedFeatureBacktest(rows, featureKeys, K) {
  const predictions = [];
  const warmup = Math.max(15, Math.floor(rows.length * 0.2)); // a bit more than the single-feature warmup — stats need to stabilize across 4 dimensions, not 1
  for (let i = warmup; i < rows.length - 1; i++) {
    const today = rows[i];
    const candidates = rows.slice(0, i); // strictly before today — same no-lookahead rule as every other backtest here

    const stats = {};
    for (const k of featureKeys) {
      const vals = candidates.map(r => r[k]);
      const mean = vals.reduce((a, b) => a + b, 0) / vals.length;
      const variance = vals.reduce((a, b) => a + (b - mean) ** 2, 0) / vals.length;
      stats[k] = { mean, std: Math.sqrt(variance) || 1 };
    }

    const withReturns = candidates
      .map((c, ci) => {
        const next = rows[ci + 1];
        if (!next) return null;
        let d = 0;
        for (const k of featureKeys) {
          const z1 = (today[k] - stats[k].mean) / stats[k].std;
          const z2 = (c[k] - stats[k].mean) / stats[k].std;
          d += (z1 - z2) ** 2;
        }
        return { dist: Math.sqrt(d), return_pct: (next.price - c.price) / c.price * 100 };
      })
      .filter(Boolean)
      .sort((a, b) => a.dist - b.dist);

    if (withReturns.length < K) continue;
    const neighbors = withReturns.slice(0, K);
    const pUp = neighbors.filter(n => n.return_pct > 0).length / neighbors.length;
    const tomorrow = rows[i + 1];
    const realizedReturn = (tomorrow.price - today.price) / today.price * 100;
    predictions.push({ ts: today.ts, p_up: pUp, realized_up: realizedReturn > 0 ? 1 : 0 });
  }

  const n = predictions.length;
  if (n === 0) return { ok: true, n_predictions: 0, note: 'Not enough shared history across all 4 features yet to run any predictions.' };

  const accuracy = predictions.filter(p => (p.p_up >= 0.5) === (p.realized_up === 1)).length / n;
  const brier = predictions.reduce((s, p) => s + (p.p_up - p.realized_up) ** 2, 0) / n;
  const upRate = predictions.filter(p => p.realized_up === 1).length / n;
  const brierBaseRate = predictions.reduce((s, p) => s + (upRate - p.realized_up) ** 2, 0) / n;
  const bestNaive = Math.min(0.25, brierBaseRate);

  return {
    ok: true,
    n_predictions: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)),
    naive_baseline_brier: Number(bestNaive.toFixed(3)),
    beats_naive_baseline: brier < bestNaive,
    date_range_start: predictions[0].ts,
    date_range_end: predictions[n - 1].ts,
    note: n < 50
      ? `Only ${n} predictions — limited by regime_mag being the newest field (~9 days of shared history). Directional only.`
      : 'Combined 4-feature test, walk-forward stats — the closest available backtest of the actual live model.',
  };
}

async function runHistoryCombinedBacktest(env, K = 10) {
  const cols = COMBINED_BACKTEST_FEATURES;
  const whereClause = cols.map(c => `${c} IS NOT NULL`).join(' AND ');
  const { results } = await env.DB.prepare(
    `SELECT ts, btc_price as price, score, technical_score, regime_mag, bottom_score FROM history WHERE btc_price IS NOT NULL AND ${whereClause} ORDER BY ts ASC`
  ).all();

  const result = runCombinedFeatureBacktest(results, cols, K);

  await env.DB.prepare(
    `INSERT INTO backtest_results (run_ts, coin, years_tested, n_days_in_source, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline, date_range_start, date_range_end)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    Date.now(), 'BTC-combined4', 0, results.length, result.n_predictions,
    result.accuracy ?? null, result.brier_score ?? null, result.naive_baseline_brier ?? null,
    result.beats_naive_baseline ? 1 : 0, result.date_range_start ?? null, result.date_range_end ?? null
  ).run();

  return result;
}

async function getBacktestHistory(env, limit = 50) {
  const { results } = await env.DB.prepare(
    'SELECT id, run_ts, coin, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline, date_range_start, date_range_end FROM backtest_results ORDER BY run_ts DESC LIMIT ?'
  ).bind(limit).all();
  return { ok: true, runs: results };
}

// Tests a specific, sharper hypothesis than "maybe reweight the features" —
// one paper found technical indicators' predictive power is CONDITIONAL on
// sentiment regime (better during "greed," worse during "fear"), not just
// a fixed weight to tune. Same walk-forward technical_score-only backtest
// as before, but every prediction is tagged with that day's actual
// sentiment reading, then split by whether it was above or below the
// median seen across the whole run — testing whether technical_score's
// OWN accuracy genuinely differs by regime, not building two separate
// models.
function runRegimeSplitBacktest(rows, K = 10) {
  const predictions = [];
  const warmup = Math.max(10, Math.floor(rows.length * 0.15));
  for (let i = warmup; i < rows.length - 1; i++) {
    const today = rows[i];
    const candidates = rows.slice(0, i);
    const withReturns = candidates
      .map((c, ci) => {
        const next = rows[ci + 1];
        if (!next) return null;
        return { dist: Math.abs(c.technical_score - today.technical_score), return_pct: (next.price - c.price) / c.price * 100 };
      })
      .filter(Boolean)
      .sort((a, b) => a.dist - b.dist);
    if (withReturns.length < K) continue;
    const neighbors = withReturns.slice(0, K);
    const pUp = neighbors.filter(n => n.return_pct > 0).length / neighbors.length;
    const tomorrow = rows[i + 1];
    const realizedReturn = (tomorrow.price - today.price) / today.price * 100;
    predictions.push({ ts: today.ts, p_up: pUp, realized_up: realizedReturn > 0 ? 1 : 0, sentiment: today.sentiment });
  }

  if (predictions.length === 0) return { ok: true, n_total: 0, note: 'Not enough shared technical_score/sentiment history yet.' };

  const sortedSentiments = predictions.map(p => p.sentiment).sort((a, b) => a - b);
  const medianSentiment = sortedSentiments[Math.floor(sortedSentiments.length / 2)];
  const highRegime = predictions.filter(p => p.sentiment >= medianSentiment);
  const lowRegime = predictions.filter(p => p.sentiment < medianSentiment);

  function aggregate(preds) {
    const n = preds.length;
    if (n === 0) return null;
    const accuracy = preds.filter(p => (p.p_up >= 0.5) === (p.realized_up === 1)).length / n;
    const brier = preds.reduce((s, p) => s + (p.p_up - p.realized_up) ** 2, 0) / n;
    const upRate = preds.filter(p => p.realized_up === 1).length / n;
    const brierBase = preds.reduce((s, p) => s + (upRate - p.realized_up) ** 2, 0) / n;
    const bestNaive = Math.min(0.25, brierBase);
    return {
      n_predictions: n,
      accuracy: Number(accuracy.toFixed(3)),
      brier_score: Number(brier.toFixed(3)),
      naive_baseline_brier: Number(bestNaive.toFixed(3)),
      beats_naive_baseline: brier < bestNaive,
    };
  }

  return {
    ok: true,
    n_total: predictions.length,
    median_sentiment_threshold: Number(medianSentiment.toFixed(1)),
    high_sentiment: aggregate(highRegime),
    low_sentiment: aggregate(lowRegime),
    note: 'Same technical_score-only k-NN as the standalone backtest, split post-hoc by that day\'s actual sentiment reading — tests whether technical_score\'s own accuracy genuinely differs by regime, not two separately-tuned models.',
  };
}

async function runBtcRegimeSplitBacktest(env) {
  const { results } = await env.DB.prepare(
    `SELECT ts, btc_price as price, technical_score, score as sentiment FROM history WHERE btc_price IS NOT NULL AND technical_score IS NOT NULL AND score IS NOT NULL ORDER BY ts ASC`
  ).all();

  const result = runRegimeSplitBacktest(results);
  const runTs = Date.now();

  if (result.high_sentiment) {
    await env.DB.prepare(
      `INSERT INTO regime_split_results (run_ts, split_label, median_sentiment_threshold, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline) VALUES (?,?,?,?,?,?,?,?)`
    ).bind(runTs, 'high_sentiment', result.median_sentiment_threshold, result.high_sentiment.n_predictions, result.high_sentiment.accuracy, result.high_sentiment.brier_score, result.high_sentiment.naive_baseline_brier, result.high_sentiment.beats_naive_baseline ? 1 : 0).run();
  }
  if (result.low_sentiment) {
    await env.DB.prepare(
      `INSERT INTO regime_split_results (run_ts, split_label, median_sentiment_threshold, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline) VALUES (?,?,?,?,?,?,?,?)`
    ).bind(runTs, 'low_sentiment', result.median_sentiment_threshold, result.low_sentiment.n_predictions, result.low_sentiment.accuracy, result.low_sentiment.brier_score, result.low_sentiment.naive_baseline_brier, result.low_sentiment.beats_naive_baseline ? 1 : 0).run();
  }

  return result;
}

async function getRegimeSplitHistory(env, limit = 50) {
  const { results } = await env.DB.prepare(
    'SELECT id, run_ts, split_label, median_sentiment_threshold, n_predictions, accuracy, brier_score, naive_baseline_brier, beats_naive_baseline FROM regime_split_results ORDER BY run_ts DESC LIMIT ?'
  ).bind(limit).all();
  return { ok: true, runs: results };
}

async function logLinkData(env) {
  const snap = await fetchLinkSnapshot();
  const { results: recent } = await env.DB.prepare(
    'SELECT link_price FROM link_data ORDER BY ts DESC LIMIT 30'
  ).all();
  const technicalScore = computeSimpleTechnicalScore(recent.reverse().map(r => r.link_price));
  await env.DB.prepare(
    'INSERT INTO link_data (ts, link_price, technical_score, funding_adj) VALUES (?,?,?,?)'
  ).bind(Date.now(), snap.price, technicalScore, snap.fundingAdj).run();
  return { price: snap.price, technical_score: technicalScore, funding_adj: snap.fundingAdj };
}

// BTC's own self-sufficient price+technical logging — same pattern as LINK,
// built so the BTC model no longer depends on V1's history table getting
// fresh rows. V1's history table is only ever updated when someone opens
// V1 in a browser (confirmed directly: INSERT INTO history only happens in
// the client-triggered POST /history route, no server cron touches it) —
// so without this, both making a fresh BTC prediction AND resolving old
// ones against reality would silently stall whenever V1 goes unvisited.
async function logBtcData(env) {
  const snap = await fetchBtcSnapshot();
  const { results: recent } = await env.DB.prepare(
    'SELECT btc_price FROM btc_data ORDER BY ts DESC LIMIT 30'
  ).all();
  const technicalScore = computeSimpleTechnicalScore(recent.reverse().map(r => r.btc_price));
  await env.DB.prepare(
    'INSERT INTO btc_data (ts, btc_price, technical_score) VALUES (?,?,?)'
  ).bind(Date.now(), snap.price, technicalScore).run();
  return { price: snap.price, technical_score: technicalScore };
}
// Mirrors logBtcData exactly, same reasoning: without this, ETH predictions
// could never resolve against reality between V1 page visits, same
// dependency gap that would have silently stalled BTC/LINK too.
async function logEthData(env) {
  const snap = await fetchEthSnapshot();
  const { results: recent } = await env.DB.prepare(
    'SELECT eth_price FROM eth_data ORDER BY ts DESC LIMIT 30'
  ).all();
  const technicalScore = computeSimpleTechnicalScore(recent.reverse().map(r => r.eth_price));
  await env.DB.prepare(
    'INSERT INTO eth_data (ts, eth_price, technical_score) VALUES (?,?,?)'
  ).bind(Date.now(), snap.price, technicalScore).run();
  return { price: snap.price, technical_score: technicalScore };
}

// technical_score + borrowed BTC regime/sentiment only — funding_adj is
// still logged on every LIVE cron snapshot (bonus context, potentially
// useful later) but deliberately not required here: real historical
// candles (used for backfill below) only carry price, no funding rate, and
// requiring it would disqualify every backfilled row from ever being a
// usable analog.
const LINK_FEATURE_KEYS = ['technical_score', 'btc_regime_mag', 'sentiment_score'];
const LINK_MIN_COMPLETE_ROWS = 30;
const LINK_MIN_RESOLVED_ANALOGS = 5;

async function runLinkPrediction(env, horizonHours = 24, { persist = true } = {}) {
  const lagMs = horizonHours * 60 * 60 * 1000;
  const tolMs = lagMs * 0.2;
  const { results: linkRows } = await env.DB.prepare(
    'SELECT ts, link_price, technical_score, funding_adj FROM link_data ORDER BY ts ASC'
  ).all();
  if (linkRows.length < LINK_MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: linkRows.length, min_required: LINK_MIN_COMPLETE_ROWS };
  }

  // Borrow BTC's regime_mag and the shared sentiment composite via
  // nearest-time join — these aren't coin-specific, no reason to duplicate
  // the underlying computation for a second coin. IMPORTANT: this context
  // only exists for the last ~7 days (V1 only started logging regime_mag
  // on 2026-07-25), while LINK's own backfilled price history goes back 90
  // days — so most rows won't have a real match. Rather than drop them
  // (which left only ~13 usable rows, well under the 30 minimum), missing
  // context gets mean-imputed from whatever real matches DO exist. After
  // z-scoring, an imputed mean value contributes exactly zero pull on that
  // feature's dimension — neutral, not a biased guess — while still
  // letting the full technical_score history do its job for the analog
  // match. Flagged per-row so this is auditable, not silently smoothed over.
  const { results: btcHistory } = await env.DB.prepare(
    'SELECT ts, score, regime_mag FROM history WHERE regime_mag IS NOT NULL ORDER BY ts ASC'
  ).all();
  const rawRows = [];
  const realRegimeVals = [], realSentimentVals = [];
  for (const r of linkRows) {
    if (r.technical_score == null) continue;
    const nearestBtc = nearestRow(btcHistory, r.ts);
    rawRows.push({
      ts: r.ts, link_price: r.link_price, technical_score: r.technical_score,
      btc_regime_mag: nearestBtc?.regime_mag ?? null, sentiment_score: nearestBtc?.score ?? null,
    });
    if (nearestBtc) { realRegimeVals.push(nearestBtc.regime_mag); realSentimentVals.push(nearestBtc.score); }
  }
  const meanRegime = realRegimeVals.length ? realRegimeVals.reduce((a, b) => a + b, 0) / realRegimeVals.length : 0;
  const meanSentiment = realSentimentVals.length ? realSentimentVals.reduce((a, b) => a + b, 0) / realSentimentVals.length : 50;
  const complete = rawRows.map(r => ({
    ts: r.ts, link_price: r.link_price, technical_score: r.technical_score,
    btc_regime_mag: r.btc_regime_mag ?? meanRegime,
    sentiment_score: r.sentiment_score ?? meanSentiment,
    context_imputed: r.btc_regime_mag == null || r.sentiment_score == null,
  }));
  if (complete.length < LINK_MIN_COMPLETE_ROWS) {
    return { ok: true, status: 'insufficient_data', n_available: complete.length, min_required: LINK_MIN_COMPLETE_ROWS };
  }

  const stats = {};
  for (const k of LINK_FEATURE_KEYS) stats[k] = meanStd(complete.map(r => r[k]));

  const today = complete[complete.length - 1];
  // Same fix as BTC's model: exclude candidates too recent to possibly have
  // resolved yet, so a dense recent cluster can't crowd out genuinely
  // resolvable older candidates.
  const candidates = complete.slice(0, -1).filter(r => r.ts <= today.ts - (lagMs + tolMs));

  const distances = candidates.map(r => {
    let d = 0;
    for (const k of LINK_FEATURE_KEYS) {
      const z1 = (today[k] - stats[k].mean) / stats[k].std;
      const z2 = (r[k] - stats[k].mean) / stats[k].std;
      d += (z1 - z2) ** 2;
    }
    return { row: r, dist: Math.sqrt(d) };
  }).sort((a, b) => a.dist - b.dist);

  const K = Math.min(15, Math.max(5, Math.floor(candidates.length / 3)));
  const neighbors = distances.slice(0, K);

  const resolved = neighbors
    .map(n => {
      const fwd = nearestRow(linkRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { analog_ts: n.row.ts, dist: n.dist, return_pct: (fwd.link_price - n.row.link_price) / n.row.link_price * 100 };
    })
    .filter(Boolean);

  if (resolved.length < LINK_MIN_RESOLVED_ANALOGS) {
    return { ok: true, status: 'insufficient_resolved_analogs', n_neighbors: neighbors.length, n_resolved: resolved.length, min_required: LINK_MIN_RESOLVED_ANALOGS };
  }

  const returns = resolved.map(r => r.return_pct).sort((a, b) => a - b);
  const nUp = returns.filter(r => r > 0).length;
  const pUp = nUp / returns.length;
  const pct = (p) => returns[Math.min(returns.length - 1, Math.floor(returns.length * p))];
  const median = pct(0.5), p25 = pct(0.25), p75 = pct(0.75);

  // ---- Experimental: adaptive K + distance-weighted aggregation ----
  // Same technique proven on BTC's model, ported here to check whether it's
  // a real methodological improvement or something that happened to fit
  // BTC's specific sample. Runs alongside the fixed-K/unweighted numbers
  // above, not instead of them.
  const historicalVol = candidates.map((_, i) => trailingVolatility(candidates, i, 14, 'link_price'));
  const todayVol = trailingVolatility(complete, complete.length - 1, 14, 'link_price');
  const volPercentile = todayVol != null ? percentileRank(todayVol, historicalVol) : null;
  let kAdaptive = K;
  if (volPercentile != null) {
    if (volPercentile >= 0.66) kAdaptive = Math.max(5, Math.floor(K * 0.6));
    else if (volPercentile <= 0.33) kAdaptive = Math.min(candidates.length, Math.floor(K * 1.4));
  }
  const neighborsAdaptive = distances.slice(0, kAdaptive);
  const resolvedAdaptive = neighborsAdaptive
    .map(n => {
      const fwd = nearestRow(linkRows, n.row.ts + lagMs, tolMs);
      if (!fwd) return null;
      return { dist: n.dist, return_pct: (fwd.link_price - n.row.link_price) / n.row.link_price * 100 };
    })
    .filter(Boolean);

  let pUpExperimental = null, medianReturnExperimental = null;
  if (resolvedAdaptive.length >= LINK_MIN_RESOLVED_ANALOGS) {
    const EPS = 0.05;
    const weighted = resolvedAdaptive.map(r => ({ value: r.return_pct, weight: 1 / (r.dist + EPS) }));
    const totalWeight = weighted.reduce((s, w) => s + w.weight, 0);
    const upWeight = weighted.filter(w => w.value > 0).reduce((s, w) => s + w.weight, 0);
    pUpExperimental = upWeight / totalWeight;
    medianReturnExperimental = weightedQuantile(weighted, 0.5);
  }

  // Regime-anomaly tripwire — same idea as BTC's: is even the closest
  // analog unusually far away compared to every closest-match distance
  // seen historically for LINK specifically.
  const closestDist = distances[0].dist;
  const historicalClosestDists = candidates.map((_, i) => {
    if (i === 0) return null;
    let best = Infinity;
    for (let j = 0; j < i; j++) {
      let d = 0;
      for (const k of LINK_FEATURE_KEYS) {
        const z1 = (candidates[i][k] - stats[k].mean) / stats[k].std;
        const z2 = (candidates[j][k] - stats[k].mean) / stats[k].std;
        d += (z1 - z2) ** 2;
      }
      d = Math.sqrt(d);
      if (d < best) best = d;
    }
    return Number.isFinite(best) ? best : null;
  });
  const closestDistPercentile = percentileRank(closestDist, historicalClosestDists);
  const isRegimeAnomaly = closestDistPercentile != null && closestDistPercentile >= 0.9;

  const trend = trendStrength(complete, 'link_price');
  const curveRows = await getLatestCalibrationCurve(env, 'LINK', horizonHours);
  const calibratedPUp = applyCalibratedProbability(pUp, curveRows);

  const nowTs = Date.now();
  const features = Object.fromEntries(LINK_FEATURE_KEYS.map(k => [k, today[k]]));

  let insert = null;
  if (persist) {
    insert = await env.DB.prepare(
      `INSERT INTO link_predictions
       (ts, target_ts, link_price_at_prediction, p_up, n_analogs, median_analog_return, return_p25, return_p75, features_json, horizon_hours,
        k_used, volatility_percentile, closest_analog_dist, is_regime_anomaly, p_up_experimental, median_return_experimental,
        trend_strength, calibrated_p_up, model_version, git_commit_sha)
       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
    ).bind(
      nowTs, nowTs + lagMs, today.link_price, pUp, resolved.length, median, p25, p75, JSON.stringify(features), horizonHours,
      kAdaptive, volPercentile, closestDist, isRegimeAnomaly ? 1 : 0, pUpExperimental, medianReturnExperimental,
      trend, calibratedPUp, MODEL_VERSIONS.link_core, currentGitSha(env)
    ).run();
  }

  const nImputedInNeighbors = neighbors.filter(n => n.row.context_imputed).length;

  return {
    ok: true, status: 'ok', prediction_id: insert ? insert.meta.last_row_id : null, persisted: persist, ts: nowTs, horizon_hours: horizonHours,
    p_up: Number(pUp.toFixed(3)), calibrated_p_up: Number(calibratedPUp.toFixed(3)), trend_strength: Number(trend.toFixed(3)),
    n_analogs: resolved.length,
    median_analog_return_pct: Number(median.toFixed(2)),
    return_range_pct: [Number(p25.toFixed(2)), Number(p75.toFixed(2))],
    link_price_now: today.link_price, features,
    top_analogs: resolved.slice(0, 5).map(r => ({ date: new Date(r.analog_ts).toISOString().slice(0, 10), return_pct: Number(r.return_pct.toFixed(2)) })),
    regime_anomaly: isRegimeAnomaly,
    experimental: {
      k_used: kAdaptive,
      volatility_percentile: volPercentile != null ? Number(volPercentile.toFixed(2)) : null,
      p_up: pUpExperimental != null ? Number(pUpExperimental.toFixed(3)) : null,
      median_return_pct: medianReturnExperimental != null ? Number(medianReturnExperimental.toFixed(2)) : null,
      note: 'Adaptive-K + distance-weighted variant, logged in parallel — proven on BTC first, now being independently tested on LINK\'s own data.',
    },
    note: `Based on the ${resolved.length} most similar days in ${candidates.length} days of LINK history (${nImputedInNeighbors} of the ${neighbors.length} matched days used an averaged macro/sentiment reading, real BTC data only goes back 7 days). Technical score is a simplified RSI-style read, not V1's full indicator system — and this whole model is much younger than BTC's. Read with extra caution relative to the BTC prediction.`,
  };
}

async function backfillLinkPredictions(env) {
  const { results: linkRows } = await env.DB.prepare(
    'SELECT ts, link_price FROM link_data ORDER BY ts ASC'
  ).all();
  const { results: unresolved } = await env.DB.prepare(
    'SELECT id, target_ts, link_price_at_prediction FROM link_predictions WHERE realized_up IS NULL AND target_ts <= ?'
  ).bind(Date.now()).all();

  let resolvedCount = 0;
  for (const p of unresolved) {
    const match = nearestRow(linkRows, p.target_ts);
    if (!match) continue;
    const ret = (match.link_price - p.link_price_at_prediction) / p.link_price_at_prediction * 100;
    await env.DB.prepare(
      'UPDATE link_predictions SET realized_link_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'
    ).bind(match.link_price, ret, ret > 0 ? 1 : 0, Date.now(), p.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

async function linkPredictAndLog(env, horizonHours = 24, { allowWrite = false } = {}) {
  const resolvedCount = await backfillLinkPredictions(env); // resolution is unconditional, same reasoning as predictAndLog
  const shouldWrite = allowWrite || (await isRecentDataStale(env, 'link_data') && await claimStaleRefresh(env, 'LINK', Date.now()));
  if (shouldWrite) await logLinkData(env);
  const result = await runLinkPrediction(env, horizonHours, { persist: shouldWrite });
  result.backfilled_this_call = resolvedCount;
  try {
    const challengerResolvedCount = await backfillChallengerPredictions(env);
    const challengerResult = await runChallengerPrediction(env, {
      coin: 'LINK', horizonHours, priceTable: 'link_data', priceCol: 'link_price', priceNow: result.link_price_now, coreResult: result,
    }, { persist: shouldWrite });
    result.challenger = challengerResult;
    result.challenger_backfilled_this_call = challengerResolvedCount;
  } catch (e) {
    result.challenger = { ok: false, error: String(e) };
  }
  return result;
}

async function getLinkCalibration(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT p_up, realized_up, p_up_experimental FROM link_predictions WHERE realized_up IS NOT NULL AND horizon_hours = ?'
  ).bind(horizonHours).all();
  const n = results.length;
  if (n === 0) return { ok: true, n_resolved: 0, note: `No resolved LINK ${horizonHours}h predictions yet — this model is newer than BTC's.` };
  const accuracy = results.filter(r => (r.p_up >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = results.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / n;
  const upRate = results.filter(r => r.realized_up === 1).length / n;
  const brierAlwaysBaseRate = results.reduce((s, r) => s + (upRate - r.realized_up) ** 2, 0) / n;
  const bestNaiveBrier = Math.min(0.25, brierAlwaysBaseRate);
  const beatsNaiveBaseline = brier < bestNaiveBrier;

  // Same tracker BTC has: only meaningful once enough resolved predictions
  // actually carry an experimental value (predictions logged before this
  // was ported to LINK won't have one).
  const withExperimental = results.filter(r => r.p_up_experimental != null);
  let experimentalComparison = { available: false, note: 'Not enough resolved predictions with the experimental variant logged yet.' };
  if (withExperimental.length >= 20) {
    const nExp = withExperimental.length;
    const accuracyExp = withExperimental.filter(r => (r.p_up_experimental >= 0.5) === (r.realized_up === 1)).length / nExp;
    const brierExp = withExperimental.reduce((s, r) => s + (r.p_up_experimental - r.realized_up) ** 2, 0) / nExp;
    const brierOrigSameSet = withExperimental.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / nExp;
    experimentalComparison = {
      available: true,
      n_resolved: nExp,
      accuracy_experimental: Number(accuracyExp.toFixed(3)),
      brier_experimental: Number(brierExp.toFixed(3)),
      brier_original_same_set: Number(brierOrigSameSet.toFixed(3)),
      experimental_wins: brierExp < brierOrigSameSet,
      note: brierExp < brierOrigSameSet
        ? 'The adaptive-K/weighted variant is currently outperforming the original on LINK too — check whether this matches what BTC showed.'
        : 'The original approach is still doing as well or better on LINK — worth noting if this differs from BTC\'s result.',
    };
  }

  return {
    ok: true, n_resolved: n, accuracy: Number(accuracy.toFixed(3)), brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)), brier_baseline_5050: 0.25,
    brier_baseline_up_rate: Number(brierAlwaysBaseRate.toFixed(3)), beats_naive_baseline: beatsNaiveBaseline,
    experimental_vs_original: experimentalComparison,
    note: n < 20
      ? `Only ${n} resolved LINK predictions — noise at this size, not a verdict yet.`
      : beatsNaiveBaseline
        ? `Beats the best naive baseline (${bestNaiveBrier.toFixed(3)}).`
        : `Does NOT beat the best naive baseline (${bestNaiveBrier.toFixed(3)}) yet.`,
  };
}

// LINK now logs the same adaptive-K/weighted experiment BTC does (ported
// after BTC's own result looked promising, to check whether it's a real
// technique or a BTC-specific fluke) — same expanding-cumulative-Brier
// idea as BTC's version, now with a real second series to compare.
async function getLinkCalibrationHistory(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT resolved_ts, p_up, p_up_experimental, calibrated_p_up, realized_up FROM link_predictions WHERE realized_up IS NOT NULL AND horizon_hours = ? ORDER BY resolved_ts ASC'
  ).bind(horizonHours).all();
  let sumOrig = 0, nOrig = 0, sumExp = 0, nExp = 0, sumCal = 0, nCal = 0;
  let correctOrig = 0, correctExp = 0, correctCal = 0;
  const points = results.map(r => {
    sumOrig += (r.p_up - r.realized_up) ** 2;
    nOrig++;
    if ((r.p_up > 0.5) === (r.realized_up === 1)) correctOrig++;
    if (r.p_up_experimental != null) {
      sumExp += (r.p_up_experimental - r.realized_up) ** 2;
      nExp++;
      if ((r.p_up_experimental > 0.5) === (r.realized_up === 1)) correctExp++;
    }
    if (r.calibrated_p_up != null) {
      sumCal += (r.calibrated_p_up - r.realized_up) ** 2;
      nCal++;
      if ((r.calibrated_p_up > 0.5) === (r.realized_up === 1)) correctCal++;
    }
    return {
      ts: r.resolved_ts,
      brier_original: Number((sumOrig / nOrig).toFixed(4)),
      n_original: nOrig,
      accuracy_original: Number((correctOrig / nOrig).toFixed(3)),
      brier_experimental: nExp > 0 ? Number((sumExp / nExp).toFixed(4)) : null,
      n_experimental: nExp,
      accuracy_experimental: nExp > 0 ? Number((correctExp / nExp).toFixed(3)) : null,
      brier_calibrated: nCal > 0 ? Number((sumCal / nCal).toFixed(4)) : null,
      n_calibrated: nCal,
      accuracy_calibrated: nCal > 0 ? Number((correctCal / nCal).toFixed(3)) : null,
    };
  });
  return { ok: true, points, naive_baseline_5050: 0.25 };
}

async function getLinkChartData(env, horizonHours = 24) {
  const { results: prices } = await env.DB.prepare(
    'SELECT ts, link_price FROM link_data ORDER BY ts ASC'
  ).all();
  const { results: predictions } = await env.DB.prepare(
    'SELECT id, ts, target_ts, link_price_at_prediction, p_up, median_analog_return, realized_up, realized_return FROM link_predictions WHERE horizon_hours = ? ORDER BY ts ASC'
  ).bind(horizonHours).all();
  return { ok: true, prices, predictions };
}

// LINK-specific daily Gemini read — deliberately narrower than the BTC
// comprehensive analysis: LINK's own narrative (oracle infra, CCIP/SWIFT,
// enterprise adoption) plus its technical picture, not a repeat of the same
// macro/geopolitics sections already covered for BTC.
// trigger: 'cron' | 'manual' -- see runGeminiDailyAnalysis's comment for
// the full rationale, identical pattern applied to LINK's own lane.
async function runLinkGeminiAnalysis(env, trigger = 'cron') {
  const consumer = trigger === 'manual' ? 'link_narrative_manual' : 'link_narrative_cron';
  const requestTs = Date.now();
  const correlationId = `GA-${requestTs}-LINK-${trigger}`;
  const model = 'gemini-3.6-flash';

  const reservation = await reserveGeminiQuotaSlot(env, consumer, GEMINI_SHARED_QUOTA_CONFIG[consumer], requestTs);
  if (!reservation.admitted) {
    await recordGeminiProviderCall(env, {
      correlationId, consumer, asset: 'LINK', requestTs, model,
      quotaDecision: reservation.reason === 'daily_limit_reached' ? 'deferred_daily' : 'deferred_hourly',
      httpStatus: null, responseStatus: 'quota_deferred', errorCategory: null,
    }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));
    return { ok: false, status: 'quota_deferred', reason: reservation.reason, correlationId };
  }

  const latest = await env.DB.prepare(
    'SELECT ts, link_price, technical_score FROM link_data ORDER BY ts DESC LIMIT 1'
  ).first();
  const linkPrice = latest?.link_price ?? null;

  const prompt = `You are a crypto analyst writing a short daily briefing specifically on Chainlink (LINK) — not a general crypto market update, focus on what's specific to LINK.

GROUND TRUTH: Current LINK price: ${linkPrice != null ? '$' + linkPrice.toFixed(2) : 'unknown'}. Current simplified technical score (0-100, RSI-style, not a full indicator suite): ${latest?.technical_score ?? 'N/A'}.

RULES: never state a guaranteed outcome as fact. Plain text, no markdown symbols. If your knowledge of very recent LINK-specific news may be incomplete, say so.

Cover, each as its own labeled section on its own line, 2-3 sentences each:
ORACLE ADOPTION: enterprise/institutional integrations, CCIP, bank/SWIFT-related activity, any recent partnership news.
TECHNICAL PICTURE: chart structure, momentum, key levels for LINK specifically.
RISK FACTORS: anything LINK-specific that could weigh on it (competition from other oracle networks, token unlock schedules if known, etc).
SYNTHESIS: 1-2 sentences tying it together.

After all sections, on its own final line, output exactly this (valid JSON, nothing after it):
LINK_JSON: {"bias_short":"bullish|neutral|bearish","bias_medium":"bullish|neutral|bearish","support_pct_below":<number>,"resistance_pct_above":<number>}`;

  const geminiResult = await callGeminiGenerateContent(env, { model, prompt });

  if (!geminiResult.ok) {
    await recordGeminiProviderCall(env, {
      correlationId, consumer, asset: 'LINK', requestTs, model, quotaDecision: 'admitted',
      httpStatus: geminiResult.status, responseStatus: geminiResult.errorCategory, errorCategory: geminiResult.errorCategory,
    }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));
    return { ok: false, status: geminiResult.errorCategory, reason: geminiResult.errorMessage, correlationId };
  }

  let narrative = geminiResult.text.trim();
  let parsed = {};
  const jsonMatch = narrative.match(/LINK_JSON:\s*(\{[\s\S]*\})\s*$/);
  if (jsonMatch) {
    try { parsed = JSON.parse(jsonMatch[1]); } catch (e) {}
    narrative = narrative.slice(0, jsonMatch.index).trim();
  }
  for (const label of ['ORACLE ADOPTION', 'TECHNICAL PICTURE', 'RISK FACTORS', 'SYNTHESIS']) {
    narrative = narrative.replace(new RegExp(`\\s*(${label}:)`, 'gi'), `\n\n$1`);
  }
  narrative = narrative.replace(/^\n+/, '').trim();

  const record = {
    ts: Date.now(), link_price_at_analysis: linkPrice,
    bias_short: normalizeBias(parsed.bias_short), bias_medium: normalizeBias(parsed.bias_medium),
    support_pct_below: Number.isFinite(parsed.support_pct_below) ? parsed.support_pct_below : null,
    resistance_pct_above: Number.isFinite(parsed.resistance_pct_above) ? parsed.resistance_pct_above : null,
    narrative, raw_json: JSON.stringify(parsed),
  };
  await env.DB.prepare(
    `INSERT INTO link_gemini_analysis (ts, link_price_at_analysis, bias_short, bias_medium, support_pct_below, resistance_pct_above, narrative, raw_json) VALUES (?,?,?,?,?,?,?,?)`
  ).bind(record.ts, record.link_price_at_analysis, record.bias_short, record.bias_medium, record.support_pct_below, record.resistance_pct_above, record.narrative, record.raw_json).run();

  await recordGeminiProviderCall(env, {
    correlationId, consumer, asset: 'LINK', requestTs, model, quotaDecision: 'admitted',
    httpStatus: geminiResult.status, responseStatus: 'ok', errorCategory: null,
  }).catch(auditErr => console.error('Failed to write gemini_provider_calls row:', auditErr));

  return { ok: true, status: 'ok', ...record, correlationId };
}

async function getLinkGeminiHistory(env, limit) {
  const { results } = await env.DB.prepare(
    'SELECT id, ts, link_price_at_analysis, bias_short, bias_medium, support_pct_below, resistance_pct_above, narrative FROM link_gemini_analysis ORDER BY ts DESC LIMIT ?'
  ).bind(limit).all();
  return { ok: true, analyses: results };
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const corsHeaders = {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type',
    };
    if (request.method === 'OPTIONS') return new Response(null, { headers: corsHeaders });

    // ---- GET / — health check ----
    if (url.pathname === '/' && request.method === 'GET') {
      return new Response(JSON.stringify({ ok: true, service: 'PulseWorkerV2', ts: Date.now() }), {
        headers: { ...corsHeaders, 'Content-Type': 'application/json' },
      });
    }

    // ---- GET /db-check — confirms the shared D1 binding actually works and
    // reports how much history is available to build the model on ----
    if (url.pathname === '/db-check' && request.method === 'GET') {
      try {
        const row = await env.DB.prepare(
          'SELECT COUNT(*) as cnt, MIN(ts) as min_ts, MAX(ts) as max_ts FROM history'
        ).first();
        return new Response(JSON.stringify({ ok: true, history: row }), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /api/learning/daily — read-only daily audit report. See
    // .ai/DAILY_AUDIT.md. Optional ?date=YYYY-MM-DD scopes to one UTC day
    // (matched against resolved_ts, i.e. "predictions that resolved that
    // day", not "predictions created that day" -- resolution is when an
    // outcome becomes knowable, which is what an audit of results cares
    // about). No params returns all-time. ----
    if (url.pathname === '/api/learning/daily' && request.method === 'GET') {
      try {
        const dateStr = url.searchParams.get('date') || null;
        let report;
        if (dateStr) {
          report = await buildDailyReport(env, { dateStr }); // rare, smaller-scoped historical path -- not cached
        } else {
          const cached = await getCachedDailyReport(env);
          report = cached ? cached.report : await refreshDailyReportCache(env); // cache miss (e.g. first call ever) -- compute once and populate, rather than staying slow forever
        }
        return new Response(JSON.stringify(report), {
          status: report.ok === false ? 400 : 200,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /api/learning/chatgpt — compact projection of the same
    // report, sized for AI analysis. Read-only, no secrets, no raw D1
    // access -- per .ai/ARCHITECTURE.md Security section. ----
    if (url.pathname === '/api/learning/chatgpt' && request.method === 'GET') {
      try {
        const dateStr = url.searchParams.get('date') || null;
        let report;
        if (dateStr) {
          report = await buildDailyReport(env, { dateStr });
        } else {
          const cached = await getCachedDailyReport(env);
          report = cached ? cached.report : await refreshDailyReportCache(env);
        }
        if (report.ok === false) {
          return new Response(JSON.stringify(report), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
        }
        return new Response(JSON.stringify(compactForChatGpt(report)), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /predict — BTC 24h k-NN historical analog prediction. Also
    // backfills any past predictions whose horizon has now passed. Runs
    // automatically every 3h via cron (see scheduled() below) in addition
    // to firing on page visits. ----
    // Selection coverage fix (H1, overnight ground-truth audit): this route
    // and link-predict/eth-predict below previously called predictAndLog
    // alone, bypassing selectBestVariant entirely -- only the scheduled()
    // cron's predictThenSelect wrapper paired them. That left every
    // manually/dashboard-triggered prediction (these are NOT a separate
    // diagnostic path -- this is the exact endpoint the live CryptoPulse
    // frontend's "Refresh" hits, writing into the same predictions table
    // everything else reads) without a selection_decisions row, a real
    // audit-coverage gap (measured: 83 BTC/24h predictions vs. 23 selection
    // decisions in the same window). Fixed by pairing them the same way
    // scheduled() already does -- selection failure is caught and attached
    // to the response, never allowed to fail the prediction response itself.
    if (url.pathname === '/predict' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await predictAndLog(env, horizon);
        try {
          result.selection = await selectBestVariant(env, 'BTC', horizon);
        } catch (selErr) {
          result.selection = { ok: false, error: String(selErr) };
        }
        return new Response(JSON.stringify(result), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /eth-predict — same shape as /predict, scoped to ETH ----
    // ---- GET /select-variant?coin=BTC|LINK|ETH&horizon=12|24 — runs the
    // condition-matched selection layer once, on demand. See
    // selectBestVariant's comment block for the full design. ----
    if (url.pathname === '/select-variant' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        // Read-only -- does NOT call selectBestVariant (that stays reserved
        // for real prediction-triggered compute+write, from /predict et al).
        const result = await getLatestSelection(env, coin, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /selection-history?coin=X&horizon=Y&limit=N — recent
    // selection decisions, for frontend display ----
    if (url.pathname === '/selection-history' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const limit = Math.min(50, parseInt(url.searchParams.get('limit'), 10) || 10);
        const { results } = await env.DB.prepare(
          'SELECT * FROM selection_decisions WHERE coin=? AND horizon_hours=? ORDER BY ts DESC LIMIT ?'
        ).bind(coin, horizon, limit).all();
        return new Response(JSON.stringify({ ok: true, coin, horizon_hours: horizon, decisions: results }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    if (url.pathname === '/eth-predict' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await ethPredictAndLog(env, horizon);
        try {
          result.selection = await selectBestVariant(env, 'ETH', horizon);
        } catch (selErr) {
          result.selection = { ok: false, error: String(selErr) };
        }
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /eth-backfill-history?days=90 — manual trigger to pull real
    // historical depth from Hyperliquid, same source already proven for
    // BTC/LINK. This is the actual first-run step: without it, ETH's model
    // only has whatever's accumulated from the live 3h cron since deploy. ----
    if (url.pathname === '/eth-backfill-history' && request.method === 'GET') {
      try {
        const days = Math.min(365, Math.max(1, parseInt(url.searchParams.get('days'), 10) || 90));
        const daily = await backfillEthHistory(env, days);
        const hourly = await backfillEthHistoryHourly(env, Math.min(days, 20));
        return new Response(JSON.stringify({ ok: true, daily, hourly }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /chart-data — price series + predictions log for the price-vs-prediction chart ----
    if (url.pathname === '/chart-data' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getChartData(env, horizon);
        return new Response(JSON.stringify(result), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /eth-chart-data — same shape as /chart-data, scoped to ETH ----
    if (url.pathname === '/eth-chart-data' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getEthChartData(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /calibration — rolling accuracy/Brier score across every
    // prediction that has actually resolved so far ----
    if (url.pathname === '/calibration' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getCalibration(env, horizon);
        return new Response(JSON.stringify(result), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /eth-calibration — same shape as /calibration, scoped to ETH ----
    if (url.pathname === '/eth-calibration' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getEthCalibration(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /challenger-calibration?coin=BTC|LINK|ETH&horizon=12|24 ----
    if (url.pathname === '/challenger-calibration' && request.method === 'GET') {
      try {
        const coinParam = url.searchParams.get('coin');
        const coin = ['BTC', 'LINK', 'ETH'].includes(coinParam) ? coinParam : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getChallengerCalibration(env, coin, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /variant-calibration?coin=BTC|LINK|ETH&horizon=12|24&variant=...
    // -- backs the Dashboard tile's trust indicator for whichever variant
    // selectBestVariant currently has chosen, in one consistent shape
    // regardless of which of the 6 possible variants it is. See
    // getVariantCalibrationSummary's own comment for the full reasoning. ----
    if (url.pathname === '/variant-calibration' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const variant = url.searchParams.get('variant') || 'original';
        const result = await getVariantCalibrationSummary(env, coin, horizon, variant);
        return new Response(JSON.stringify(result), { status: result.ok === false ? 400 : 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /research/regime-directional?coin=&horizon= -- read-only
    // instrumentation for the regime-anomaly-directional-split audit. No
    // writes, no effect on any production logic. See
    // computeRegimeDirectionalReport's own comment for full context. ----
    if (url.pathname === '/research/regime-directional' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const report = await computeRegimeDirectionalReport(env, coin, horizon);
        return new Response(JSON.stringify(report), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /challenger-recent?limit=20 ----
    if (url.pathname === '/challenger-recent' && request.method === 'GET') {
      try {
        const limit = Math.min(100, parseInt(url.searchParams.get('limit') || '20', 10));
        const result = await getChallengerRecent(env, limit);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /challenger-calibration-history?coin=BTC|LINK|ETH&horizon=12|24 ----
    if (url.pathname === '/challenger-calibration-history' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getChallengerCalibrationHistory(env, coin, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    if (url.pathname === '/calibration-history' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getCalibrationHistory(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /eth-calibration-history — same shape, scoped to ETH ----
    if (url.pathname === '/eth-calibration-history' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getEthCalibrationHistory(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /recalibrate-refresh — manual trigger, same logic the daily cron runs ----
    if (url.pathname === '/recalibrate-refresh' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await refreshCalibrationCurve(env, coin, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /recalibrate-refresh-challenger?coin=X&horizon=Y — manual
    // trigger for Challenger's own calibration curve, same purpose as the
    // core model's version above: force a rebuild now rather than waiting
    // for the next 07:00 UTC daily cron tick.
    if (url.pathname === '/recalibrate-refresh-challenger' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await refreshChallengerCalibrationCurve(env, coin, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /calibration-curve — inspect the current decile mapping ----
    if (url.pathname === '/calibration-curve' && request.method === 'GET') {
      try {
        const coin = ['BTC', 'LINK', 'ETH'].includes(url.searchParams.get('coin')) ? url.searchParams.get('coin') : 'BTC';
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const curve = await getLatestCalibrationCurve(env, coin, horizon);
        return new Response(JSON.stringify({ ok: true, coin, horizon_hours: horizon, curve }), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /analyst-relay/prompt — the current best candidate + prompt
    // to paste into the Gemini app, if one clears the priority bar ----
    if (url.pathname === '/analyst-relay/prompt' && request.method === 'GET') {
      try {
        const result = await getAnalystRelayCandidate(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- POST /analyst-relay/submit — a human-pasted Gemini-app response.
    // Never touches gemini_investigations/gemini_provider_calls -- see the
    // design note above recordAnalystRelay. ----
    if (url.pathname === '/analyst-relay/submit' && request.method === 'POST') {
      try {
        const body = await request.json();
        if (!body || typeof body.rawResponseText !== 'string' || !body.rawResponseText.trim()) {
          return new Response(JSON.stringify({ ok: false, error: 'rawResponseText is required' }), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
        }
        const result = await recordAnalystRelay(env, {
          candidateId: body.candidateId ?? 'unknown',
          assets: Array.isArray(body.assets) ? body.assets : [],
          promptRequestedTs: Number.isFinite(body.promptRequestedTs) ? body.promptRequestedTs : null,
          rawResponseText: body.rawResponseText,
          // Round-tripped from what GET /analyst-relay/prompt returned --
          // records exactly the evidence the human actually saw, rather
          // than a freshly-rebuilt context that could have drifted if D1
          // state changed between fetching the prompt and submitting.
          context: (body.context && typeof body.context === 'object') ? body.context : null,
        });
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    // ---- GET /gemini-analysis — latest N daily analyses (default 10) ----
    if (url.pathname === '/gemini-analysis' && request.method === 'GET') {
      try {
        const limit = Math.min(50, parseInt(url.searchParams.get('limit') || '10', 10));
        const result = await getGeminiAnalysisHistory(env, limit);
        return new Response(JSON.stringify(result), {
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ---- GET /run-analysis — manually trigger the daily Gemini analysis
    // outside its 07:00 UTC schedule. Same pattern as V1's Outlook tab
    // having both a cron and a manual button. ----
    if (url.pathname === '/run-analysis' && request.method === 'GET') {
      try {
        // 'manual' -- covers both the explicit button and the frontend's
        // per-app-boot call. The 07:00 UTC cron passes 'cron' instead (see
        // scheduled()), landing in a separate quota lane so this route can
        // never exhaust the daily analysis's own guaranteed slot.
        const result = await runGeminiDailyAnalysis(env, 'manual');
        return new Response(JSON.stringify(result), {
          status: geminiStatusToHttpCode(result.status),
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, status: 'error', error: String(err) }), {
          status: 500,
          headers: { ...corsHeaders, 'Content-Type': 'application/json' },
        });
      }
    }

    // ==================== LINK routes ====================
    if (url.pathname === '/btc-backfill' && request.method === 'GET') {
      try {
        const days = Math.min(365, parseInt(url.searchParams.get('days') || '90', 10));
        const result = await backfillBtcHistory(env, days);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/btc-backfill-hourly' && request.method === 'GET') {
      try {
        const days = Math.min(30, parseInt(url.searchParams.get('days') || '20', 10));
        const result = await backfillBtcHistoryHourly(env, days);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-technical' && request.method === 'GET') {
      try {
        const years = Math.min(10, Math.max(1, parseFloat(url.searchParams.get('years') || '3')));
        const result = await runBtcOfflineBacktest(env, years);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-feature' && request.method === 'GET') {
      try {
        const feature = url.searchParams.get('feature') || 'regime_mag';
        const result = await runHistoryFeatureBacktest(env, feature);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-combined' && request.method === 'GET') {
      try {
        const result = await runHistoryCombinedBacktest(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-history' && request.method === 'GET') {
      try {
        const result = await getBacktestHistory(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-regime-split' && request.method === 'GET') {
      try {
        const result = await runBtcRegimeSplitBacktest(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/backtest-regime-split-history' && request.method === 'GET') {
      try {
        const result = await getRegimeSplitHistory(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-backfill' && request.method === 'GET') {
      try {
        const days = Math.min(365, parseInt(url.searchParams.get('days') || '90', 10));
        const result = await backfillLinkHistory(env, days);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-backfill-hourly' && request.method === 'GET') {
      try {
        const days = Math.min(30, parseInt(url.searchParams.get('days') || '20', 10));
        const result = await backfillLinkHistoryHourly(env, days);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-predict' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await linkPredictAndLog(env, horizon);
        try {
          result.selection = await selectBestVariant(env, 'LINK', horizon);
        } catch (selErr) {
          result.selection = { ok: false, error: String(selErr) };
        }
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-calibration' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getLinkCalibration(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-calibration-history' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getLinkCalibrationHistory(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-chart-data' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await getLinkChartData(env, horizon);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/link-gemini-analysis' && request.method === 'GET') {
      try {
        const limit = Math.min(50, parseInt(url.searchParams.get('limit') || '10', 10));
        const result = await getLinkGeminiHistory(env, limit);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }
    if (url.pathname === '/run-link-analysis' && request.method === 'GET') {
      try {
        const result = await runLinkGeminiAnalysis(env, 'manual');
        return new Response(JSON.stringify(result), { status: geminiStatusToHttpCode(result.status), headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, status: 'error', error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      }
    }

    return new Response(JSON.stringify({ ok: false, error: 'not_found' }), {
      status: 404,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    });
  },

  // Fires every 3h (see wrangler.toml [triggers]) so predictions get logged
  // and old ones get resolved regardless of whether anyone opens the page.
  // Same predictAndLog() the /predict route uses — one code path either way.
  // Two crons share this handler (see wrangler.toml): every 3h for
  // predict-and-log, once daily for the comprehensive Gemini analysis.
  // event.cron tells us which one fired, same dispatch pattern the
  // original PulseWorker already uses for its own two crons.
  async scheduled(event, env, ctx) {
    if (event.cron === '0 7 * * *') {
      ctx.waitUntil(runGeminiDailyAnalysis(env, 'cron').catch(err => console.error('Daily Gemini analysis failed:', err)));
      ctx.waitUntil(runLinkGeminiAnalysis(env, 'cron').catch(err => console.error('Daily LINK Gemini analysis failed:', err)));
      // Recalibration is cheap and only needs daily freshness — resolved
      // counts move by at most a handful of predictions per day.
      for (const coin of ['BTC', 'LINK', 'ETH']) {
        for (const h of [12, 24]) {
          ctx.waitUntil(refreshCalibrationCurve(env, coin, h).catch(err => console.error(`Calibration refresh ${coin}/${h}h failed:`, err)));
          ctx.waitUntil(refreshChallengerCalibrationCurve(env, coin, h).catch(err => console.error(`Challenger calibration refresh ${coin}/${h}h failed:`, err)));
        }
      }
    } else {
      // Both horizons, both coins, every 3h tick. logBtcData/logLinkData and
      // the backfill steps inside predictAndLog/linkPredictAndLog are
      // horizon-agnostic and safely re-run each call (idempotent — just an
      // extra D1 read at our tiny data scale, not worth avoiding).
      //
      // Each coin/horizon's predict-then-select is sequenced explicitly
      // (await, not two independent waitUntil calls) — selectBestVariant
      // reads the LATEST prediction's features_json, so it must run after
      // that specific prediction exists, not race it. Different coin/
      // horizon pairs still run concurrently with each other, just not with
      // themselves.
      const predictThenSelect = async (predictFn, coin, horizon) => {
        await predictFn(env, horizon, { allowWrite: true });
        await selectBestVariant(env, coin, horizon).catch(err => console.error(`Selection ${coin}/${horizon}h failed:`, err));
      };

      // The six prediction/selection tasks still run concurrently with each
      // other (unchanged). The ordering wrapper that used to sequence a
      // Gemini evaluation after all six had settled was removed along with
      // that evaluation step -- see the comment above the Analyst Relay
      // section for why. Promise.allSettled (not Promise.all) kept
      // deliberately: each task already wraps its own .catch(), so none of
      // these reject, but this stays correct even if a future edit removes
      // one of those inner .catch() calls and a task genuinely rejects.
      const predictionTasks = [
        predictThenSelect(predictAndLog, 'BTC', 24).catch(err => console.error('BTC 24h predict-and-log failed:', err)),
        predictThenSelect(predictAndLog, 'BTC', 12).catch(err => console.error('BTC 12h predict-and-log failed:', err)),
        predictThenSelect(linkPredictAndLog, 'LINK', 24).catch(err => console.error('LINK 24h predict-and-log failed:', err)),
        predictThenSelect(linkPredictAndLog, 'LINK', 12).catch(err => console.error('LINK 12h predict-and-log failed:', err)),
        predictThenSelect(ethPredictAndLog, 'ETH', 24).catch(err => console.error('ETH 24h predict-and-log failed:', err)),
        predictThenSelect(ethPredictAndLog, 'ETH', 12).catch(err => console.error('ETH 12h predict-and-log failed:', err)),
      ];
      ctx.waitUntil(Promise.allSettled(predictionTasks).then(
        () => refreshDailyReportCache(env).catch(err => console.error('Daily report cache refresh failed:', err))
      ));
    }
  },
};
