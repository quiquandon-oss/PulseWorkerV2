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

// Nearest row to targetTs within TOL_MS, used both for "what happened 24h
// after this analog" and later for backfilling a prediction's real outcome.
function nearestRow(history, targetTs) {
  let best = null, bestDiff = Infinity;
  for (const r of history) {
    const diff = Math.abs(r.ts - targetTs);
    if (diff <= TOL_MS && diff < bestDiff) { bestDiff = diff; best = r; }
  }
  return best;
}

async function runPrediction(env) {
  const { results: history } = await env.DB.prepare(
    'SELECT ts, score, btc_price, technical_score, regime_mag, bottom_score FROM history WHERE btc_price IS NOT NULL ORDER BY ts ASC'
  ).all();

  const complete = history.filter(r => FEATURE_KEYS.every(k => r[k] !== null && r[k] !== undefined));
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
      const fwd = nearestRow(history, n.row.ts + LAG_MS);
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
  const median = returns[Math.floor(returns.length / 2)];

  const nowTs = Date.now();
  const features = Object.fromEntries(FEATURE_KEYS.map(k => [k, today[k]]));

  const insert = await env.DB.prepare(
    'INSERT INTO predictions (ts, target_ts, btc_price_at_prediction, p_up, n_analogs, median_analog_return, features_json) VALUES (?,?,?,?,?,?,?)'
  ).bind(nowTs, nowTs + LAG_MS, today.btc_price, pUp, resolved.length, median, JSON.stringify(features)).run();

  return {
    ok: true,
    status: 'ok',
    prediction_id: insert.meta.last_row_id,
    ts: nowTs,
    horizon_hours: 24,
    p_up: Number(pUp.toFixed(3)),
    n_analogs: resolved.length,
    median_analog_return_pct: Number(median.toFixed(2)),
    btc_price_now: today.btc_price,
    features,
    top_analogs: resolved.slice(0, 5).map(r => ({ date: new Date(r.analog_ts).toISOString().slice(0, 10), return_pct: Number(r.return_pct.toFixed(2)) })),
    note: `Based on the ${resolved.length} most similar days in ${candidates.length} days of history. Small sample — read as a rough lean, not a forecast.`,
  };
}

// Fills in what actually happened for any prediction whose 24h horizon has
// passed but hasn't been resolved yet. This is the calibration loop — the
// part that actually lets the model be checked against reality over time.
async function backfillPredictions(env) {
  const { results: history } = await env.DB.prepare(
    'SELECT ts, btc_price FROM history WHERE btc_price IS NOT NULL ORDER BY ts ASC'
  ).all();
  const { results: unresolved } = await env.DB.prepare(
    'SELECT id, target_ts, btc_price_at_prediction FROM predictions WHERE realized_up IS NULL AND target_ts <= ?'
  ).bind(Date.now()).all();

  let resolvedCount = 0;
  for (const p of unresolved) {
    const match = nearestRow(history, p.target_ts);
    if (!match) continue;
    const ret = (match.btc_price - p.btc_price_at_prediction) / p.btc_price_at_prediction * 100;
    await env.DB.prepare(
      'UPDATE predictions SET realized_btc_price=?, realized_return=?, realized_up=?, resolved_ts=? WHERE id=?'
    ).bind(match.btc_price, ret, ret > 0 ? 1 : 0, Date.now(), p.id).run();
    resolvedCount++;
  }
  return resolvedCount;
}

