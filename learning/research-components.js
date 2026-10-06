// Research components of the Risk Regime Shock new-signal prototype. READ-ONLY and informational: nothing
// here is a V1 input, a weight, a confidence or a methodology version. The GDELT geopolitical component is
// produced by research/gdelt_research_run.py; its committed result artifact is summarised here so the learning
// candidate can show what exists, what was measured and what is still missing.
import GDELT_RESULT from '../research/results/gdelt_risk_regime_shock.json';

export const RISK_REGIME_SHOCK_SIGNAL_ID = 'risk_regime_shock';

function gdeltSummary(a) {
  const live = a && a.status === 'OK';
  const e = live ? a.event15 : null;
  return {
    id: 'gdelt_geopolitical',
    input: 'Geopolitical event severity',
    status: live ? 'RESEARCH_RESULT_AVAILABLE' : 'BUILT_LIVE_DATA_PENDING',
    source: {
      name: a.source.name, url: a.source.event_files_base, master_file_list: a.source.master_file_list, codebook: a.source.codebook,
      cost: a.source.cost, api_key_required: a.source.api_key_required, update_frequency: a.source.update_frequency,
      historical_coverage: a.source.history_from,
    },
    fields_used: a.fields_used,
    transformation: 'Conflict-category CAMEO events (QuadClass 3/4) per 15-minute batch; hourly shares of all GDELT events, '
      + 'escalation share, Gulf/Middle-East corridor escalation count and source breadth; each ranked against its own trailing '
      + '72-hour point-in-time history; geo_shock_score = median of the four percentiles (0-100). Goldstein is used for sign only.',
    timestamp_policy: 'Events are placed at DATEADDED (when GDELT first saw them), never at SQLDATE (the reported event day).',
    lookahead_policy: 'A 15-minute batch is usable only 15 minutes after its timestamp; rows dated after their discovery are rejected.',
    limitations: [
      'Counts are first-seen counts from the export file; later mention growth (mentions table) is not used yet.',
      'Geographic and URL-keyword relevance are coarse; protests and verbal conflict add noise.',
      'Isolated top-decile readings occur in quiet periods; the 90th-percentile "elevated" line is a reporting convention, not a validated threshold.',
      'No weight exists: the component has not been validated against V1 outcomes.',
    ],
    live_run: { status: a.status, error: a.error || null, required_batches: a.required_batches, required_range: a.required_range },
    event15: e ? {
      detected: e.detected, first_elevated: e.first_elevated, elevated_before_event: e.elevated_before_event,
      elevated_during_decline_24h: e.elevated_during_decline_24h, persistence_hours: e.persistence_hours_longest_run,
      score_at_event: e.at_event.geo_shock_score ?? null, counts_6h_before: e.counts_6h_before_event,
    } : { status: 'NOT_MEASURED', reason: 'The live GDELT read did not complete; no Event #15 result is claimed.' },
    v1_coverage: a.v1_coverage,
    v1_context: a.v1_only_findings,
    v1_weight: null,
    v1_impact: 'NONE',
  };
}

const PLANNED = [
  ['oil_shock', 'Brent / WTI'], ['treasury_shock', 'Treasury yields'], ['equity_volatility_shock', 'Equity futures / volatility'],
  ['usd_rates_shock', 'USD / rate expectations'], ['liquidation_shock', 'Crypto liquidation intensity'],
];

export function riskRegimeShockComponents(artifact = GDELT_RESULT) {
  return {
    signal_id: RISK_REGIME_SHOCK_SIGNAL_ID,
    combination: 'NOT_DEFINED: no combined weighting exists; each component is researched separately first.',
    components: [gdeltSummary(artifact), ...PLANNED.map(([id, input]) => ({ id, input, status: 'NOT_STARTED', v1_weight: null, v1_impact: 'NONE' }))],
  };
}

// The candidate shows the components when it is (or would become) the Risk Regime Shock prototype.
export function researchComponentsFor(adjustment, prototypeSuggestion) {
  const id = (adjustment && adjustment.type === 'SIGNAL_PROTOTYPE' && adjustment.signal_id) || (prototypeSuggestion && prototypeSuggestion.signal_id);
  return id === RISK_REGIME_SHOCK_SIGNAL_ID ? riskRegimeShockComponents() : null;
}
