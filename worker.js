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
function trailingVolatility(sortedRows, endIdx, lookback = 14) {
  const start = Math.max(0, endIdx - lookback + 1);
  const window = sortedRows.slice(start, endIdx + 1);
  if (window.length < 4) return null;
  const rets = [];
  for (let i = 1; i < window.length; i++) {
    rets.push((window[i].btc_price - window[i - 1].btc_price) / window[i - 1].btc_price * 100);
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

const HISTORY_FRESHNESS_MS = 48 * 60 * 60 * 1000; // beyond this, a "history" match is too stale to trust as real, falls back to imputed

async function runPrediction(env, horizonHours = 24) {
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
  const candidates = complete.slice(0, -1); // every complete row except today itself

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

  const nowTs = Date.now();
  const features = Object.fromEntries(FEATURE_KEYS.map(k => [k, today[k]]));

  const insert = await env.DB.prepare(
    `INSERT INTO predictions
     (ts, target_ts, btc_price_at_prediction, p_up, n_analogs, median_analog_return, return_p25, return_p75, features_json,
      k_used, volatility_percentile, closest_analog_dist, is_regime_anomaly, p_up_experimental, median_return_experimental, horizon_hours)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    nowTs, nowTs + lagMs, today.btc_price, pUp, resolved.length, median, p25, p75, JSON.stringify(features),
    kAdaptive, volPercentile, closestDist, isRegimeAnomaly ? 1 : 0, pUpExperimental, medianReturnExperimental, horizonHours
  ).run();

  return {
    ok: true,
    status: 'ok',
    prediction_id: insert.meta.last_row_id,
    ts: nowTs,
    horizon_hours: horizonHours,
    p_up: Number(pUp.toFixed(3)),
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
async function predictAndLog(env, horizonHours = 24) {
  await logBtcData(env);
  const resolvedCount = await backfillPredictions(env);
  const geminiResolvedCount = await backfillGeminiBiasShort(env);
  const result = await runPrediction(env, horizonHours);
  result.backfilled_this_call = resolvedCount;
  result.gemini_bias_backfilled_this_call = geminiResolvedCount;
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

async function runGeminiDailyAnalysis(env) {
  if (!env.GEMINI_API_KEY) throw new Error('GEMINI_API_KEY not configured on this Worker');

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

  const geminiRes = await fetch(
    `https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'x-goog-api-key': env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }),
    }
  );
  if (!geminiRes.ok) {
    const errBody = await geminiRes.text();
    throw new Error(`Gemini API ${geminiRes.status}: ${errBody.slice(0, 300)}`);
  }
  const geminiJson = await geminiRes.json();
  const text = geminiJson?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) throw new Error('Empty or unexpected Gemini response shape');

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

  return { ok: true, ...record };
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

// technical_score + borrowed BTC regime/sentiment only — funding_adj is
// still logged on every LIVE cron snapshot (bonus context, potentially
// useful later) but deliberately not required here: real historical
// candles (used for backfill below) only carry price, no funding rate, and
// requiring it would disqualify every backfilled row from ever being a
// usable analog.
const LINK_FEATURE_KEYS = ['technical_score', 'btc_regime_mag', 'sentiment_score'];
const LINK_MIN_COMPLETE_ROWS = 30;
const LINK_MIN_RESOLVED_ANALOGS = 5;

