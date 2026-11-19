// GLM evidence intelligence: read-only adapters (contract §1–§3).
// readGlmInputs() issues a fixed set of bounded, indexed SELECTs in ONE D1 batch (one subrequest). Nothing here writes,
// and nothing touches V1 tables except SELECT. The normalizers turn rows into `glm-input-1` items: they never invent a
// value, label every time with its basis, and mark interpretations (Gemini or other models) as such with derived_from.
import { GLM_LIMITS, GLM_FAMILY_OF_CLASS } from './contract.js';

const H = 3600e3;
const DAY = 24 * H;

// Every query: indexed or primary-key access, explicit LIMIT, parameters only (no string interpolation of values).
// `rowsBound` is the worst-case rows read, used for the per-run budget check (contract §12).
export const GLM_READ_QUERIES = Object.freeze([
  { key: 'v1_composite', rowsBound: 24, sql:
    'SELECT slot_ts, computed_ts, version, weights_version, composite, completeness, below_floor, n_ok, n_missing, provenance_json ' +
    'FROM server_composite_readings WHERE slot_ts > ? AND slot_ts <= ? ORDER BY slot_ts DESC LIMIT 24',
    args: (c) => [c - DAY, c] },
  { key: 'v1_source_obs', rowsBound: 41, sql:
    'SELECT slot_ts, source_id, status, score, observed_at, fetched_at, collector_version FROM server_source_observations ' +
    'WHERE slot_ts = (SELECT MAX(slot_ts) FROM server_source_observations WHERE slot_ts <= ?) LIMIT 40',
    args: (c) => [c] },
  { key: 'v1_history', rowsBound: 12, sql:
    'SELECT id, ts, score, technical_score, regime_mag, bottom_score, gold_regime, sources_json FROM history ' +
    'WHERE ts > ? AND ts <= ? ORDER BY ts DESC LIMIT 12',
    args: (c) => [c - DAY, c] },
  { key: 'v1_technical', rowsBound: 2, sql:
    'SELECT id, ts, evaluation, score FROM technical_eval WHERE ts <= ? ORDER BY ts DESC LIMIT 2',
    args: (c) => [c] },
  { key: 'v1_risk_regime', rowsBound: 1, sql:
    'SELECT ts, version, status, score, regime, generated_ts FROM risk_regime_readings WHERE ts <= ? ORDER BY ts DESC LIMIT 1',
    args: (c) => [c] },
  { key: 'v1_foufi', rowsBound: 10, sql:
    'SELECT video_id, published_ts, title, url, transcript_status, summary_json, overall_lean, fetched_ts, video_type, special_edition ' +
    'FROM foufi_digest ORDER BY rowid DESC LIMIT 10',
    args: () => [] },
  { key: 'v1_catalysts', rowsBound: 20, sql:
    'SELECT id, ts, coin, price_move_pct, headline_matched, headline_source, extracted_reason, verdict, category, direction, ' +
    'source_url, discovery_timestamp, confidence, first_public_timestamp, investigation_id, source_grounded, timestamp_source ' +
    'FROM coin_catalyst_log ORDER BY rowid DESC LIMIT 20',
    args: () => [] },
  { key: 'v2_gemini_daily', rowsBound: 14, sql:
    'SELECT id, ts, bias_short, bias_medium, bias_long, narrative, bias_short_correct, resolved_ts FROM gemini_daily_analysis ' +
    'ORDER BY rowid DESC LIMIT 14',
    args: () => [] },
  { key: 'collector', rowsBound: 60, sql:
    'SELECT evidence_id, source_id, family, canonical_url, title, summary, published_ts, retrieved_ts, title_hash, content_hash, dup_of ' +
    'FROM sv2_evidence WHERE retrieved_ts > ? AND retrieved_ts <= ? ORDER BY retrieved_ts DESC LIMIT 60',
    args: (c) => [c - GLM_LIMITS.evidenceLookbackMs, c] },
  { key: 'events', rowsBound: 20, sql:
    'SELECT event_id, event_type, asset, source, source_url, source_ts, detected_ts, direction, severity, explanation, status, expires_ts ' +
    'FROM event_ledger WHERE detected_ts > ? AND detected_ts <= ? ORDER BY detected_ts DESC LIMIT 20',
    args: (c) => [c - GLM_LIMITS.evidenceLookbackMs, c] },
  { key: 'market', rowsBound: 64, sql:
    'SELECT ts, btc_price FROM btc_data WHERE ts > ? AND ts <= ? ORDER BY ts ASC LIMIT 64',
    args: (c) => [c - 2 * DAY, c] },
  { key: 'v2_track', rowsBound: 30, sql:
    'SELECT id, ts, target_ts, p_up, calibrated_p_up, realized_up, resolved_ts FROM predictions ' +
    'WHERE horizon_hours = 24 AND ts <= ? ORDER BY ts DESC LIMIT 30',
    args: (c) => [c] },
  // NOT INDEXED: a reverse rowid scan that stops at the first match. With the (asset, calc_version, decision_ts) index the
  // planner sorted every BTC row in a temp B-tree (production EXPLAIN 2026-11-19), which grows without bound.
  // result_id order = insertion order = decision order (runSentimentV2Cycle inserts pending readings in time order).
  { key: 'v2_sentiment', rowsBound: 10, sql:
    'SELECT result_id, asset, decision_ts, calc_version, status, sentiment_score, sentiment_class, confidence, evidence_coverage, created_ts ' +
    'FROM sentiment_v2_results NOT INDEXED WHERE asset = ? AND decision_ts <= ? ORDER BY result_id DESC LIMIT 1',
    args: (c) => ['BTC', c] },
  { key: 'timesfm', rowsBound: 11, sql:
    'SELECT id, coin, horizon_hours, ts, input_end_ts, target_ts, price_at_prediction, forecast_price, predicted_return_pct, direction, ' +
    'model_version, actual_direction, correct, resolved_ts FROM experiment_4_timesfm ' +
    'WHERE coin = ? AND horizon_hours = ? AND ts <= ? ORDER BY ts DESC LIMIT 11',
    args: (c) => ['BTC', 24, c] },
]);

