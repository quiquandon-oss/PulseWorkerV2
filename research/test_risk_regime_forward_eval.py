import hashlib
import json

import risk_regime_forward_eval as ev

D = ev.DAY
S = ev.EVAL_START_MS


def row(ts, direction, realized, status="RESOLVED"):
    return {"ts": ts, "v1_direction": direction, "btc_outcome_24h": {"outcome_status": status, "realized_direction": realized}}


def test_preregistration_is_frozen():
    # Any change to the registration changes this hash; a new hash is a new study, not a confirmation.
    assert ev.PREREG_SHA256 == hashlib.sha256(json.dumps(ev.PREREGISTRATION, sort_keys=True).encode()).hexdigest()
    assert ev.PREREG_SHA256 == "46c0d52b47350a7c7e83841e83ff646d8e5d0f5b9146d98908dab32411ff3e93"
    assert ev.PREREGISTRATION["evaluation_start_utc"] == "2026-10-10T00:00:00Z" and ev.EVAL_START_MS == 1791590400000
    assert [h["measure"] for h in ev.PREREGISTRATION["hypotheses"]] == ["oi.level", "oi.chg24", "oi.chg72", "bnfund.level"]


def test_minimum_sample_is_derived_from_the_design_effect():
    assert ev.MIN_UNITS == ev.min_units_per_group(0.30, 0.10, 0.05 / 4, 0.80) == 75
    assert ev.min_units_per_group(0.30, 0.10, 0.05, 0.80) < ev.MIN_UNITS          # stricter alpha -> more units


def test_units_one_per_day_new_calls_only():
    rows = [row(S - 1, "UP", "DOWN"),                        # before registration: excluded
            row(S + 1000, "UP", "DOWN"), row(S + 2000, "UP", "UP"),        # same day: only the first counts
            row(S + D + 5, "DOWN", "DOWN"),                  # DOWN call: not in the UP population
            row(S + D + 9, "UP", "FLAT"), row(S + D + 10, "UP", None, status="PENDING"),
            row(S + 2 * D, "UP", "UP")]
    us = ev.units(rows)
    assert [(u["ts"], u["failed"]) for u in us] == [(S + 1000, True), (S + 2 * D, False)]


def test_fisher_one_sided():
    assert abs(ev.fisher_one_sided(3, 1, 1, 3) - 17 / 70) < 1e-12
    assert ev.fisher_one_sided(0, 4, 4, 0) == 1.0 and ev.fisher_one_sided(4, 0, 0, 4) < 0.02


def fake_assess(flagged_ts):
    def a(p, v1, m, t):
        return {"pct_rank": 1.0 if t in flagged_ts else 50.0, "abnormal": t in flagged_ts}
    return a


def test_no_decision_and_no_p_value_before_minimum(monkeypatch):
    us = [{"ts": S + k * D, "failed": k % 2 == 0} for k in range(20)]
    monkeypatch.setattr(ev.er, "assess", fake_assess({u["ts"] for u in us if u["failed"]}))
    r = ev.evaluate(None, None, us)
    assert r["overall"] == "INSUFFICIENT_SAMPLE"
    assert all(h["decision"] == "INSUFFICIENT_SAMPLE" and "p_one_sided" not in h for h in r["hypotheses"].values())
    assert r["hypotheses"]["H1"]["table"]["failed_flagged"] == 10          # counts are still reported


def test_decision_rule_once_minimum_met(monkeypatch):
    n = ev.MIN_UNITS
    us = [{"ts": S + k * D, "failed": k % 2 == 0} for k in range(2 * n)]
    failed = [u["ts"] for u in us if u["failed"]]
    correct = [u["ts"] for u in us if not u["failed"]]
    flagged = set(failed[::2]) | set(correct[::10])                     # 50 % vs 10 %, in both halves
    monkeypatch.setattr(ev.er, "assess", fake_assess(flagged))
    r = ev.evaluate(None, None, us)
    assert r["hypotheses"]["H1"]["decision"] == "SUPPORTED" and r["overall"] == "SUPPORTED"
    monkeypatch.setattr(ev.er, "assess", fake_assess(set(failed[::10]) | set(correct[::10])))   # no difference
    r = ev.evaluate(None, None, us)
    assert r["overall"] == "NOT_SUPPORTED"
    first_half_only = set(failed[: n // 2])                               # effect only early: fails consistency
    monkeypatch.setattr(ev.er, "assess", fake_assess(first_half_only | set(correct[::10])))
    assert ev.evaluate(None, None, us)["hypotheses"]["H1"]["consistent_halves"] is False


def test_exploratory_section_is_separate(monkeypatch):
    us = [{"ts": S + k * D, "failed": k % 2 == 0} for k in range(4)]
    import copy
    monkeypatch.setattr(ev.er, "assess", fake_assess(set()))
    dims, evd = copy.deepcopy(ev.er.DIMENSIONS), list(ev.er.EVIDENCE_DIMS)
    try:
        ev.er.enable_oi()
        r = ev.evaluate(None, None, us)
    finally:
        ev.er.DIMENSIONS.clear(); ev.er.DIMENSIONS.update(dims); ev.er.EVIDENCE_DIMS[:] = evd
    assert not set(r["exploratory"]) & {h["measure"] for h in ev.PREREGISTRATION["hypotheses"]}
    assert all(v["label"].startswith("EXPLORATORY") for v in r["exploratory"].values())
