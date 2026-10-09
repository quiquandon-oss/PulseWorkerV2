"""Pre-registered forward evaluation of NEW V1 calls against venue OI / funding context. RESEARCH ONLY.

Purpose: accumulate genuinely new evidence for (or against) reconsidering the Risk Regime Shock hypothesis,
without tuning anything on the data it is judged on. No score, no weights, no signal, no V1 change.

Everything that decides the outcome is fixed in PREREGISTRATION below. It was written on 2026-10-09, before any of
the evaluated calls existed (evaluation starts 2026-10-10 00:00 UTC). Its sha256 is reported with every result.
Changing it creates a new registration that cannot count the data already seen as confirmation.

Inputs (all read-only):
  --v1      read-only extract of V1 observations: [{"ts", "score", "sources_json" | "sources"}] (production `history`,
            SELECT only; see RISK_REGIME_FORWARD.md for the exact statement)
  --btc     read-only extract of btc_data: [{"ts", "btc_price"}] (outcomes via outcome_engine, unchanged)
  --forward the forward store (risk_regime_forward.py); the frozen historical files are loaded alongside, read-only,
            so trailing 7-day baselines are available from the first forward day.

Usage: python3 research/risk_regime_forward_eval.py --v1 v1.json --btc btc.json \
         --forward research/results/risk_regime_forward --out research/results/risk_regime_forward_eval.json
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk_regime_event_research as er  # noqa: E402
import risk_regime_forward as fw  # noqa: E402
import risk_regime_reconstruction as rc  # noqa: E402

UTC = timezone.utc
DAY, HOUR = er.DAY, er.HOUR

PREREGISTRATION = {
    "registered_on": "2026-10-09",
    "evaluation_start_utc": "2026-10-10T00:00:00Z",
    "call_rule": "EXP-005 V1 baseline call, unchanged: score >= 50 -> UP, else DOWN",
    "outcome_rule": "outcome_engine.compute_forward_returns_from_history, unchanged, 24h; FLAT and unresolved excluded",
    "population": "V1 UP calls whose 24h outcome resolved UP (correct) or DOWN (failed)",
    "unit_of_analysis": "the FIRST resolved V1 UP call of each UTC day (one unit per day; later calls the same day overlap "
                        "the same 24h outcome window and are excluded from the tests)",
    "abnormality_rule": "unchanged existing convention: outside the 5th-95th percentile of the measure's own trailing 7 days, "
                        "built only from values available at the call time (risk_regime_event_research.assess)",
    "hypotheses": [
        {"id": "H1", "measure": "oi.level", "tail": "low", "claim": "coin OI in its weekly low tail is more frequent before failed than before correct UP calls"},
        {"id": "H2", "measure": "oi.chg24", "tail": "high", "claim": "a 24h coin-OI rise in its weekly high tail is more frequent before failed UP calls"},
        {"id": "H3", "measure": "oi.chg72", "tail": "low", "claim": "a 72h coin-OI fall in its weekly low tail is more frequent before failed UP calls"},
        {"id": "H4", "measure": "bnfund.level", "tail": "low", "claim": "Binance funding in its weekly low tail is more frequent before failed UP calls"},
    ],
    "test": "one-sided Fisher exact test per hypothesis on the day-unit 2x2 table (flag x failed/correct); Bonferroni over the 4 hypotheses",
    "alpha_family": 0.05,
    "design_effect": {"flag_rate_failed": 0.30, "flag_rate_correct": 0.10, "power": 0.80},
    "minimum_units": "per group (failed / correct) at least the n from the design effect (computed below; not tunable)",
    "consistency": "the failed-minus-correct difference has the same sign in the chronological first and second half of the units",
    "decision": {
        "INSUFFICIENT_SAMPLE": "either group below the minimum: no conclusion, keep collecting",
        "SUPPORTED": "minimum met AND Bonferroni-adjusted p < alpha AND consistent in both halves -> reconsider the hypothesis "
                     "(a reason to design a new pre-registered study, not an activation)",
        "NOT_SUPPORTED": "minimum met and the criteria above not all satisfied",
    },
    "no_peeking": "interim reports show counts only; the decision is computed only once the minimum is met, and never re-run "
                  "with changed hypotheses on the same units",
    "exploratory": "every other measure is reported descriptively in a separate section and is never used for the decision",
}
PREREG_SHA256 = hashlib.sha256(json.dumps(PREREGISTRATION, sort_keys=True).encode()).hexdigest()
EVAL_START_MS = int(datetime(2026, 10, 10, tzinfo=UTC).timestamp() * 1000)


def min_units_per_group(p1: float, p0: float, alpha: float, power: float) -> int:
    """Two-proportion sample size (normal approximation, one-sided alpha, equal groups)."""
    z_a, z_b = NormalDist().inv_cdf(1 - alpha), NormalDist().inv_cdf(power)
    pbar = (p1 + p0) / 2
    n = (z_a * math.sqrt(2 * pbar * (1 - pbar)) + z_b * math.sqrt(p1 * (1 - p1) + p0 * (1 - p0))) ** 2 / (p1 - p0) ** 2
    return math.ceil(n)


MIN_UNITS = min_units_per_group(PREREGISTRATION["design_effect"]["flag_rate_failed"], PREREGISTRATION["design_effect"]["flag_rate_correct"],
                                PREREGISTRATION["alpha_family"] / len(PREREGISTRATION["hypotheses"]), PREREGISTRATION["design_effect"]["power"])


def fisher_one_sided(a: int, b: int, c: int, d: int) -> float:
    """P(X >= a) for the 2x2 table [[a, b], [c, d]] (rows: failed, correct; cols: flagged, not flagged)."""
    n1, n2, k = a + b, c + d, a + c
    total = math.comb(n1 + n2, k)
    return sum(math.comb(n1, x) * math.comb(n2, k - x) for x in range(a, min(n1, k) + 1)) / total if total else 1.0


def v1_rows(extract: Sequence[Dict], btc: Sequence[Dict]) -> List[Dict]:
    """Builds rows in the shape the existing analysis uses (ts, v1_score, v1_direction, v1_sources, btc_outcome_24h)."""
    rows = []
    for r in sorted(extract, key=lambda x: x["ts"]):
        src = r.get("sources")
        if src is None and r.get("sources_json"):
            src = json.loads(r["sources_json"])
        rows.append({"ts": r["ts"], "v1_score": r["score"], "v1_sources": src or {}})
    outs = rc.v1_outcomes([{"ts": r["ts"], "score": r["v1_score"]} for r in rows], btc) if rows else {}
    for r in rows:
        r["v1_direction"] = rc.v1_direction(r["v1_score"])
        r["btc_outcome_24h"] = outs.get(r["ts"], {})
    return rows


def units(rows: Sequence[Dict], start_ms: int = EVAL_START_MS) -> List[Dict]:
    """First resolved UP call per UTC day, from the registered start."""
    seen, out = set(), []
    for r in rows:
        o = r["btc_outcome_24h"]
        if r["ts"] < start_ms or r["v1_direction"] != "UP" or o.get("outcome_status") != "RESOLVED" or o.get("realized_direction") not in ("UP", "DOWN"):
            continue
        day = r["ts"] // DAY
        if day in seen:
            continue
        seen.add(day)
        out.append({"ts": r["ts"], "t": er.iso(r["ts"]), "failed": o["realized_direction"] == "DOWN"})
    return out


def flag(a: Dict, tail: str) -> Optional[bool]:
    r = a.get("pct_rank")
    if r is None:
        return None
    return r < er.LO_PCT if tail == "low" else r > er.HI_PCT


def evaluate(p: "er.PIT", v1: "er.V1", us: Sequence[Dict]) -> Dict:
    res = {"preregistration_sha256": PREREG_SHA256, "evaluation_start": er.iso(EVAL_START_MS), "min_units_per_group": MIN_UNITS,
           "units": len(us), "failed_units": sum(u["failed"] for u in us), "correct_units": sum(not u["failed"] for u in us),
           "hypotheses": {}, "exploratory": {}}
    enough = res["failed_units"] >= MIN_UNITS and res["correct_units"] >= MIN_UNITS
    alpha_each = PREREGISTRATION["alpha_family"] / len(PREREGISTRATION["hypotheses"])
    for h in PREREGISTRATION["hypotheses"]:
        rows = [(u, flag(er.assess(p, v1, h["measure"], u["ts"]), h["tail"])) for u in us]
        known = [(u, f) for u, f in rows if f is not None]
        a = sum(1 for u, f in known if u["failed"] and f)
        b = sum(1 for u, f in known if u["failed"] and not f)
        c = sum(1 for u, f in known if not u["failed"] and f)
        d = sum(1 for u, f in known if not u["failed"] and not f)
        half = len(known) // 2

        def diff(part):
            fa = [f for u, f in part if u["failed"]]
            co = [f for u, f in part if not u["failed"]]
            return None if not fa or not co else sum(fa) / len(fa) - sum(co) / len(co)
        d1, d2 = diff(known[:half]), diff(known[half:])
        out = {"measure": h["measure"], "tail": h["tail"], "table": {"failed_flagged": a, "failed_not": b, "correct_flagged": c, "correct_not": d},
               "missing_flag": len(rows) - len(known),
               "flag_rate_failed": round(a / (a + b), 3) if a + b else None, "flag_rate_correct": round(c / (c + d), 3) if c + d else None,
               "first_half_diff": None if d1 is None else round(d1, 3), "second_half_diff": None if d2 is None else round(d2, 3)}
        if not enough:
            out["decision"] = "INSUFFICIENT_SAMPLE"          # no p-value is computed before the minimum is met (no peeking)
        else:
            pval = fisher_one_sided(a, b, c, d)
            consistent = d1 is not None and d2 is not None and d1 > 0 and d2 > 0
            out.update(p_one_sided=pval, p_bonferroni=min(1.0, pval * len(PREREGISTRATION["hypotheses"])), consistent_halves=consistent,
                       decision="SUPPORTED" if pval < alpha_each and consistent else "NOT_SUPPORTED")
        res["hypotheses"][h["id"]] = out
    tested = {h["measure"] for h in PREREGISTRATION["hypotheses"]}
    for m in er.all_measures():
        if m in tested:
            continue
        fl = [(u, er.assess(p, v1, m, u["ts"])["abnormal"]) for u in us]
        fl = [(u, f) for u, f in fl if f is not None]
        fa = [f for u, f in fl if u["failed"]]
        co = [f for u, f in fl if not u["failed"]]
        res["exploratory"][m] = {"label": "EXPLORATORY (not part of the decision)", "n": len(fl),
                                 "abnormal_rate_failed": round(sum(fa) / len(fa), 3) if fa else None,
                                 "abnormal_rate_correct": round(sum(co) / len(co), 3) if co else None}
    res["overall"] = ("INSUFFICIENT_SAMPLE" if not enough else
                      "SUPPORTED" if any(h["decision"] == "SUPPORTED" for h in res["hypotheses"].values()) else "NOT_SUPPORTED")
    return res


def load_pit(forward: Path) -> "er.PIT":
    rows = []
    for path in fw.FROZEN:
        with gzip.open(path, "rt") as f:
            rows += [json.loads(line) for line in f]
    rows += fw.load_forward(forward)
    return er.PIT(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--v1", required=True)
    ap.add_argument("--btc", required=True)
    ap.add_argument("--forward", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    er.enable_oi()
    rows = v1_rows(json.load(open(args.v1)), json.load(open(args.btc)))
    v1 = er.V1(rows)
    us = units(rows)
    res = evaluate(load_pit(Path(args.forward)), v1, us)
    res["inputs_sha256"] = {n: hashlib.sha256(Path(x).read_bytes()).hexdigest() for n, x in (("v1", args.v1), ("btc", args.btc))}
    res["unit_list"] = us
    Path(args.out).write_text(json.dumps(res, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps({"overall": res["overall"], "units": res["units"], "failed": res["failed_units"], "correct": res["correct_units"],
                      "min_units_per_group": MIN_UNITS, "prereg": PREREG_SHA256[:12]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
