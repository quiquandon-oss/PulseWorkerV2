// Learning loop -- pure core (no D1, no network, no clock except where passed in).
//
// Research Lab's product loop is UNDERSTAND -> INVESTIGATE -> DISCOVER -> LEARN -> ADJUST -> RECALCULATE -> VALIDATE
// -> APPROVE -> UPDATE. This module holds the deterministic pieces of the first slice:
//   describeMarketEvent()      plain-language "what happened"
//   assessV1Sources()          "do V1's existing sources explain it?"
//   buildResearchCase()        "what don't we understand, what should we research?"
//   buildResearchPack()        compact prompt the human pastes into ChatGPT / Claude / Gemini / Grok
//   parseAiResearchResponse()  pasted AI answer -> structured, UNTRUSTED finding draft (never executed, never trusted)
//   findingToStage7Registration()  confirmed finding -> the existing Stage 7 response-registration payload
//
// Nothing here changes V1, its weights or any prediction. V1_METHODOLOGY_V1 is a read-only snapshot used to describe
// and (in a later slice) recalculate V1 in a sandbox.

// V1's published default source configuration: CryptoPulse index.html COMPOSITE_SOURCES_DEFAULTS at the commit the
// staging collector also pins (0a1dfb8ce88883336ee2e712a84e49be857b1724). V1's composite is
// round(sum(score * weight * confidence) / sum(weight * confidence)) over the sources that resolved.
// Known gap (disclosed in the UI): the browser that writes each V1 score may apply its own localStorage weight
// overrides and a temporary x1.5 Foufi "driver boost", neither of which is persisted -- so a stored V1 score is not
// always exactly reproducible from its sources_json. Comparisons therefore always recompute both sides with the same
// methodology version.
export const V1_METHODOLOGY_V1 = Object.freeze({
  version: 'v1-defaults@0a1dfb8c',
  sources: Object.freeze([
    { id: 'fng', label: 'Fear & Greed Index', weight: 14, confidence: 1.0, group: 'BREADTH' },
    { id: 'funding', label: 'Funding rate (BTC/ETH perps)', weight: 15, confidence: 1.0, group: 'DERIVATIVES' },
    { id: 'longshort', label: 'Long/short positioning proxy', weight: 10, confidence: 0.5, group: 'DERIVATIVES' },
    { id: 'global', label: 'Total crypto market cap (24h)', weight: 6, confidence: 1.0, group: 'BREADTH' },
    { id: 'cryptonews', label: 'Crypto news (CoinTelegraph)', weight: 15, confidence: 0.7, group: 'NEWS' },
    { id: 'macrogeo', label: 'Macro economy news', weight: 8, confidence: 0.7, group: 'MACRO' },
    { id: 'geopolitics', label: 'Geopolitics news', weight: 10, confidence: 0.6, group: 'NEWS' },
    { id: 'regulatory', label: 'Crypto regulation news', weight: 12, confidence: 0.6, group: 'NEWS' },
    { id: 'sosovalue', label: 'Macro news (SoSoValue)', weight: 15, confidence: 0.7, group: 'NEWS' },
    { id: 'onchain', label: 'On-chain activity (BTC)', weight: 8, confidence: 0.5, group: 'ONCHAIN' },
    { id: 'oil', label: 'Brent oil', weight: 6, confidence: 0.6, group: 'MACRO' },
    { id: 'yield10y', label: '10Y Treasury yield', weight: 5, confidence: 0.6, group: 'MACRO' },
    { id: 'usd', label: 'US dollar strength', weight: 8, confidence: 0.6, group: 'MACRO' },
    { id: 'nasdaq', label: 'Nasdaq', weight: 6, confidence: 0.7, group: 'EQUITIES' },
    { id: 'sp500', label: 'S&P 500', weight: 6, confidence: 0.7, group: 'EQUITIES' },
    { id: 'ninemag', label: 'Big-tech / AI basket', weight: 8, confidence: 0.6, group: 'EQUITIES' },
    { id: 'foufi', label: 'Foufi daily (analyst)', weight: 6, confidence: 0.4, group: 'NEWS' },
    { id: 'etfflows', label: 'BTC ETF flows', weight: 10, confidence: 0.8, group: 'FLOWS' },
    { id: 'hypefunding', label: 'HYPE perp funding', weight: 4, confidence: 0.4, group: 'DERIVATIVES' },
    { id: 'gold', label: 'Gold', weight: 5, confidence: 0.5, group: 'MACRO' },
    { id: 'strc', label: 'STRC (Strategy treasury health)', weight: 5, confidence: 0.35, group: 'TREASURY' },
  ]),
});

