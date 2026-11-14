// Deterministic scoring of independently collected evidence for Sentiment V2 (calc version sv2-1.1.0).
//
// Headlines are scored with V1's lexicon and arithmetic, copied verbatim from PulseWorker's scomp-v1 port
// (SCOMP_SENT_LEXICON / scompScoreHeadline / scompScoreHeadlineBatch, itself contract-tested against the browser's
// SENT_LEXICON in CryptoPulse/index.html, defaults version 2026-10-12). Using the same scorer means a headline means
// the same thing in V1 and V2; what V2 adds is independent sources, freshness, de-duplication and provenance.
// A test pins this copy by hash so a silent change is caught.

export const SV2_LEXICON_SOURCE = 'PulseWorker scomp-v1 SCOMP_SENT_LEXICON (browser SENT_LEXICON, 2026-10-12)';
export const SV2_SENT_LEXICON = {
  surge: 2, rally: 2, soar: 2, bullish: 2, gain: 1, gains: 1, rise: 1, rises: 1, rising: 1,
  jump: 1, jumps: 1, boost: 1, boosts: 1, record: 1, high: 1, highs: 1, growth: 1, grows: 1,
  approval: 2, approve: 2, approves: 2, partnership: 1, adoption: 2, upgrade: 1, breakthrough: 2,
  recovery: 1, recovers: 1, inflow: 1, inflows: 1, buy: 1, buying: 1, optimism: 2, stabilize: 1,
  ease: 1, eases: 1, cut: 1, cuts: 1, 'rate cut': 2, stimulus: 1, expansion: 1, strong: 1, beat: 1,
  crash: -2, plunge: -2, plunges: -2, bearish: -2, drop: -1, drops: -1, falls: -1, falling: -1,
  decline: -1, declines: -1, slump: -2, 'sell-off': -2, selloff: -2, fear: -1, fears: -1, panic: -2,
  ban: -2, bans: -2, banned: -2, hack: -2, hacked: -2, exploit: -2, lawsuit: -1, sue: -1, sues: -1,
  sanction: -2, sanctions: -2, war: -2, conflict: -1, tension: -1, tensions: -1, attack: -2,
  inflation: -1, recession: -2, default: -2, bankruptcy: -2, bankrupt: -2, liquidation: -1,
  liquidations: -1, hike: -1, hikes: -1, 'rate hike': -2, crackdown: -2, investigation: -1, fraud: -2,
  outflow: -1, outflows: -1, downturn: -1, volatility: -1, uncertainty: -1, risk: -1, risks: -1,
  tariff: -1, tariffs: -1, 'trade war': -2, stagflation: -2, escalate: -1, escalation: -1,
  blockade: -2, strikes: -1, hawkish: -1,
  ceasefire: 2, 'de-escalate': 1, 'de-escalation': 1, 'peace treaty': 2, dovish: 1,
};
const WORD_KEYS = Object.keys(SV2_SENT_LEXICON).filter(k => /^[a-z0-9_]+$/.test(k));
const PHRASE_RES = Object.keys(SV2_SENT_LEXICON).filter(k => !/^[a-z0-9_]+$/.test(k))
  .map(k => [k, new RegExp('\\b' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b')]);

export function sv2ScoreHeadline(title) {
  const t = String(title).toLowerCase();
  const tokens = new Set(t.split(/[^a-z0-9_]+/));
  let score = 0, hits = 0;
  for (const w of WORD_KEYS) if (tokens.has(w)) { score += SV2_SENT_LEXICON[w]; hits++; }
  for (const [k, re] of PHRASE_RES) if (re.test(t)) { score += SV2_SENT_LEXICON[k]; hits++; }
  return { score, hits };
}

// V1's batch arithmetic: mean lexicon score per hit, saturated with tanh(x / 1.5), mapped to 0..100 (50 = neutral).
export function sv2HeadlineBatchTo0100(total, hits) {
  return Math.round((Math.tanh((hits > 0 ? total / hits : 0) / 1.5) + 1) * 50);
}

export const SV2_EVIDENCE_RULES = { minHits: 2, maxItemsPerSource: 25 };

// Point-in-time readings, one per registry source, at decision time t.
// Uses only items retrieved at or before t that are not cross-source duplicates; an item's evidence time is its
// publication time, or its retrieval time when the publisher gave none. States:
//   fresh        ≥ minHits lexicon hits across items whose evidence time is within freshness_h before t → scored
//   insufficient items in the window, but fewer lexicon hits than minHits → excluded (never imputed as neutral 50)
//   stale        items exist, none within the window → excluded
//   missing      nothing retrieved from this source by t → excluded
export function sv2EvidenceReadings(rows, t, registry, rules = SV2_EVIDENCE_RULES) {
  const bySource = {};
  for (const r of rows || []) {
    if (r.dup_of) continue;
    const retrieved = Number(r.retrieved_ts);
    if (!(retrieved <= t)) continue;
    const pub = r.published_ts === null || r.published_ts === undefined ? null : Number(r.published_ts);
    const evTs = pub !== null && Number.isFinite(pub) && pub <= retrieved ? pub : retrieved;
    (bySource[r.source_id] = bySource[r.source_id] || []).push({ id: r.evidence_id, title: r.title, ts: evTs, url: r.canonical_url });
  }
  const out = {};
  for (const s of registry) {
    const key = `ev:${s.id}`;
    const items = (bySource[s.id] || []).sort((a, b) => b.ts - a.ts);
    if (!items.length) { out[key] = { state: 'missing', family: s.family, source_id: s.id }; continue; }
    const win = items.filter(x => x.ts <= t && x.ts >= t - s.freshness_h * 3600000).slice(0, rules.maxItemsPerSource);
    if (!win.length) { out[key] = { state: 'stale', family: s.family, source_id: s.id, latest_ts: items[0].ts }; continue; }
    let total = 0, hits = 0;
    const scored = win.map(x => { const h = sv2ScoreHeadline(x.title); total += h.score; hits += h.hits; return { id: x.id, ts: x.ts, score: h.score, hits: h.hits }; });
    const base = { family: s.family, source_id: s.id, items: win.length, hits, latest_ts: win[0].ts,
      item_ids: scored.filter(x => x.hits > 0).map(x => ({ id: x.id, sign: Math.sign(x.score) })) };
    out[key] = hits >= rules.minHits ? { ...base, state: 'fresh', value: sv2HeadlineBatchTo0100(total, hits) } : { ...base, state: 'insufficient' };
  }
  return out;
}