export const GLM_READ_ROWS_BOUND = GLM_READ_QUERIES.reduce((s, q) => s + q.rowsBound, 0);

// One batch, one subrequest. A failing table (e.g. not yet created) degrades only that input: D1 batches are
// all-or-nothing, so on a batch error each query is retried alone ONCE to isolate the failure (still bounded).
export async function readGlmInputs(db, cutoffTs, queries = GLM_READ_QUERIES) {
  const out = { rows: {}, errors: {}, rowsRead: 0, calls: 0 };
  const stmts = queries.map((q) => db.prepare(q.sql).bind(...q.args(cutoffTs)));
  try {
    out.calls += 1;
    const res = await db.batch(stmts);
    queries.forEach((q, i) => {
      const r = res[i] || {};
      out.rows[q.key] = r.results || [];
      out.rowsRead += Number(r.meta?.rows_read ?? out.rows[q.key].length) || 0;
    });
    return out;
  } catch (e) {
    for (let i = 0; i < queries.length; i++) {
      const q = queries[i];
      try {
        out.calls += 1;
        const r = await stmts[i].all();
        out.rows[q.key] = r.results || [];
        out.rowsRead += Number(r.meta?.rows_read ?? out.rows[q.key].length) || 0;
      } catch (err) {
        out.rows[q.key] = [];
        out.errors[q.key] = String(err?.message || err).slice(0, 160);
      }
    }
    return out;
  }
}

// ---------- normalization helpers ----------

export function validTs(x) {
  const n = Number(x);
  return Number.isFinite(n) && n >= GLM_LIMITS.minTs && n <= GLM_LIMITS.maxTs ? n : null;
}

const CONTROL_CHARS = new RegExp('[\\u0000-\\u001f\\u007f-\\u009f\\u200b-\\u200f\\u2028-\\u202e\\u2066-\\u2069]', 'g');

