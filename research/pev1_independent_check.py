"""Independent re-implementation of PEV-1 VAL (model B, climatology) from the protocol text, sharing no code with
prediction_eval.py. Usage: python3 research/pev1_independent_check.py research/evaluations/2026-11-01-pev1-report.json
Needs the research-data/market-moves checkout at /home/user/mm-data (or edit MM below)."""
import json, numpy as np, sys
MM = "/home/user/mm-data/market_moves/hourly"
H = 3600000; S = 3 * H; SEAL_S = 1784678400000; DEV_END = 1781481600000
def rsi(p):
    p = np.asarray(p, float); ch = np.diff(p)
    g = ch[ch > 0].sum() / len(ch); l = (-ch[ch < 0]).sum() / len(ch)
    if g + l == 0: return 50
    if l == 0: return 100
    return int(np.floor(100 - 100 / (1 + g / l) + 0.5))
out = {}
for coin in ("BTC", "ETH", "LINK"):
    px = {}
    for line in open(f"{MM}/{coin}/observations.jsonl"):
        c = json.loads(line); a = c["close_ts"] + 1
        if a <= SEAL_S and a % S == 0: px[a] = float(c["c"])
    ts = sorted(px)
    feats = {}
    run = []
    prev = None
    for t in ts:
        if prev is not None and t - prev != S: run = []
        run.append(px[t]); prev = t
        if len(run) >= 30:
            r = run[-21:]; lm = np.mean(r); sm = np.mean(r[-8:])
            feats[t] = (rsi(run[-30:]), (sm - lm) / lm * 100)
    for h in (12, 24):
        lab = {t: px[t + h * H] / px[t] - 1 for t in feats if t + h * H in px}
        units = [t for t in feats if DEV_END <= t < SEAL_S and t % (h * H) == 0 and t in lab]
        pb, pc, ys = [], [], []
        for t in units:
            train = np.array([feats[x] for x in feats if x < t])
            mu, sd = train.mean(0), train.std(0); sd[sd == 0] = 1
            cand = [x for x in feats if x <= t - 1.2 * h * H and x in lab]
            Z = (np.array([feats[x] for x in cand]) - mu) / sd; q = (np.array(feats[t]) - mu) / sd
            d = np.sqrt(((Z - q) ** 2).sum(1)); k = min(15, max(5, len(cand) // 3))
            idx = np.argsort(d, kind="stable")[:k]
            ups = np.array([lab[cand[i]] > 0 for i in idx])
            pb.append(ups.mean()); pc.append(np.mean([lab[x] > 0 for x in cand])); ys.append(lab[t] > 0)
        pb, pc, ys = map(np.array, (pb, pc, ys))
        def ba(p): return (np.mean(p[ys] >= .5) + np.mean(p[~ys] < .5)) / 2
        out[f"{coin} {h}h"] = dict(n=len(units), B_brier=round(float(np.mean((pb - ys) ** 2)), 4), clim_brier=round(float(np.mean((pc - ys) ** 2)), 4),
                                    B_ba=round(float(ba(pb)), 4), up_share=round(float(ys.mean()), 4))
rep = json.load(open(sys.argv[1]))
for k, v in out.items():
    coin, h = k.split()
    m = rep["coins"][coin][h]["val"]
    hv = dict(n=m["n_units"], B_brier=m["models"]["B"]["brier"], clim_brier=m["models"]["climatology"]["brier"], B_ba=m["models"]["B"]["balanced_accuracy"], up_share=m["up_share"])
    print(k, "MATCH" if hv == v else "DIFF", v, "" if hv == v else hv)