export const SOURCE_GROUP_LABELS = Object.freeze({
  FLOWS: 'ETF flows', DERIVATIVES: 'Funding & positioning', MACRO: 'Macro', EQUITIES: 'Equities',
  NEWS: 'News & narrative', BREADTH: 'Market breadth', ONCHAIN: 'On-chain', TREASURY: 'Corporate treasuries',
});

// Market drivers V1 has NO source for at all. These are the default "what might be missing" research directions.
export const UNCOVERED_DRIVERS = Object.freeze([
  { key: 'OPTIONS', label: 'Options positioning (open interest, max pain, gamma, expiries)' },
  { key: 'LIQUIDATIONS', label: 'Leverage liquidations / liquidation cascades' },
  { key: 'STABLECOIN_FLOWS', label: 'Stablecoin supply and exchange inflows/outflows' },
  { key: 'WHALES_MINERS', label: 'Whale, miner and long-term-holder flows' },
  { key: 'SPECIFIC_CATALYST', label: 'A specific catalyst (hack, listing, policy decision, company announcement)' },
]);

export const DRIVER_CATEGORIES = Object.freeze([
  ...UNCOVERED_DRIVERS.map((d) => d.key), 'FLOWS', 'DERIVATIVES', 'MACRO', 'EQUITIES', 'NEWS', 'BREADTH', 'ONCHAIN',
  'TREASURY', 'OTHER',
]);
export const FINDING_TYPES = Object.freeze(['NEW_SOURCE', 'NEW_TREND', 'NEW_SIGNAL', 'EXISTING_SOURCE_MISREAD', 'NO_NEW_DRIVER']);
export const CONFIDENCE_LEVELS = Object.freeze(['LOW', 'MEDIUM', 'HIGH']);
export const SENTIMENT_ASSESSMENTS = Object.freeze(['POSITIVE', 'NEGATIVE', 'MIXED', 'INDETERMINATE']);

// Display heuristics (disclosed in Advanced): a source "leans" up at >= 55, down at <= 45; "moved" if it changed by
// at least 5 points over the same window; BTC "moved" if it changed by at least 1%. V1 observations are written by the
// V1 web app only while it is open (~15/day, multi-hour night gaps), so the nearest observation up to 8h earlier is used
// and its own timestamp is returned for display.
export const ASSESSMENT_RULES = Object.freeze({
  version: 'source-assessment-v1', leanUp: 55, leanDown: 45, minSourceDelta: 5, minBtcMovePct: 1,
  windowMs: 24 * 3600000, maxLookupGapMs: 8 * 3600000, explainedShare: 0.5, partialShare: 0.25,
});

const HOUR = 3600000;

export function v1Composite(sourceScores, methodology = V1_METHODOLOGY_V1) {
  let total = 0, acc = 0, used = 0;
  for (const s of methodology.sources) {
    const v = sourceScores ? sourceScores[s.id] : undefined;
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    const w = s.weight * (s.confidence ?? 1);
    total += w; acc += v * w; used++;
  }
  return used ? { score: Math.round(acc / total), exact: acc / total, sources_used: used } : null;
}

// Nearest row at or before ts within maxGapMs. rows must be sorted ascending by tsKey.
export function nearestAtOrBefore(rows, ts, tsKey = 'ts', maxGapMs = ASSESSMENT_RULES.maxLookupGapMs) {
  let lo = 0, hi = rows.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (rows[mid][tsKey] <= ts) { best = mid; lo = mid + 1; } else hi = mid - 1;
  }
  if (best < 0) return null;
  return ts - rows[best][tsKey] <= maxGapMs ? rows[best] : null;
}

function directionOfPct(pct, minPct) {
  if (typeof pct !== 'number' || !Number.isFinite(pct)) return null;
  if (pct >= minPct) return 'UP';
  if (pct <= -minPct) return 'DOWN';
  return 'FLAT';
}
function leanOfScore(score, rules = ASSESSMENT_RULES) {
  if (typeof score !== 'number') return null;
  if (score >= rules.leanUp) return 'UP';
  if (score <= rules.leanDown) return 'DOWN';
  return 'NEUTRAL';
}
function opposite(dir) { return dir === 'UP' ? 'DOWN' : dir === 'DOWN' ? 'UP' : null; }
function fmtPct(p) { return (p > 0 ? '+' : '') + p.toFixed(1) + '%'; }
function fmtDay(ts) { return new Date(ts).toISOString().slice(0, 16).replace('T', ' ') + ' UTC'; }