export function sanitizeText(s, max = GLM_LIMITS.maxTextChars) {
  if (s == null) return null;
  let t = String(s)
    .replace(/<[^>]*>/g, ' ')                       // markup
    .replace(CONTROL_CHARS, ' ')                    // control / bidi
    .replace(/https?:\/\/\S+/gi, (u) => { try { return '[link:' + new URL(u).host + ']'; } catch { return '[link]'; } })
    .replace(/\s+/g, ' ')
    .trim();
  if (t.length > max) t = t.slice(0, max - 1) + '…';
  return t || null;
}

function host(u) { try { return new URL(String(u)).host.toLowerCase(); } catch { return null; } }

// Canonical URL used for the independence key: lower-case host, no fragment, no tracking params, no trailing slash.
export function canonicalUrl(u) {
  try {
    const x = new URL(String(u));
    x.hash = '';
    for (const k of [...x.searchParams.keys()]) if (/^(utm_|fbclid|gclid|mc_|ref$|source$)/i.test(k)) x.searchParams.delete(k);
    x.host = x.host.toLowerCase();
    x.protocol = 'https:';
    let s = x.toString();
    if (s.endsWith('/') && x.pathname !== '/') s = s.slice(0, -1);
    return s;
  } catch { return null; }
}

function safeJson(s) { if (s == null) return null; try { return JSON.parse(s); } catch { return undefined; } }

// times: every known time, the first-available time and the basis it was taken from (contract §5).
function times(t, basisOrder) {
  const out = { event_ts: null, published_ts: null, retrieved_ts: null, created_ts: null, input_cutoff_ts: null,
    target_ts: null, resolved_ts: null, first_available_ts: null, first_available_basis: null, uncertain: [] };
  for (const k of Object.keys(t)) {
    if (t[k] == null) continue;
    const v = validTs(t[k]);
    if (v == null) out.uncertain.push(k + ':invalid');
    else out[k] = v;
  }
  for (const k of basisOrder) {
    if (out[k] != null) { out.first_available_ts = out[k]; out.first_available_basis = k; break; }
  }
  if (out.published_ts != null && out.retrieved_ts != null && out.published_ts > out.retrieved_ts) out.uncertain.push('published_after_retrieved');
  return out;
}

function item(ref, cls, o) {
  return {
    ref, provenance_class: cls, family: o.family || GLM_FAMILY_OF_CLASS[cls],
    origin: o.origin, table: o.table, record_id: o.record_id == null ? null : String(o.record_id),
    asset_scope: o.asset_scope || ['BTC'], independence_key: o.independence_key, derived_from: o.derived_from || [],
    times: o.times, content: o.content,
  };
}

// ---------- per-source normalizers (rows -> items) ----------

export function normV1Composite(rows) {
  return rows.map((r) => {
    const prov = safeJson(r.provenance_json);
    const sources = Array.isArray(prov?.sources)
      ? prov.sources.slice(0, 20).map((s) => ({ id: String(s.id), score: Number.isFinite(Number(s.score)) ? Number(s.score) : null }))
      : null;
    return item(`v1c:${r.slot_ts}`, 'v1_aggregate', {
      origin: 'sentiment-ff75', table: 'server_composite_readings', record_id: r.slot_ts, independence_key: 'v1agg',
      asset_scope: ['*'],
      times: times({ created_ts: r.computed_ts, event_ts: r.slot_ts }, ['created_ts']),
      content: { kind: 'v1_server_composite', version: r.version ?? null, composite: r.composite ?? null,
        completeness: r.completeness ?? null, below_floor: r.below_floor ?? null, n_ok: r.n_ok ?? null,
        n_missing: r.n_missing ?? null, sources, provenance_parse: prov === undefined ? 'malformed' : (prov ? 'ok' : 'missing') },
    });
  });
}