async function getCalibration(env) {
  const { results } = await env.DB.prepare(
    'SELECT p_up, realized_up FROM predictions WHERE realized_up IS NOT NULL'
  ).all();
  const n = results.length;
  if (n === 0) return { ok: true, n_resolved: 0, note: 'No resolved predictions yet — the first ones need 24h to mature.' };

  const accuracy = results.filter(r => (r.p_up >= 0.5) === (r.realized_up === 1)).length / n;
  const brier = results.reduce((s, r) => s + (r.p_up - r.realized_up) ** 2, 0) / n;

  return {
    ok: true,
    n_resolved: n,
    accuracy: Number(accuracy.toFixed(3)),
    brier_score: Number(brier.toFixed(3)),
    note: n < 20
      ? 'Fewer than 20 resolved predictions — these numbers will be noisy for a while yet.'
      : 'Brier score: 0 is perfect, 0.25 is what guessing 50/50 every time gives you.',
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
async function predictAndLog(env) {
  const resolvedCount = await backfillPredictions(env);
  const result = await runPrediction(env);
  result.backfilled_this_call = resolvedCount;
  return result;
}

// ---- Chart data: BTC price series + the full predictions log in one call,
// so the frontend can filter to 1D/1W/1M/ALL client-side without refetching
// on every range-tab click. ----
async function getChartData(env) {
  const { results: prices } = await env.DB.prepare(
    'SELECT ts, btc_price FROM history WHERE btc_price IS NOT NULL ORDER BY ts ASC'
  ).all();
  const { results: predictions } = await env.DB.prepare(
    'SELECT id, ts, target_ts, btc_price_at_prediction, p_up, median_analog_return, realized_up, realized_return FROM predictions ORDER BY ts ASC'
  ).all();
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
  // the deterministic engine already computes.
  const latest = await env.DB.prepare(
    'SELECT ts, btc_price, score, technical_score, regime_mag, bottom_score FROM history WHERE btc_price IS NOT NULL ORDER BY ts DESC LIMIT 1'
  ).first();
  const btcPrice = latest?.btc_price ?? null;

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
ANALYSIS_JSON: {"bias_short":"bullish|neutral|bearish","bias_medium":"bullish|neutral|bearish","bias_long":"bullish|neutral|bearish","support_pct_below":<number, % below current price>,"resistance_pct_above":<number, % above current price>,"macro_risk":"low|medium|high","geopolitical_risk":"low|medium|high"}`;

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
    narrative,
    raw_json: JSON.stringify(parsed),
  };

  await env.DB.prepare(
    `INSERT INTO gemini_daily_analysis
     (ts, btc_price_at_analysis, bias_short, bias_medium, bias_long, support_pct_below, resistance_pct_above, macro_risk, geopolitical_risk, narrative, raw_json)
     VALUES (?,?,?,?,?,?,?,?,?,?,?)`
  ).bind(
    record.ts, record.btc_price_at_analysis, record.bias_short, record.bias_medium, record.bias_long,
    record.support_pct_below, record.resistance_pct_above, record.macro_risk, record.geopolitical_risk,
    record.narrative, record.raw_json
  ).run();

  return { ok: true, ...record };
}

async function getGeminiAnalysisHistory(env, limit) {
  const { results } = await env.DB.prepare(
    'SELECT id, ts, btc_price_at_analysis, bias_short, bias_medium, bias_long, support_pct_below, resistance_pct_above, macro_risk, geopolitical_risk, narrative FROM gemini_daily_analysis ORDER BY ts DESC LIMIT ?'
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
    // automatically every 6h via cron (see scheduled() below) in addition
    // to firing on page visits. ----
    if (url.pathname === '/predict' && request.method === 'GET') {
      try {
        const result = await predictAndLog(env);
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
        const result = await getChartData(env);
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
        const result = await getCalibration(env);
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

    return new Response(JSON.stringify({ ok: false, error: 'not_found' }), {
      status: 404,
      headers: { ...corsHeaders, 'Content-Type': 'application/json' },
    });
  },

  // Fires every 6h (see wrangler.toml [triggers]) so predictions get logged
  // and old ones get resolved regardless of whether anyone opens the page.
  // Same predictAndLog() the /predict route uses — one code path either way.
  // Two crons share this handler (see wrangler.toml): every 6h for
  // predict-and-log, once daily for the comprehensive Gemini analysis.
  // event.cron tells us which one fired, same dispatch pattern the
  // original PulseWorker already uses for its own two crons.
  async scheduled(event, env, ctx) {
    if (event.cron === '0 7 * * *') {
      ctx.waitUntil(runGeminiDailyAnalysis(env).catch(err => console.error('Daily Gemini analysis failed:', err)));
    } else {
      ctx.waitUntil(predictAndLog(env));
    }
  },
};