// btc: { atEvent, before } prices (before = ~24h earlier). Returns the user-facing "what happened" for one event.
export function describeMarketEvent(event, btc) {
  // A LARGE_MOVE's own trigger value is the authoritative 24h move (the detector's window); recomputing it from
  // btc_data, which has gaps, can disagree with the headline. Other categories use btc_data prices.
  const detectorMove = event.category === 'LARGE_MOVE' && typeof event.intensity === 'number' && (event.direction === 'UP' || event.direction === 'DOWN')
    ? (event.direction === 'DOWN' ? -1 : 1) * event.intensity : null;
  const priceMove = btc && btc.atEvent && btc.before ? ((btc.atEvent - btc.before) / btc.before) * 100 : null;
  const movePct = detectorMove !== null ? detectorMove : priceMove;
  const when = fmtDay(event.event_ts);
  let headline;
  switch (event.category) {
    case 'LARGE_MOVE':
      headline = `BTC ${event.direction === 'DOWN' ? 'fell' : 'rose'} ${Math.abs(event.intensity || movePct || 0).toFixed(1)}% in 24 hours (to ${when}).`;
      break;
    case 'REGIME_REVERSAL': {
      const [from, to] = String(event.direction || '').split('_to_');
      headline = from && to
        ? `BTC's 7-day trend flipped from ${from} to ${to === 'rally' ? 'rallying' : to === 'selloff' ? 'selling off' : to} around ${when}.`
        : `BTC's 7-day trend changed character around ${when}.`;
      break;
    }
    case 'VOLATILITY_EXPANSION':
      headline = `BTC became unusually volatile: 7-day volatility reached ${Number(event.intensity || 0).toFixed(2)}x its normal level around ${when}.`;
      break;
    case 'V2_FAILURE_CLUSTER': {
      const horizon = String(event.direction || '').replace('BTC_', '');
      headline = `Our prediction model got ${Math.round(event.intensity || 5)} BTC ${horizon} calls wrong in a row, ending ${when}.`;
      break;
    }
    default:
      headline = `${String(event.category || 'Market event').replace(/_/g, ' ').toLowerCase()} around ${when}.`;
  }
  return {
    event_id: event.event_id,
    category: event.category,
    event_ts: event.event_ts,
    headline,
    btc_move_24h_pct: movePct === null ? null : Math.round(movePct * 100) / 100,
    btc_move_text: movePct === null ? 'BTC price around this time is not available.'
      : detectorMove !== null ? `BTC moved ${fmtPct(movePct)} over the 24 hours the event detector measured.`
        : `BTC moved ${fmtPct(movePct)} over the 24 hours to this event (${Math.round(btc.before).toLocaleString('en-US')} -> ${Math.round(btc.atEvent).toLocaleString('en-US')} USD).`,
  };
}