// observed_at semantics are per source in V1 (publisher time when the API gives one, else fetch time). It is kept as
// observed_ts with that caveat, never relabelled as a publication time.
export function normV1SourceObs(rows) {
  return rows.filter((r) => r.status === 'ok' && r.score != null).map((r) => item(`v1s:${r.slot_ts}:${r.source_id}`, 'v1_aggregate', {
    origin: 'sentiment-ff75', table: 'server_source_observations', record_id: `${r.slot_ts}:${r.source_id}`,
    independence_key: `v1src:${r.source_id}`, asset_scope: ['*'],
    times: times({ retrieved_ts: r.fetched_at, event_ts: r.observed_at }, ['retrieved_ts']),
    content: { kind: 'v1_source_score', source_id: String(r.source_id), score: Number(r.score),
      observed_ts_semantics: 'publisher_time_if_given_else_fetch_time' },
  }));
}

export function normV1History(rows) {
  return rows.map((r) => {
    const src = safeJson(r.sources_json);
    return item(`v1h:${r.id}`, 'v1_aggregate', {
      origin: 'sentiment-ff75', table: 'history', record_id: r.id, independence_key: 'v1agg', asset_scope: ['*'],
      times: times({ created_ts: r.ts }, ['created_ts']),
      content: { kind: 'v1_composite_reading', score: r.score ?? null, technical_score: r.technical_score ?? null,
        regime_mag: r.regime_mag ?? null, bottom_score: r.bottom_score ?? null, gold_regime: r.gold_regime ?? null,
        source_scores: src && typeof src === 'object' ? Object.fromEntries(Object.entries(src).slice(0, 20)) : null,
        sources_parse: src === undefined ? 'malformed' : (src ? 'ok' : 'missing') },
    });
  });
}

// Llama-3.1-8B technical read: a V1 model interpretation of price, so its family is `market`, not news.
export function normV1Technical(rows) {
  return rows.map((r) => item(`v1t:${r.id}`, 'v1_ai_interpretation', {
    family: 'market', origin: 'sentiment-ff75', table: 'technical_eval', record_id: r.id, independence_key: 'v1tech',
    times: times({ created_ts: r.ts }, ['created_ts']),
    content: { kind: 'v1_technical_read', model: '@cf/meta/llama-3.1-8b-instruct-fast', score: r.score ?? null,
      text: sanitizeText(r.evaluation) },
  }));
}

export function normV1RiskRegime(rows) {
  return rows.map((r) => item(`v1r:${r.ts}`, 'v1_aggregate', {
    origin: 'sentiment-ff75', table: 'risk_regime_readings', record_id: r.ts, independence_key: 'v1agg', asset_scope: ['*'],
    times: times({ created_ts: r.generated_ts ?? r.ts }, ['created_ts']),
    content: { kind: 'v1_risk_regime', version: r.version ?? null, status: r.status ?? null, score: r.score ?? null, regime: r.regime ?? null },
  }));
}

