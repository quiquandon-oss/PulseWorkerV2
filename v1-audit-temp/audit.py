#!/usr/bin/env python3
"""
V1 Technical Score audit -- frozen methodology, executed once, no peeking.
Reimplements V1's EXACT formulas (verified against the real source, not
approximated) against a real, freshly-fetched 365-day CoinGecko series.
"""
import json, math, sys
import urllib.request

import numpy as np
from scipy import stats

# ---------- Phase 1: fetch the exact same data V1 fetches ----------
URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365"
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())

prices = data.get("prices", [])
if not prices:
    print("DATA_UNAVAILABLE: CoinGecko returned no price data. Stopping per Phase 2 instruction.")
    sys.exit(1)

closes = [p[1] for p in prices]
dates = [p[0] for p in prices]  # ms epoch
n_raw = len(closes)

# ---------- V1's exact formulas, verbatim-equivalent to index.html ----------
def tech_sma(values, period, end_index):
    if end_index - period + 1 < 0: return None
    return sum(values[end_index-period+1:end_index+1]) / period

def tech_ichimoku(closes, period, end_index):
    if end_index - period + 1 < 0: return None
    window = closes[end_index-period+1:end_index+1]
    return (max(window) + min(window)) / 2

def tech_rsi(closes, period, end_index):
    if end_index - period < 0: return None
    gains = losses = 0.0
    for i in range(end_index-period+1, end_index+1):
        diff = closes[i] - closes[i-1]
        if diff >= 0: gains += diff
        else: losses -= diff
    avg_gain, avg_loss = gains/period, losses/period
    if avg_loss == 0: return 100.0
    return 100 - 100/(1+avg_gain/avg_loss)

def find_local_extrema(values, start_idx, end_idx, k, kind):
    out = []
    for i in range(start_idx+k, end_idx-k+1):
        is_extreme = True
        for j in range(i-k, i+k+1):
            if j == i: continue
            if kind == 'peak' and values[j] >= values[i]: is_extreme = False; break
            if kind == 'trough' and values[j] <= values[i]: is_extreme = False; break
        if is_extreme: out.append(i)
    return out

def tech_ichimoku_cloud(closes, last_index):
    cloud_calc_index = last_index - 26
    if cloud_calc_index < 52: return {'position': 'N/A'}
    tenkan_then = tech_ichimoku(closes, 9, cloud_calc_index)
    kijun_then = tech_ichimoku(closes, 26, cloud_calc_index)
    senkou_a = (tenkan_then+kijun_then)/2 if tenkan_then is not None and kijun_then is not None else None
    senkou_b = tech_ichimoku(closes, 52, cloud_calc_index)
    if senkou_a is None or senkou_b is None: return {'position': 'N/A'}
    price = closes[last_index]
    cloud_top, cloud_bottom = max(senkou_a, senkou_b), min(senkou_a, senkou_b)
    position = 'above' if price > cloud_top else 'below' if price < cloud_bottom else 'inside'
    return {'position': position}

def tech_ma_crossover_event(closes, last_index, lookback_days):
    last_cross_type, days_ago, prev_diff = None, None, None
    start = max(200, last_index - lookback_days)
    for i in range(start, last_index+1):
        ma50, ma200 = tech_sma(closes, 50, i), tech_sma(closes, 200, i)
        if ma50 is None or ma200 is None: continue
        diff = ma50 - ma200
        if prev_diff is not None:
            if prev_diff <= 0 and diff > 0: last_cross_type, days_ago = 'golden', last_index - i
            elif prev_diff >= 0 and diff < 0: last_cross_type, days_ago = 'death', last_index - i
        prev_diff = diff
    return {'type': last_cross_type, 'daysAgo': days_ago}

def tech_swing_structure(closes, last_index, lookback_days):
    start_idx = max(0, last_index - lookback_days)
    peaks = find_local_extrema(closes, start_idx, last_index, 3, 'peak')
    troughs = find_local_extrema(closes, start_idx, last_index, 3, 'trough')
    if len(peaks) < 2 or len(troughs) < 2: return {'structure': None}
    p1, p2 = closes[peaks[-2]], closes[peaks[-1]]
    t1, t2 = closes[troughs[-2]], closes[troughs[-1]]
    hh, hl = p2 > p1, t2 > t1
    if hh and hl: structure = 'Uptrend'
    elif not hh and not hl: structure = 'Downtrend'
    else: structure = 'Mixed'
    return {'structure': structure}