// before/at: {score, sources} V1 observations ~24h before and at the event. btcMovePct: realized BTC % over the window.
export function assessV1Sources({ before, at, btcMovePct }, methodology = V1_METHODOLOGY_V1, rules = ASSESSMENT_RULES) {
  const actual = directionOfPct(btcMovePct, rules.minBtcMovePct);
  const v1Lean = before ? leanOfScore(before.score, rules) : null;
  const totalWeight = methodology.sources.reduce((s, src) => s + src.weight * (src.confidence ?? 1), 0);
  const rows = methodology.sources.map((src) => {
    const scoreAt = at && at.sources ? at.sources[src.id] : undefined;
    const scoreBefore = before && before.sources ? before.sources[src.id] : undefined;
    const share = (src.weight * (src.confidence ?? 1)) / totalWeight;
    const base = { id: src.id, label: src.label, group: src.group, weight_share: Math.round(share * 1000) / 10, score_before: scoreBefore ?? null, score_at: scoreAt ?? null };
    if (typeof scoreAt !== 'number') return { ...base, verdict: 'MISSING', reason: 'No reading from this source at the time.' };
    const lean = leanOfScore(scoreAt, rules);
    const delta = typeof scoreBefore === 'number' ? scoreAt - scoreBefore : null;
    const trend = delta === null ? 'UNKNOWN' : delta >= rules.minSourceDelta ? 'UP' : delta <= -rules.minSourceDelta ? 'DOWN' : 'FLAT';
    base.delta = delta;
    if (!actual || actual === 'FLAT') return { ...base, verdict: 'NOT_APPLICABLE', reason: 'BTC did not make a clear directional move, so there is nothing directional to explain.' };
    if (lean === actual && trend !== opposite(actual)) return { ...base, verdict: 'EXPLAINS', reason: `Read ${scoreAt} (${actual === 'UP' ? 'bullish' : 'bearish'})${delta !== null ? `, ${delta >= 0 ? '+' : ''}${delta} over 24h` : ''}.` };
    if ((lean === 'NEUTRAL' && trend === actual) || (lean === actual && trend === opposite(actual))) return { ...base, verdict: 'PARTIAL', reason: `Mixed: read ${scoreAt}${delta !== null ? `, ${delta >= 0 ? '+' : ''}${delta} over 24h` : ''}.` };
    if (lean === opposite(actual) && trend !== actual) return { ...base, verdict: 'CONTRADICTS', reason: `Pointed the other way: read ${scoreAt} (${lean === 'UP' ? 'bullish' : 'bearish'}).` };
    return { ...base, verdict: 'SILENT', reason: `Neutral (${scoreAt}) and did not move.` };
  });
  const sum = (v) => rows.filter((r) => r.verdict === v).reduce((s, r) => s + r.weight_share, 0) / 100;
  const explainedShare = sum('EXPLAINS') + 0.5 * sum('PARTIAL');
  let verdict;
  if (!at) verdict = 'NO_V1_DATA';
  else if (!actual) verdict = 'NO_PRICE_DATA';
  else if (actual === 'FLAT') verdict = 'NO_DIRECTIONAL_MOVE';
  else if (explainedShare >= rules.explainedShare) verdict = 'EXPLAINED';
  else if (explainedShare >= rules.partialShare) verdict = 'PARTIALLY_EXPLAINED';
  else verdict = 'NOT_EXPLAINED';
  // A group's status is the verdict carrying the most V1 weight inside that group (ties go to the less flattering
  // verdict), so one small source cannot make a whole group look like it "explains" the move.
  const ORDER = ['CONTRADICTS', 'MISSING', 'SILENT', 'NOT_APPLICABLE', 'PARTIAL', 'EXPLAINS'];
  const groups = {};
  for (const r of rows) {
    const g = groups[r.group] || (groups[r.group] = { group: r.group, label: SOURCE_GROUP_LABELS[r.group] || r.group, weight: {}, sources: [] });
    g.weight[r.verdict] = (g.weight[r.verdict] || 0) + r.weight_share;
    g.sources.push(r.id);
  }
  for (const g of Object.values(groups)) {
    g.status = ORDER.reduce((best, v) => ((g.weight[v] || 0) > (g.weight[best] || 0) ? v : best), ORDER[0]);
    if (g.status === 'NOT_APPLICABLE') g.status = 'SILENT';
  }
  return {
    rules_version: rules.version,
    methodology_version: methodology.version,
    actual_direction: actual,
    v1_score_before: before ? before.score : null,
    v1_before_ts: before ? before.ts ?? null : null,
    v1_at_ts: at ? at.ts ?? null : null,
    v1_score_at: at ? at.score : null,
    v1_lean_before: v1Lean,
    v1_called_it: v1Lean && actual && actual !== 'FLAT' ? v1Lean === actual : null,
    explained_share: Math.round(explainedShare * 1000) / 10,
    verdict,
    sources: rows,
    groups: Object.values(groups),
  };
}

export const VERDICT_TEXT = Object.freeze({
  EXPLAINED: 'Mostly explained by current V1 sources',
  PARTIALLY_EXPLAINED: 'Only partially explained by current V1 sources',
  NOT_EXPLAINED: 'Not explained by current V1 sources',
  NO_DIRECTIONAL_MOVE: 'No clear BTC move to explain',
  NO_PRICE_DATA: 'BTC price not available for this event',
  NO_V1_DATA: 'No V1 sentiment recorded around this event',
});

