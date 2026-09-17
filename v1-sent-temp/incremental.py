#!/usr/bin/env python3
import json, urllib.request
import numpy as np
from scipy import stats
from datetime import datetime, timezone

URL = "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365"
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    data = json.loads(resp.read())
prices = data["prices"]
closes = [p[1] for p in prices]
dates_ms = [p[0] for p in prices]
date_strs = [datetime.fromtimestamp(t/1000, tz=timezone.utc).date().isoformat() for t in dates_ms]

def tech_sma(v,p,e):
    if e-p+1<0: return None
    return sum(v[e-p+1:e+1])/p
def tech_ichimoku(c,p,e):
    if e-p+1<0: return None
    w=c[e-p+1:e+1]; return (max(w)+min(w))/2
def tech_rsi(c,p,e):
    if e-p<0: return None
    g=l=0.0
    for i in range(e-p+1,e+1):
        d=c[i]-c[i-1]
        if d>=0: g+=d
        else: l-=d
    ag,al=g/p,l/p
    if al==0: return 100.0
    return 100-100/(1+ag/al)
def find_extrema(v,s,e,k,kind):
    out=[]
    for i in range(s+k,e-k+1):
        ext=True
        for j in range(i-k,i+k+1):
            if j==i: continue
            if kind=='peak' and v[j]>=v[i]: ext=False; break
            if kind=='trough' and v[j]<=v[i]: ext=False; break
        if ext: out.append(i)
    return out
def cloud(c,e):
    cc=e-26
    if cc<52: return {'position':'N/A'}
    t=tech_ichimoku(c,9,cc); k=tech_ichimoku(c,26,cc)
    sa=(t+k)/2 if t is not None and k is not None else None
    sb=tech_ichimoku(c,52,cc)
    if sa is None or sb is None: return {'position':'N/A'}
    p=c[e]; ct,cb=max(sa,sb),min(sa,sb)
    return {'position':'above' if p>ct else 'below' if p<cb else 'inside'}
def crossover(c,e,lb):
    lct,da,pd=None,None,None
    st=max(200,e-lb)
    for i in range(st,e+1):
        m50,m200=tech_sma(c,50,i),tech_sma(c,200,i)
        if m50 is None or m200 is None: continue
        diff=m50-m200
        if pd is not None:
            if pd<=0 and diff>0: lct,da='golden',e-i
            elif pd>=0 and diff<0: lct,da='death',e-i
        pd=diff
    return {'type':lct,'daysAgo':da}
def swing(c,e,lb):
    st=max(0,e-lb)
    pk=find_extrema(c,st,e,3,'peak'); tr=find_extrema(c,st,e,3,'trough')
    if len(pk)<2 or len(tr)<2: return {'structure':None}
    p1,p2=c[pk[-2]],c[pk[-1]]; t1,t2=c[tr[-2]],c[tr[-1]]
    hh,hl=p2>p1,t2>t1
    return {'structure':'Uptrend' if hh and hl else 'Downtrend' if not hh and not hl else 'Mixed'}
def divergence(c,e,lb):
    st=max(52,e-lb)
    pk=find_extrema(c,st,e,3,'peak'); tr=find_extrema(c,st,e,3,'trough')
    if len(pk)>=2:
        p1,p2=pk[-2],pk[-1]; r1,r2=tech_rsi(c,14,p1),tech_rsi(c,14,p2)
        if r1 is not None and r2 is not None and c[p2]>c[p1] and r2<r1: return {'type':'bearish'}
    if len(tr)>=2:
        t1,t2=tr[-2],tr[-1]; r1,r2=tech_rsi(c,14,t1),tech_rsi(c,14,t2)
        if r1 is not None and r2 is not None and c[t2]<c[t1] and r2>r1: return {'type':'bullish'}
    return {'type':'none'}
def rsi_score(r): return 50 if r is None else round(100-max(0,min(100,r)))
def cloud_score(cl): return 50 if not cl or cl['position']=='N/A' else 80 if cl['position']=='above' else 20 if cl['position']=='below' else 50
def cross_score(cr):
    if not cr or not cr['type']: return 50
    rec=max(0,1-(cr['daysAgo'] or 90)/90); mag=30*rec
    return round(50+mag) if cr['type']=='golden' else round(50-mag)
def swing_score(sw):
    if not sw or not sw.get('structure'): return 50
    return 80 if sw['structure']=='Uptrend' else 20 if sw['structure']=='Downtrend' else 50
def div_score(dv):
    if not dv or not dv.get('type') or dv['type']=='none': return 50
    return 80 if dv['type']=='bullish' else 20

tech_by_date = {}
for i in range(200, len(closes)):
    d = date_strs[i]
    t_score = round((rsi_score(tech_rsi(closes,14,i)) + cloud_score(cloud(closes,i)) +
                      cross_score(crossover(closes,i,90)) + swing_score(swing(closes,i,60)) +
                      div_score(divergence(closes,i,60))) / 5)
    tech_by_date[d] = t_score

with open('data.json') as f:
    sent_days = json.load(f)

merged = []
for i in range(len(sent_days)-1):
    d = sent_days[i]['day']
    if d in tech_by_date:
        merged.append({'sentiment': sent_days[i]['recomputed'], 'technical': tech_by_date[d],
                        'fwd24h': (sent_days[i+1]['btc_price']-sent_days[i]['btc_price'])/sent_days[i]['btc_price']*100})

n = len(merged)
sent = [m['sentiment'] for m in merged]
tech = [m['technical'] for m in merged]
fwd = [m['fwd24h'] for m in merged]

r_sent,p_sent = stats.pearsonr(sent, fwd)
r_tech,p_tech = stats.pearsonr(tech, fwd)
r_st,p_st = stats.pearsonr(sent, tech)

result = {
    'n_merged': n,
    'model_A_sentiment_alone': {'r': round(float(r_sent),4), 'p': round(float(p_sent),4)},
    'model_B_technical_alone': {'r': round(float(r_tech),4), 'p': round(float(p_tech),4)},
    'sentiment_vs_technical_correlation': {'r': round(float(r_st),4), 'p': round(float(p_st),4)},
}
try:
    from sklearn.linear_model import LinearRegression
    X = np.array([[s,t] for s,t in zip(sent,tech)])
    Xs = (X-X.mean(axis=0))/(X.std(axis=0)+1e-9)
    y = np.array(fwd)
    reg = LinearRegression().fit(Xs, y)
    result['model_C_combined_standardized_coefs'] = {'sentiment': round(float(reg.coef_[0]),4), 'technical': round(float(reg.coef_[1]),4)}
    result['model_C_r_squared'] = round(float(reg.score(Xs,y)),4)
except ImportError:
    result['model_C'] = 'sklearn unavailable'

print("===INCREMENTAL_JSON_START===")
print(json.dumps(result, indent=2))
print("===INCREMENTAL_JSON_END===")
