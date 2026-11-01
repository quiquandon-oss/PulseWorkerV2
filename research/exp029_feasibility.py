"""EXP-029 feasibility from stored source scores only (no returns, no outcomes): per weighting, how often its call
differs from production W0, and how correlated the composites are. Seal-safe: reads only post-seal readings.

  python3 research/exp029_feasibility.py <experiment5-sentiment archive.json> <PulseWorker worker.js>
"""
import json
import math
import re
import sys

SEAL_END = 1791579600000


def weights_from_worker(src):
    blk = src[src.index('const SENTIMENT_DEFAULT_WEIGHTS = {'):src.index('};', src.index('const SENTIMENT_DEFAULT_WEIGHTS = {'))]
    return {k: (float(a), float(b)) for k, a, b in re.findall(r"(\w+): \[([\d.]+), ([\d.]+)\]", blk)}


def composite(sources, w, scheme):
    """Weighted mean over the sources present (renormalised, never neutral-filled). scheme: W0 base*conf, W1 conf only
    (equal base), W2 base only (confidence fixed at 1)."""
    num = den = 0.0
    for k, v in sources.items():
        if k not in w or not isinstance(v, (int, float)):
            continue
        b, c = w[k]
        x = {"W0": b * c, "W1": c, "W2": b}[scheme]
        num += v * x
        den += x
    return num / den if den else None


def _rank(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    for k, i in enumerate(order):
        r[i] = k
    return r


def _pearson(a, b):
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else None


def feasibility(readings, w):
    rows = [r["sources"] for r in readings]
    c = {s: [composite(x, w, s) for x in rows] for s in ("W0", "W1", "W2")}
    out = {"n_readings": len(rows)}
    for s in ("W1", "W2"):
        pairs = [(a, b) for a, b in zip(c["W0"], c[s]) if a is not None and b is not None and a != 50 and b != 50]
        disc = sum((a > 50) != (b > 50) for a, b in pairs)
        ok = [(a, b) for a, b in zip(c["W0"], c[s]) if a is not None and b is not None]
        out[s] = {"n": len(pairs), "discordant": disc, "discordance_rate": disc / len(pairs) if pairs else None,
                  "spearman_r_with_W0": _pearson(_rank([a for a, _ in ok]), _rank([b for _, b in ok])) if len(ok) > 2 else None}
    return out


def main(argv):
    data = json.load(open(argv[1]))
    series = data["series"] if isinstance(data, dict) else data
    readings = [{"ts": int(r["observation_ts"]), "sources": json.loads(r.get("sources_json") or "{}")}
                for r in series if int(r["observation_ts"]) > SEAL_END]
    print(json.dumps(feasibility(readings, weights_from_worker(open(argv[2]).read())), indent=1))


if __name__ == "__main__":
    main(sys.argv)