// Research Case: the "why don't we understand it" object. Persisted (when a human opens it) as a
// stage7_research_requests row -- see learning-api.js openResearchCase().
export function buildResearchCase(described, assessment, evidence) {
  const explaining = assessment.sources.filter((s) => s.verdict === 'EXPLAINS').map((s) => s.label);
  const contradicting = assessment.sources.filter((s) => s.verdict === 'CONTRADICTS').map((s) => s.label);
  const missingGroups = assessment.groups.filter((g) => g.status === 'MISSING' || g.status === 'SILENT' || g.status === 'CONTRADICTS').map((g) => g.label);
  const moveWord = assessment.actual_direction === 'UP' ? 'rise' : assessment.actual_direction === 'DOWN' ? 'fall' : 'move';
  const question = assessment.verdict === 'EXPLAINED'
    ? `V1's sources mostly explain this BTC ${moveWord}. Is there an additional driver V1 is not measuring that made it larger or earlier than V1 suggested?`
    : `What market driver explains this BTC ${moveWord} that CryptoPulse's current V1 sentiment sources do not capture?`;
  const reasons = [];
  if (assessment.verdict === 'NO_V1_DATA') reasons.push('No V1 sentiment reading exists around this event, so V1 cannot be assessed for it.');
  else if (assessment.verdict !== 'EXPLAINED') reasons.push(`Only ${assessment.explained_share}% of V1's source weight pointed in the direction BTC actually moved.`);
  if (assessment.v1_called_it === false) {
    reasons.push(assessment.v1_lean_before === 'NEUTRAL'
      ? `V1 was neutral (${assessment.v1_score_before}) before the move, so it gave no warning that BTC would ${moveWord}.`
      : `V1 leaned ${assessment.v1_lean_before} (${assessment.v1_score_before}) before the move, but BTC went ${assessment.actual_direction}.`);
  }
  if (contradicting.length) reasons.push(`Sources pointing the other way: ${contradicting.join(', ')}.`);
  if (!evidence.length) reasons.push('No news evidence was collected around this event.');
  return {
    event_id: described.event_id,
    question,
    reasons,
    explained_by: explaining,
    contradicted_by: contradicting,
    weak_or_missing_areas: missingGroups,
    uncovered_drivers: UNCOVERED_DRIVERS.map((d) => d.label),
    evidence_count: evidence.length,
    sufficiency_status: contradicting.length >= 3 ? 'CONFLICTING' : evidence.length ? 'INSUFFICIENT' : 'INSUFFICIENT_EVIDENCE',
  };
}

const FINDING_JSON_TEMPLATE = `{
  "explanation": "2-4 sentences: what actually drove the move",
  "primary_driver": "the single most important driver, in a few words",
  "driver_category": "${DRIVER_CATEGORIES.join(' | ')}",
  "finding_type": "${FINDING_TYPES.join(' | ')}",
  "covered_by_existing_v1_source": "one of the V1 source ids listed above, or \\"none\\"",
  "proposed_new_source": { "name": "", "url": "", "what_it_measures": "", "update_frequency": "", "free_or_paid": "" },
  "proposed_signal": "how that source would be turned into a 0-100 bullish/bearish reading",
  "trend": "is this a one-off or an ongoing trend? since when?",
  "evidence": [ { "claim": "", "url": "", "publisher": "", "date": "YYYY-MM-DD" } ],
  "alternative_explanations": [ "" ],
  "limitations": "what you could not verify",
  "confidence": "LOW | MEDIUM | HIGH",
  "sentiment_assessment": "POSITIVE | NEGATIVE | MIXED | INDETERMINATE"
}`;