// Foufi: the Gemini summary is an interpretation; the video itself is the original source (metadata only: there is no
// stored transcript, so none is claimed). Both share one independence key: one confirmation, not two.
export function normV1Foufi(rows, cutoffTs, { keep = 3 } = {}) {
  const out = [];
  const usable = rows
    .filter((r) => validTs(r.published_ts) != null && r.summary_json)
    .sort((a, b) => b.published_ts - a.published_ts)
    .slice(0, keep);
  for (const r of usable) {
    const key = `src:youtube:${r.video_id}`;
    const orig = `src:youtube:${r.video_id}`;
    out.push(item(orig, 'original_source', {
      origin: 'youtube', table: 'foufi_digest', record_id: r.video_id, independence_key: key, asset_scope: ['*'],
      times: times({ published_ts: r.published_ts, retrieved_ts: r.fetched_ts }, ['published_ts']),
      content: { kind: 'video', host: host(r.url), title: sanitizeText(r.title, 160), video_type: r.video_type ?? null,
        transcript_available: false },
    }));
    const s = safeJson(r.summary_json);
    const btc = s && s.coins && s.coins.BTC ? s.coins.BTC : null;
    out.push(item(`gem:foufi:${r.video_id}`, 'gemini_interpretation', {
      origin: 'sentiment-ff75', table: 'foufi_digest', record_id: r.video_id, independence_key: key, derived_from: [orig],
      asset_scope: ['*'],
      // A summary is available when V1 fetched (generated) it, never earlier than the video itself.
      times: times({ published_ts: r.published_ts, retrieved_ts: r.fetched_ts }, ['retrieved_ts']),
      content: s ? { kind: 'gemini_video_summary', model: 'gemini-3.6-flash', overall_lean: r.overall_lean ?? null,
        market_summary: sanitizeText(s.market_summary),
        macro: s.macro ? { lean: s.macro.lean ?? null, text: sanitizeText(s.macro.text, 200) } : null,
        micro: s.micro ? { lean: s.micro.lean ?? null, text: sanitizeText(s.micro.text, 200) } : null,
        tradfi: s.tradfi ? { lean: s.tradfi.lean ?? null, text: sanitizeText(s.tradfi.text, 200) } : null,
        btc: btc ? { score: Number.isFinite(Number(btc.score)) && btc.score !== null ? Number(btc.score) : null, note: sanitizeText(btc.note, 200) } : null,
        dominant_driver: s.dominant_driver ?? null, major_catalyst: s.major_catalyst?.name ? sanitizeText(s.major_catalyst.name, 120) : null }
        : { kind: 'gemini_video_summary', parse: 'malformed' },
    }));
  }
  return out;
}

export function normV1Catalysts(rows, cutoffTs) {
  const out = [];
  for (const r of rows) {
    const ts = validTs(r.ts);
    if (ts == null || ts < cutoffTs - 7 * DAY) continue;
    if (r.coin && !['BTC', '*'].includes(String(r.coin).toUpperCase())) continue;
    const cu = canonicalUrl(r.source_url);
    const key = cu ? `url:${cu}` : `catalyst:${r.id}`;
    const derived = [];
    if (cu) {
      const oref = `src:url:${r.id}`;
      derived.push(oref);
      out.push(item(oref, 'original_source', {
        origin: host(cu) || 'unknown', table: 'coin_catalyst_log', record_id: r.id, independence_key: key,
        times: times({ published_ts: r.first_public_timestamp, retrieved_ts: r.discovery_timestamp ?? r.ts }, ['retrieved_ts']),
        content: { kind: 'headline', host: host(cu), headline: sanitizeText(r.headline_matched, 200), source: r.headline_source ?? null },
      }));
    }
    const gemini = r.investigation_id != null && String(r.investigation_id) !== '';
    out.push(item(`cat:${r.id}`, gemini ? 'gemini_interpretation' : 'v1_ai_interpretation', {
      family: 'news', origin: gemini ? 'pulseworker-v2 (historical gemini investigation)' : 'sentiment-ff75', table: 'coin_catalyst_log',
      record_id: r.id, independence_key: key, derived_from: derived,
      times: times({ created_ts: r.ts, published_ts: r.first_public_timestamp }, ['created_ts']),
      content: { kind: 'catalyst_classification', model: gemini ? 'gemini' : '@cf/meta/llama-3.1-8b-instruct-fast',
        price_move_pct: r.price_move_pct ?? null, verdict: r.verdict ?? null, category: r.category ?? null,
        direction: r.direction ?? null, confidence: r.confidence ?? null, reason: sanitizeText(r.extracted_reason, 200),
        source_grounded: r.source_grounded ?? null },
    }));
  }
  return out;
}

// Frozen V2 Gemini daily analyses: track record only (counts computed in code); never a live input after 24 h.
export function summarizeGeminiDaily(rows, cutoffTs) {
  const resolved = rows.filter((r) => validTs(r.resolved_ts) != null && r.resolved_ts <= cutoffTs && r.bias_short_correct != null);
  const latest = rows.map((r) => validTs(r.ts)).filter((x) => x != null && x <= cutoffTs).sort((a, b) => b - a)[0] ?? null;
  return { kind: 'gemini_daily_track_record', model: 'gemini (historical, V2, cron off since 2026-11-17)',
    n_resolved: resolved.length, n_correct: resolved.filter((r) => Number(r.bias_short_correct) === 1).length,
    latest_ts: latest };
}