async function runLinkPrediction(env, horizonHours = 24) {
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
  const candidates = complete.slice(0, -1);

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

  const nowTs = Date.now();
  const features = Object.fromEntries(LINK_FEATURE_KEYS.map(k => [k, today[k]]));

  const insert = await env.DB.prepare(
    'INSERT INTO link_predictions (ts, target_ts, link_price_at_prediction, p_up, n_analogs, median_analog_return, return_p25, return_p75, features_json, horizon_hours) VALUES (?,?,?,?,?,?,?,?,?,?)'
  ).bind(nowTs, nowTs + lagMs, today.link_price, pUp, resolved.length, median, p25, p75, JSON.stringify(features), horizonHours).run();

  const nImputedInNeighbors = neighbors.filter(n => n.row.context_imputed).length;

  return {
    ok: true, status: 'ok', prediction_id: insert.meta.last_row_id, ts: nowTs, horizon_hours: horizonHours,
    p_up: Number(pUp.toFixed(3)), n_analogs: resolved.length,
    median_analog_return_pct: Number(median.toFixed(2)),
    return_range_pct: [Number(p25.toFixed(2)), Number(p75.toFixed(2))],
    link_price_now: today.link_price, features,
    top_analogs: resolved.slice(0, 5).map(r => ({ date: new Date(r.analog_ts).toISOString().slice(0, 10), return_pct: Number(r.return_pct.toFixed(2)) })),
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

async function linkPredictAndLog(env, horizonHours = 24) {
  await logLinkData(env);
  const resolvedCount = await backfillLinkPredictions(env);
  const result = await runLinkPrediction(env, horizonHours);
  result.backfilled_this_call = resolvedCount;
  return result;
}

async function getLinkCalibration(env, horizonHours = 24) {
  const { results } = await env.DB.prepare(
    'SELECT p_up, realized_up FROM link_predictions WHERE realized_up IS NOT NULL AND horizon_hours = ?'
  ).bind(horizonHours).all();
  const n = results.length;
  if (n === 0) return { ok: true, n_resolved: 0, note: `No resolved LINK ${horizonHours}h predictions yet — this model is newer than BTC's.` };
  const accuracy = results.filter(r => (r.p_up >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = results.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / n;
  const upRate = results.filter(r => r.realized_up === 1).length / n;
  const brierAlwaysBaseRate = results.reduce((s, r) => s + (upRate - r.realized_up) ** 2, 0) / n;
  const bestNaiveBrier = Math.min(0.25, brierAlwaysBaseRate);
  const beatsNaiveBaseline = brier < bestNaiveBrier;
  return {
    ok: true, n_resolved: n, accuracy: Number(accuracy.toFixed(3)), brier_score: Number(brier.toFixed(3)),
    historical_up_rate: Number(upRate.toFixed(3)), brier_baseline_5050: 0.25,
    brier_baseline_up_rate: Number(brierAlwaysBaseRate.toFixed(3)), beats_naive_baseline: beatsNaiveBaseline,
    note: n < 20
      ? `Only ${n} resolved LINK predictions — noise at this size, not a verdict yet.`
      : beatsNaiveBaseline
        ? `Beats the best naive baseline (${bestNaiveBrier.toFixed(3)}).`
        : `Does NOT beat the best naive baseline (${bestNaiveBrier.toFixed(3)}) yet.`,
  };
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
async function runLinkGeminiAnalysis(env) {
  if (!env.GEMINI_API_KEY) throw new Error('GEMINI_API_KEY not configured on this Worker');
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

  const geminiRes = await fetch(
    'https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent',
    { method: 'POST', headers: { 'Content-Type': 'application/json', 'x-goog-api-key': env.GEMINI_API_KEY },
      body: JSON.stringify({ contents: [{ parts: [{ text: prompt }] }] }) }
  );
  if (!geminiRes.ok) throw new Error(`Gemini API ${geminiRes.status}: ${(await geminiRes.text()).slice(0, 300)}`);
  const geminiJson = await geminiRes.json();
  const text = geminiJson?.candidates?.[0]?.content?.parts?.[0]?.text;
  if (!text) throw new Error('Empty or unexpected Gemini response shape');

  let narrative = text.trim();
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

  return { ok: true, ...record };
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

    // ---- GET /predict — BTC 24h k-NN historical analog prediction. Also
    // backfills any past predictions whose horizon has now passed. Runs
    // automatically every 3h via cron (see scheduled() below) in addition
    // to firing on page visits. ----
    if (url.pathname === '/predict' && request.method === 'GET') {
      try {
        const horizon = [12, 24].includes(parseInt(url.searchParams.get('horizon'), 10)) ? parseInt(url.searchParams.get('horizon'), 10) : 24;
        const result = await predictAndLog(env, horizon);
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
        const result = await runGeminiDailyAnalysis(env);
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
        const result = await runLinkGeminiAnalysis(env);
        return new Response(JSON.stringify(result), { headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
      } catch (err) {
        return new Response(JSON.stringify({ ok: false, error: String(err) }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } });
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
      ctx.waitUntil(runGeminiDailyAnalysis(env).catch(err => console.error('Daily Gemini analysis failed:', err)));
      ctx.waitUntil(runLinkGeminiAnalysis(env).catch(err => console.error('Daily LINK Gemini analysis failed:', err)));
    } else {
      // Both horizons, both coins, every 3h tick. logBtcData/logLinkData and
      // the backfill steps inside predictAndLog/linkPredictAndLog are
      // horizon-agnostic and safely re-run each call (idempotent — just an
      // extra D1 read at our tiny data scale, not worth avoiding).
      ctx.waitUntil(predictAndLog(env, 24).catch(err => console.error('BTC 24h predict-and-log failed:', err)));
      ctx.waitUntil(predictAndLog(env, 12).catch(err => console.error('BTC 12h predict-and-log failed:', err)));
      ctx.waitUntil(linkPredictAndLog(env, 24).catch(err => console.error('LINK 24h predict-and-log failed:', err)));
      ctx.waitUntil(linkPredictAndLog(env, 12).catch(err => console.error('LINK 12h predict-and-log failed:', err)));
    }
  },
};