// The compact text the human pastes into an external AI. Plain text, no secrets, no internal URLs.
export function buildResearchPack(described, assessment, researchCase, evidence) {
  const lines = [];
  lines.push('You are helping improve a crypto market-sentiment model ("V1") that scores BTC sentiment 0-100 (50 = neutral) from 21 sources.');
  lines.push('');
  lines.push('EVENT');
  lines.push(`- ${described.headline}`);
  lines.push(`- ${described.btc_move_text}`);
  lines.push(`- Event time: ${new Date(described.event_ts).toISOString()}`);
  if (assessment.v1_score_before !== null) lines.push(`- V1 sentiment 24h before: ${assessment.v1_score_before}/100 (${assessment.v1_lean_before}); at the event: ${assessment.v1_score_at ?? 'n/a'}/100.`);
  lines.push(`- Our verdict: ${VERDICT_TEXT[assessment.verdict]} (${assessment.explained_share}% of V1 source weight pointed the right way).`);
  lines.push('');
  lines.push('V1 SOURCES (id: reading 24h before -> at event, verdict)');
  for (const s of assessment.sources) {
    lines.push(`- ${s.id} [${SOURCE_GROUP_LABELS[s.group]}]: ${s.score_before ?? 'n/a'} -> ${s.score_at ?? 'n/a'}, ${s.verdict}`);
  }
  lines.push('');
  lines.push('DRIVERS V1 DOES NOT MEASURE AT ALL');
  for (const d of UNCOVERED_DRIVERS) lines.push(`- ${d.label}`);
  if (evidence.length) {
    lines.push('');
    lines.push(`NEWS HEADLINES WE COLLECTED AROUND THE EVENT (${evidence.length} total, first ${Math.min(evidence.length, 12)})`);
    for (const e of evidence.slice(0, 12)) lines.push(`- [${(e.evidence_relation || '').replace('_', '-').toLowerCase()}] ${e.publisher}: ${String(e.headline).slice(0, 160)}`);
  }
  lines.push('');
  lines.push('QUESTION');
  lines.push(researchCase.question);
  lines.push('');
  lines.push('Research the real market context around this date. Cite real, checkable sources with URLs and dates. Do not invent sources or numbers; say so when unsure. If a measurable data source would have captured this driver, name it precisely.');
  lines.push('');
  lines.push('END YOUR ANSWER WITH EXACTLY ONE JSON BLOCK in this format (keep the keys; use "" or [] when unknown):');
  lines.push('```json');
  lines.push(FINDING_JSON_TEMPLATE);
  lines.push('```');
  return lines.join('\n');
}

// ---- Pasted AI answer -> structured finding draft. The text is UNTRUSTED: it is only parsed as JSON data, every
// field is type-checked, enum-whitelisted, length-capped; URLs must be http(s). Nothing is executed or followed. ----
const MAX_STR = 2000;
function str(v, max = MAX_STR) { return typeof v === 'string' ? v.trim().slice(0, max) : ''; }
function oneOf(v, allowed, fallback) {
  const u = typeof v === 'string' ? v.trim().toUpperCase().replace(/[\s-]+/g, '_') : '';
  return allowed.includes(u) ? u : fallback;
}
function httpUrl(v) {
  const s = str(v, 500);
  return /^https?:\/\/[^\s"'<>]+$/i.test(s) ? s : '';
}
function extractJsonCandidate(text) {
  const fences = [...text.matchAll(/```(?:json)?\s*([\s\S]*?)```/gi)].map((m) => m[1]);
  for (let i = fences.length - 1; i >= 0; i--) if (fences[i].trim().startsWith('{')) return fences[i];
  const start = text.indexOf('{');
  const end = text.lastIndexOf('}');
  return start >= 0 && end > start ? text.slice(start, end + 1) : null;
}

export function parseAiResearchResponse(rawText, v1SourceIds = V1_METHODOLOGY_V1.sources.map((s) => s.id)) {
  const text = typeof rawText === 'string' ? rawText.slice(0, 60000) : '';
  if (!text.trim()) return { ok: false, error: 'Paste the AI answer first.', finding: null, warnings: [] };
  const warnings = [];
  let obj = null;
  const candidate = extractJsonCandidate(text);
  if (candidate) {
    try { obj = JSON.parse(candidate); } catch (_e) {
      try { obj = JSON.parse(candidate.replace(/,\s*([}\]])/g, '$1')); warnings.push('Fixed trailing commas in the JSON block.'); } catch (_e2) { obj = null; }
    }
  }
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) {
    return {
      ok: false,
      error: 'No valid JSON block found in the answer. Ask the AI: "Please end with the JSON block exactly as requested." You can also fill the fields by hand.',
      finding: emptyFinding(str(text.replace(/```[\s\S]*?```/g, ''), 1500)),
      warnings,
    };
  }
  const src = obj.proposed_new_source && typeof obj.proposed_new_source === 'object' ? obj.proposed_new_source : {};
  const covered = str(obj.covered_by_existing_v1_source, 40).toLowerCase();
  const finding = {
    explanation: str(obj.explanation),
    primary_driver: str(obj.primary_driver, 200),
    driver_category: oneOf(obj.driver_category, DRIVER_CATEGORIES, 'OTHER'),
    finding_type: oneOf(obj.finding_type, FINDING_TYPES, 'NEW_SOURCE'),
    covered_by_existing_v1_source: v1SourceIds.includes(covered) ? covered : 'none',
    proposed_new_source: {
      name: str(src.name, 200), url: httpUrl(src.url), what_it_measures: str(src.what_it_measures, 500),
      update_frequency: str(src.update_frequency, 100), free_or_paid: str(src.free_or_paid, 100),
    },
    proposed_signal: str(obj.proposed_signal, 1000),
    trend: str(obj.trend, 1000),
    evidence: (Array.isArray(obj.evidence) ? obj.evidence : []).slice(0, 20).map((e) => ({
      claim: str(e && e.claim, 500), url: httpUrl(e && e.url), publisher: str(e && e.publisher, 120), date: str(e && e.date, 20),
    })).filter((e) => e.claim || e.url),
    alternative_explanations: (Array.isArray(obj.alternative_explanations) ? obj.alternative_explanations : []).map((a) => str(a, 500)).filter(Boolean).slice(0, 10),
    limitations: str(obj.limitations, 1000),
    confidence: oneOf(obj.confidence, CONFIDENCE_LEVELS, 'LOW'),
    sentiment_assessment: oneOf(obj.sentiment_assessment, SENTIMENT_ASSESSMENTS, 'INDETERMINATE'),
  };
  if (!finding.explanation) warnings.push('The AI gave no explanation.');
  if (!finding.evidence.length) warnings.push('No checkable evidence (claim + URL) was provided. Treat this finding as unverified.');
  if (finding.evidence.some((e) => !e.url)) warnings.push('Some evidence items have no valid http(s) URL.');
  if (covered && covered !== 'none' && finding.covered_by_existing_v1_source === 'none') warnings.push(`"${covered}" is not a V1 source id; treated as none.`);
  if (finding.finding_type === 'NEW_SOURCE' && !finding.proposed_new_source.name) warnings.push('Finding type is NEW_SOURCE but no source name was given.');
  return { ok: true, finding, warnings };
}