export function normCollector(rows) {
  return rows.map((r) => {
    const cu = canonicalUrl(r.canonical_url);
    return item(`ev:${r.evidence_id}`, 'collector_evidence', {
      origin: String(r.source_id), table: 'sv2_evidence', record_id: r.evidence_id,
      independence_key: cu ? `url:${cu}` : `ev:${r.evidence_id}`, asset_scope: ['*'],
      times: times({ published_ts: r.published_ts, retrieved_ts: r.retrieved_ts }, ['retrieved_ts']),
      content: { kind: 'evidence', source_id: String(r.source_id), source_family: r.family ?? null, host: host(r.canonical_url),
        title: sanitizeText(r.title, 200), summary: sanitizeText(r.summary), title_hash: r.title_hash ?? null,
        content_hash: r.content_hash ?? null, dup_of: r.dup_of ?? null },
    });
  });
}

export function normEvents(rows) {
  return rows.filter((r) => !r.asset || ['BTC', '*', 'ALL', 'CRYPTO'].includes(String(r.asset).toUpperCase())).map((r) => {
    const cu = canonicalUrl(r.source_url);
    return item(`evt:${r.event_id}`, 'collector_evidence', {
      origin: String(r.source || 'event_ledger'), table: 'event_ledger', record_id: r.event_id,
      independence_key: cu ? `url:${cu}` : `evt:${r.event_id}`,
      times: times({ event_ts: r.source_ts, retrieved_ts: r.detected_ts }, ['retrieved_ts']),
      content: { kind: 'detected_event', event_type: r.event_type ?? null, direction: r.direction ?? null,
        severity: r.severity ?? null, status: r.status ?? null, explanation: sanitizeText(r.explanation, 200) },
    });
  });
}

// Market: one summary item computed from btc_data at or before the cutoff. Returns null when there is no price.
export function normMarket(rows, cutoffTs) {
  const pts = rows.map((r) => ({ ts: validTs(r.ts), p: Number(r.btc_price) }))
    .filter((x) => x.ts != null && x.ts <= cutoffTs && Number.isFinite(x.p) && x.p > 0).sort((a, b) => a.ts - b.ts);
  if (!pts.length) return null;
  const last = pts[pts.length - 1];
  const at = (ms) => { let best = null; for (const x of pts) if (x.ts <= last.ts - ms) best = x; return best; };
  const ret = (ms) => { const a = at(ms); return a ? Math.round((last.p / a.p - 1) * 1e4) / 100 : null; };
  const lr = [];
  for (let i = 1; i < pts.length; i++) if (pts[i].ts - pts[i - 1].ts > 60e3) lr.push(Math.log(pts[i].p / pts[i - 1].p));
  const sd = lr.length > 2 ? Math.sqrt(lr.reduce((s, x) => s + x * x, 0) / lr.length) : null;
  return item(`mkt:btc:${last.ts}`, 'market_data', {
    origin: 'pulseworker-v2', table: 'btc_data', record_id: last.ts, independence_key: 'market:btc',
    times: times({ created_ts: last.ts }, ['created_ts']),
    content: { kind: 'btc_market_summary', price: last.p, ret_6h_pct: ret(6 * H), ret_24h_pct: ret(DAY), ret_48h_pct: ret(2 * DAY),
      tick_return_sd_pct: sd == null ? null : Math.round(sd * 1e4) / 100, n_points: pts.length,
      high_48h: Math.max(...pts.map((x) => x.p)), low_48h: Math.min(...pts.map((x) => x.p)) },
  });
}