def tech_rsi_divergence(closes, last_index, lookback_days):
    start_idx = max(52, last_index - lookback_days)
    peaks = find_local_extrema(closes, start_idx, last_index, 3, 'peak')
    troughs = find_local_extrema(closes, start_idx, last_index, 3, 'trough')
    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        r1, r2 = tech_rsi(closes,14,p1), tech_rsi(closes,14,p2)
        if r1 is not None and r2 is not None and closes[p2] > closes[p1] and r2 < r1:
            return {'type': 'bearish'}
    if len(troughs) >= 2:
        t1, t2 = troughs[-2], troughs[-1]
        r1, r2 = tech_rsi(closes,14,t1), tech_rsi(closes,14,t2)
        if r1 is not None and r2 is not None and closes[t2] < closes[t1] and r2 > r1:
            return {'type': 'bullish'}
    return {'type': 'none'}

def tech_rsi_to_score(rsi):
    if rsi is None: return 50
    return round(100 - max(0, min(100, rsi)))

def tech_cloud_to_score(cloud):
    if not cloud or cloud['position'] == 'N/A': return 50
    return 80 if cloud['position']=='above' else 20 if cloud['position']=='below' else 50

def tech_crossover_to_score(crossover):
    if not crossover or not crossover['type']: return 50
    recency = max(0, 1-(crossover['daysAgo'] or 90)/90)
    magnitude = 30*recency
    return round(50+magnitude) if crossover['type']=='golden' else round(50-magnitude)

def tech_swing_to_score(swing):
    if not swing or not swing.get('structure'): return 50
    s = swing['structure']
    if s == 'Uptrend': return 80
    if s == 'Downtrend': return 20
    return 50

def tech_divergence_to_score(div):
    if not div or not div.get('type') or div['type']=='none': return 50
    return 80 if div['type']=='bullish' else 20

print(json.dumps({"phase": "fetch", "n_raw_points": n_raw,
                   "first_date_ms": dates[0], "last_date_ms": dates[-1]}))

# ---------- Build per-day signal scores, exactly mirroring computeTechnicalScoreSeries ----------
MIN_INDEX = 200
rows = []
for i in range(MIN_INDEX, n_raw):
    rsi14 = tech_rsi(closes, 14, i)
    cloud = tech_ichimoku_cloud(closes, i)
    crossover = tech_ma_crossover_event(closes, i, 90)
    swing = tech_swing_structure(closes, i, 60)
    divergence = tech_rsi_divergence(closes, i, 60)
    s_rsi = tech_rsi_to_score(rsi14)
    s_cloud = tech_cloud_to_score(cloud)
    s_cross = tech_crossover_to_score(crossover)
    s_swing = tech_swing_to_score(swing)
    s_div = tech_divergence_to_score(divergence)
    combined = round((s_rsi+s_cloud+s_cross+s_swing+s_div)/5)
    rows.append({'i': i, 'date_ms': dates[i], 'price': closes[i],
                 'rsi': s_rsi, 'cloud': s_cloud, 'crossover': s_cross,
                 'swing': s_swing, 'divergence': s_div, 'combined': combined})

# ---------- Phase 3: forward return (next-day), non-overlapping at h=1 ----------
for k in range(len(rows)-1):
    rows[k]['fwd_ret'] = (rows[k+1]['price'] - rows[k]['price']) / rows[k]['price'] * 100
usable = [r for r in rows if 'fwd_ret' in r]

# ---------- Phase 4: regime, FROZEN rule, trailing 7-calendar-day return ----------
# Declared before any correlation was computed (see chat): rally >+5%, correction <-5%, chop between.
price_by_i = {r['i']: r['price'] for r in rows}
for r in usable:
    i = r['i']
    idx7 = i - 7
    if idx7 in price_by_i:
        trail7 = (r['price'] - price_by_i[idx7]) / price_by_i[idx7] * 100
        r['trail7'] = trail7
        r['regime'] = 'rally' if trail7 > 5 else 'correction' if trail7 < -5 else 'chop'
    else:
        r['regime'] = None

usable_regime = [r for r in usable if r['regime'] is not None]

