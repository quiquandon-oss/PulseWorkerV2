// Event Detection Layer -- leakage-safe evaluation of detections against independently labelled move episodes.
// Pure function. It measures DETECTION and EARLY WARNING only; it cannot establish PREDICTION (that needs a frozen
// out-of-sample design comparing against a baseline -- see EVENT_DETECTION.md section "Evaluation").
//
// Inputs
//   events   : ledger rows. Only detected_ts is used for timing (what the system knew, when) -- never source_ts.
//   episodes : [{ id, onset_ts, end_ts, direction: 'UP'|'DOWN' }] from a labeller that does not see the events
//              (e.g. the frozen market-moves v1.0.0 labels). Episodes are not tuned on the events.
//   window   : { start_ts, end_ts } observation window. Events and episodes outside it are excluded.
//   leadMs   : how long before onset a detection still counts (early-warning horizon).
//   lagMs    : how long after the episode end a detection still counts (late detection).
//   coverage : optional [{ start_ts, end_ts }] intervals when the detector was healthy (OK); episodes outside
//              coverage are reported separately and excluded from recall (a detector cannot see what it did not watch).
export function evaluateDetections({ events, episodes, window, leadMs, lagMs, coverage = null }) {
  if (!window || !(window.end_ts > window.start_ts)) throw new Error('window.start_ts < window.end_ts required');
  if (!(leadMs >= 0) || !(lagMs >= 0)) throw new Error('leadMs and lagMs must be >= 0');
  const inWin = (t) => t >= window.start_ts && t <= window.end_ts;
  const covered = (t) => !coverage || coverage.some((c) => t >= c.start_ts && t <= c.end_ts);
  const evs = events.filter((e) => Number.isInteger(e.detected_ts) && inWin(e.detected_ts));
  const eps = episodes.filter((p) => inWin(p.onset_ts));
  const watched = eps.filter((p) => covered(p.onset_ts));

  const matches = [];
  const matchedEvent = new Set();
  for (const p of watched) {
    const hits = evs.filter((e) => e.detected_ts >= p.onset_ts - leadMs && e.detected_ts <= p.end_ts + lagMs);
    if (!hits.length) { matches.push({ episode: p.id, detected: false }); continue; }
    hits.sort((a, b) => a.detected_ts - b.detected_ts);
    for (const h of hits) matchedEvent.add(h.event_id);
    const first = hits[0];
    const directional = hits.filter((h) => h.direction === 'UP' || h.direction === 'DOWN');
    matches.push({
      episode: p.id, detected: true, first_event: first.event_id, first_type: first.event_type,
      latency_ms: first.detected_ts - p.onset_ts,                    // negative = before onset
      kind: first.detected_ts < p.onset_ts ? 'EARLY_WARNING' : 'DETECTION',
      direction_agreement: directional.length ? directional.filter((h) => h.direction === p.direction).length / directional.length : null,
    });
  }
  const detected = matches.filter((m) => m.detected);
  const lat = detected.map((m) => m.latency_ms).sort((a, b) => a - b);
  const days = (window.end_ts - window.start_ts) / 86_400_000;
  const unmatched = evs.filter((e) => !matchedEvent.has(e.event_id));
  const byType = {};
  for (const e of evs) {
    const t = (byType[e.event_type] = byType[e.event_type] || { events: 0, matched: 0 });
    t.events += 1;
    if (matchedEvent.has(e.event_id)) t.matched += 1;
  }
  return {
    window, lead_ms: leadMs, lag_ms: lagMs,
    episodes_total: eps.length, episodes_watched: watched.length, episodes_unwatched: eps.length - watched.length,
    events_total: evs.length,
    recall: watched.length ? detected.length / watched.length : null,
    precision: evs.length ? (evs.length - unmatched.length) / evs.length : null,
    false_positives_per_30d: days > 0 ? (unmatched.length / days) * 30 : null,
    early_warnings: detected.filter((m) => m.kind === 'EARLY_WARNING').length,
    detections_after_onset: detected.filter((m) => m.kind === 'DETECTION').length,
    latency_ms_median: lat.length ? lat[Math.floor(lat.length / 2)] : null,
    latency_ms_min: lat.length ? lat[0] : null,
    by_type: byType,
    matches,
    caveat: 'Descriptive detection metrics. Not evidence of predictive value: no out-of-sample baseline comparison.',
  };
}