// V2 24 h track record: only outcomes resolved by the cutoff (a later resolution is look-ahead).
export function normV2Track(rows, cutoffTs) {
  const resolved = rows.filter((r) => validTs(r.resolved_ts) != null && r.resolved_ts <= cutoffTs && r.realized_up != null);
  if (!resolved.length) return null;
  const hits = resolved.filter((r) => (Number(r.p_up) >= 0.5) === (Number(r.realized_up) === 1)).length;
  return item(`v2t:24:${cutoffTs}`, 'v2_prediction', {
    origin: 'pulseworker-v2', table: 'predictions', record_id: resolved.map((r) => r.id).join(','), independence_key: 'v2',
    times: times({ created_ts: Math.max(...resolved.map((r) => r.resolved_ts)) }, ['created_ts']),
    content: { kind: 'v2_track_record', horizon_hours: 24, n_resolved: resolved.length, n_direction_correct: hits },
  });
}

// V2 baseline as served (the displayed headline), from the prediction snapshot body: cycle_p_up for a non-original
// chosen variant, else p_up (owner decision 2026-11-19; mirrors the frontend's headlineOf).
export function headlineBaseline(body) {
  if (!body || typeof body !== 'object') return null;
  const sel = body.selection;
  // Exact mirror of CryptoPulseV2 headlineOf(): non-original -> cycle_p_up when it is a number, else chosen_p_up.
  if (sel && sel.ok && sel.chosen_variant && sel.chosen_variant !== 'original') {
    if (typeof sel.cycle_p_up === 'number' && Number.isFinite(sel.cycle_p_up)) {
      return { baseline_p_up: sel.cycle_p_up, baseline_source: 'cycle_p_up', chosen_variant: sel.chosen_variant };
    }
    if (typeof sel.chosen_p_up === 'number' && Number.isFinite(sel.chosen_p_up)) {
      return { baseline_p_up: sel.chosen_p_up, baseline_source: 'chosen_p_up', chosen_variant: sel.chosen_variant };
    }
    return null;
  }
  if (Number.isFinite(Number(body.p_up))) return { baseline_p_up: Number(body.p_up), baseline_source: 'p_up', chosen_variant: sel?.chosen_variant ?? null };
  return null;
}

export function normV2Sentiment(rows) {
  return rows.map((r) => item(`v2s:${r.result_id}`, 'v2_sentiment', {
    origin: 'pulseworker-v2', table: 'sentiment_v2_results', record_id: r.result_id, independence_key: 'v2',
    times: times({ created_ts: r.created_ts ?? r.decision_ts, event_ts: r.decision_ts }, ['created_ts']),
    content: { kind: 'v2_sentiment', calc_version: r.calc_version ?? null, status: r.status ?? null,
      score: r.sentiment_score ?? null, class: r.sentiment_class ?? null, confidence: r.confidence ?? null,
      evidence_coverage: r.evidence_coverage ?? null },
  }));
}

// TimesFM: a forecast is available at its row creation time (`ts`), never at its input cutoff. Resolved outcomes are
// admissible only if resolved by the decision cutoff.
export function normTimesfm(rows, cutoffTs) {
  const items = [];
  const sorted = rows.filter((r) => validTs(r.ts) != null && r.ts <= cutoffTs).sort((a, b) => b.ts - a.ts);
  const latest = sorted[0];
  if (latest) {
    items.push(item(`tfm:${latest.id}`, 'forecast_timesfm', {
      origin: 'github-actions exp004-timesfm', table: 'experiment_4_timesfm', record_id: latest.id, independence_key: 'model:timesfm',
      times: times({ created_ts: latest.ts, input_cutoff_ts: latest.input_end_ts, target_ts: latest.target_ts }, ['created_ts']),
      content: { kind: 'timesfm_forecast', model_version: latest.model_version ?? null, horizon_hours: latest.horizon_hours,
        price_at_prediction: latest.price_at_prediction ?? null, forecast_price: latest.forecast_price ?? null,
        predicted_return_pct: latest.predicted_return_pct ?? null, direction: latest.direction ?? null,
        uncertainty: 'not_supplied (point forecast; quantiles not stored)' },
    }));
  }
  const res = sorted.slice(1).filter((r) => validTs(r.resolved_ts) != null && r.resolved_ts <= cutoffTs && r.correct != null);
  return { items, latest: latest || null, track: { n_resolved: res.length, n_correct: res.filter((r) => Number(r.correct) === 1).length } };
}