export function emptyFinding(explanation = '') {
  return {
    explanation, primary_driver: '', driver_category: 'OTHER', finding_type: 'NEW_SOURCE', covered_by_existing_v1_source: 'none',
    proposed_new_source: { name: '', url: '', what_it_measures: '', update_frequency: '', free_or_paid: '' },
    proposed_signal: '', trend: '', evidence: [], alternative_explanations: [], limitations: '', confidence: 'LOW',
    sentiment_assessment: 'INDETERMINATE',
  };
}

// A human-confirmed finding -> the body registerStage7ResearchResponse already accepts. Stage 7's own fields
// (summary, transmission_mechanism, contradictory_evidence, sentiment_assessment, limitations) are filled so the
// existing Stage 7 views keep working; the learning fields ride along in the same inert findings JSON.
export function findingToStage7Registration(requestId, provider, edited) {
  const parsed = parseAiResearchResponse('```json\n' + JSON.stringify(edited || {}) + '\n```');
  const f = parsed.ok ? parsed.finding : emptyFinding();
  return {
    request_id: requestId,
    provider: ['claude', 'chatgpt', 'gemini', 'grok', 'other'].includes(provider) ? provider : null,
    findings: {
      schema: 'learning-finding-v1',
      summary: f.explanation,
      transmission_mechanism: f.primary_driver,
      contradictory_evidence: f.alternative_explanations.join('\n'),
      sentiment_assessment: f.sentiment_assessment,
      limitations: f.limitations,
      ...f,
    },
    sources: f.evidence.map((e) => ({ url: e.url, publisher: e.publisher, publication_date: e.date, claim: e.claim })),
    confidence: f.confidence,
    validated: true,
  };
}

export const LEARNING_CYCLE_STEPS = Object.freeze(['UNDERSTAND', 'INVESTIGATE', 'DISCOVER', 'LEARN', 'ADJUST', 'RECALCULATE', 'VALIDATE', 'APPROVE']);

// Where an event is in the loop, from what is persisted today. Steps after DISCOVER arrive in later slices.
export function learningCycleStage(caseRow, responseRow) {
  if (responseRow && responseRow.validation_status === 'VALIDATED') return 'LEARN';
  if (responseRow) return 'DISCOVER';
  if (caseRow) return 'DISCOVER';
  return 'INVESTIGATE';
}

export { HOUR };