def pearson_with_ci(x, y):
    n = len(x)
    if n < 4: return {'r': None, 'p': None, 'n': n, 'ci_lo': None, 'ci_hi': None}
    r, p = stats.pearsonr(x, y)
    z = np.arctanh(r)
    se = 1/np.sqrt(n-3)
    lo, hi = np.tanh(z-1.96*se), np.tanh(z+1.96*se)
    return {'r': round(float(r),4), 'p': round(float(p),4), 'n': n,
            'ci_lo': round(float(lo),4), 'ci_hi': round(float(hi),4)}

SIGNALS = ['rsi','cloud','crossover','swing','divergence']

def block_for(rows_subset, label):
    out = {'label': label, 'n': len(rows_subset)}
    if len(rows_subset) < 4:
        out['note'] = 'N too small for any reliable stat'
        return out
    fwd = [r['fwd_ret'] for r in rows_subset]
    for sig in SIGNALS:
        vals = [r[sig] for r in rows_subset]
        out[sig] = pearson_with_ci(vals, fwd)
    out['always_up_rate'] = round(sum(1 for f in fwd if f>0)/len(fwd), 4)
    return out

results = {}
results['unconditional'] = block_for(usable, 'unconditional (all usable days)')
for regime in ['rally','correction','chop']:
    subset = [r for r in usable_regime if r['regime']==regime]
    results[regime] = block_for(subset, regime)

# ---------- Phase 6: signal redundancy (inter-signal correlation) ----------
redundancy = {}
for a in range(len(SIGNALS)):
    for b in range(a+1, len(SIGNALS)):
        s1, s2 = SIGNALS[a], SIGNALS[b]
        v1 = [r[s1] for r in usable]; v2 = [r[s2] for r in usable]
        r, p = stats.pearsonr(v1, v2)
        redundancy[f'{s1}_vs_{s2}'] = round(float(r), 4)

# ---------- Phase 8: temporal stability -- fixed midpoint split, declared in advance ----------
mid = len(usable)//2
earlier, later = usable[:mid], usable[mid:]
temporal = {'earlier': block_for(earlier, 'earlier half'), 'later': block_for(later, 'later half')}

# ---------- Phase 7: incremental value -- simple multivariate logistic, if N supports it ----------
incremental = {'attempted': False}
try:
    from sklearn.linear_model import LogisticRegression
    X = np.array([[r[s] for s in SIGNALS] for r in usable])
    y = np.array([1 if r['fwd_ret']>0 else 0 for r in usable])
    if len(y) >= 100 and len(set(y.tolist())) == 2:
        Xs = (X - X.mean(axis=0)) / (X.std(axis=0)+1e-9)
        clf = LogisticRegression(max_iter=1000).fit(Xs, y)
        incremental = {'attempted': True, 'n': len(y),
                        'coefs': {SIGNALS[i]: round(float(clf.coef_[0][i]),4) for i in range(5)},
                        'note': 'standardized coefficients, sign/magnitude only -- not a formal significance test per-coefficient'}
    else:
        incremental = {'attempted': False, 'reason': f'n={len(y)} or single-class outcome, insufficient for a reliable multivariate fit'}
except ImportError:
    incremental = {'attempted': False, 'reason': 'sklearn unavailable in this environment'}

# ---------- Multiple-testing correction, Bonferroni across the 5 unconditional p-values ----------
p_values = [results['unconditional'][s]['p'] for s in SIGNALS if results['unconditional'][s]['p'] is not None]
bonferroni_alpha = 0.05/5
n_survive_bonferroni = sum(1 for p in p_values if p < bonferroni_alpha)

# ---------- Duplicate/missing check ----------
date_days = set()
dup_days = 0
for r in rows:
    from datetime import datetime, timezone
    day = datetime.fromtimestamp(r['date_ms']/1000, tz=timezone.utc).date().isoformat()
    if day in date_days: dup_days += 1
    date_days.add(day)

final = {
    'n_raw_points': n_raw,
    'n_usable_for_forward_return': len(usable),
    'n_usable_with_regime': len(usable_regime),
    'regime_counts': {r: len([x for x in usable_regime if x['regime']==r]) for r in ['rally','correction','chop']},
    'duplicate_calendar_days_in_raw_series': dup_days,
    'results': results,
    'signal_redundancy': redundancy,
    'temporal_stability': temporal,
    'incremental_value': incremental,
    'bonferroni_alpha': bonferroni_alpha,
    'n_signals_surviving_bonferroni_unconditional': n_survive_bonferroni,
}
print("===FINAL_JSON_START===")
print(json.dumps(final, indent=2, default=str))
print("===FINAL_JSON_END===")