// Chronos-2: parse the derived ledger (format exp008-chronos-ledger-v1); never imputes a missing pair.
export function normChronos(ledger, cutoffTs, horizon = 24) {
  if (!ledger || ledger.format !== 'exp008-chronos-ledger-v1' || !Array.isArray(ledger.forecasts)) {
    return { items: [], latest: null, track: { n_resolved: 0, n_correct: 0 }, error: 'ledger_unavailable_or_unknown_format' };
  }
  const fs = ledger.forecasts.filter((f) => f && f.coin === 'BTC' && Number(f.horizon_hours) === horizon && validTs(f.issued_ts) != null && f.issued_ts <= cutoffTs)
    .sort((a, b) => b.issued_ts - a.issued_ts);
  const latest = fs[0] || null;
  const items = [];
  if (latest) {
    items.push(item(`chr:${latest.id}`, 'forecast_chronos', {
      origin: 'github-actions exp008-chronos', table: 'research-data/exp008-chronos', record_id: latest.id, independence_key: 'model:chronos',
      times: times({ created_ts: latest.issued_ts, input_cutoff_ts: latest.input_end_ts, target_ts: latest.target_ts }, ['created_ts']),
      content: { kind: 'chronos_forecast', model_id: latest.model_id ?? null, model_revision: latest.model_revision_resolved ?? latest.model_revision ?? null,
        horizon_hours: horizon, price_at_prediction: latest.price_at_prediction ?? null, forecast_median: latest.forecast_median ?? null,
        forecast_q10: latest.forecast_q10 ?? null, forecast_q90: latest.forecast_q90 ?? null,
        predicted_return_pct: latest.predicted_return_pct ?? null, direction: latest.direction ?? null, timesfm_ts: latest.timesfm_ts ?? null },
    }));
  }
  const res = fs.slice(1).filter((f) => validTs(f.resolved_ts) != null && f.resolved_ts <= cutoffTs && f.correct != null);
  return { items, latest, track: { n_resolved: res.length, n_correct: res.filter((f) => f.correct === true || Number(f.correct) === 1).length } };
}

// Bounded Chronos ledger fetch (1 subrequest, size + time capped, no retry). Returns { ledger, error }.
export const GLM_CHRONOS_LEDGER_URL =
  'https://raw.githubusercontent.com/quiquandon-oss/PulseWorkerV2/research-data/exp008-chronos/exp008_chronos/derived/ledger.json';

export async function fetchChronosLedger(fetchImpl = fetch, { url = GLM_CHRONOS_LEDGER_URL, maxBytes = GLM_LIMITS.chronosMaxBytes, timeoutMs = GLM_LIMITS.chronosTimeoutMs } = {}) {
  const ctl = typeof AbortController !== 'undefined' ? new AbortController() : null;
  const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
  try {
    const r = await fetchImpl(url, { signal: ctl?.signal, headers: { accept: 'application/json' } });
    if (!r.ok) return { ledger: null, error: `http_${r.status}` };
    const len = Number(r.headers?.get?.('content-length'));
    if (Number.isFinite(len) && len > maxBytes) return { ledger: null, error: 'too_large' };
    const text = await r.text();
    if (text.length > maxBytes) return { ledger: null, error: 'too_large' };
    try { return { ledger: JSON.parse(text), error: null }; } catch { return { ledger: null, error: 'malformed_json' }; }
  } catch (e) {
    return { ledger: null, error: e?.name === 'AbortError' ? 'timeout' : 'fetch_failed' };
  } finally {
    if (timer) clearTimeout(timer);
  }
}
