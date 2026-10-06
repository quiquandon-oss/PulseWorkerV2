"""Tests for the Risk Regime Shock convergence research.

Every series below is a FIXTURE: synthetic candles / GDELT readings used only to test the logic. None of it is,
or stands in for, real GDELT or Hyperliquid data.
"""
import json
from datetime import datetime, timezone

import pytest

import risk_regime_shock as r
import risk_regime_shock_run as run

H = r.HOUR


def candles(closes, start=0, v=1.0):
    return [{"t": start + i * H, "T": start + i * H + H - 1, "o": c * 0.999, "h": c, "l": c * 0.998, "c": c, "v": v, "n": 1}
            for i, c in enumerate(closes)]


def ms(y, mo, d, h=0):
    return int(datetime(y, mo, d, h, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------- market hours ----------------
def test_underlying_open_weekend_rule():
    assert r.underlying_open(ms(2026, 9, 25, 20))          # Fri 20:00
    assert not r.underlying_open(ms(2026, 9, 25, 21))      # Fri 21:00
    assert not r.underlying_open(ms(2026, 9, 26, 12))      # Sat
    assert not r.underlying_open(ms(2026, 9, 27, 12))      # Sun 12:00 (the Event #15 boundary)
    assert r.underlying_open(ms(2026, 9, 27, 22))          # Sun 22:00 reopen
    assert r.underlying_open(ms(2026, 9, 28, 3))           # Mon


# ---------------- instrument features ----------------
def test_instrument_features_point_in_time_and_inactive():
    closes = [100 + (i % 7) * 0.1 for i in range(200)] + [90.0]
    inst = r.Instrument("X", candles(closes))
    f = inst.features(201 * H)
    assert f["status"] == "OK" and f["pct_6h"] == 0.0 and r.abnormal_dir(f) == "down"
    assert inst.features(200 * H)["price"] != 90.0             # the 90 candle is not closed yet: no look-ahead
    flat = [{"t": i * H, "T": i * H + H - 1, "o": 5.0, "h": 5.0, "l": 5.0, "c": 5.0, "v": 0.0, "n": 0} for i in range(200)]
    g = r.Instrument("FLAT", flat).features(200 * H)
    assert g["status"] == "INACTIVE" and r.abnormal_dir(g) is None


def test_abnormal_dir_needs_ok_and_baseline():
    assert r.abnormal_dir({"status": "OK", "pct_6h": 96}) == "up"
    assert r.abnormal_dir({"status": "OK", "pct_6h": 50}) is None
    assert r.abnormal_dir({"status": "OK", "pct_6h": None}) is None
    assert r.abnormal_dir({"status": "INACTIVE", "pct_6h": 1}) is None


def _f(p):
    return {"status": "OK", "pct_6h": p, "underlying_open": True}


def test_market_state_formulations_and_groups():
    feats = {"xyz:SP500": _f(2), "xyz:XYZ100": _f(3), "xyz:GOLD": _f(97), "xyz:SILVER": _f(50), "xyz:COPPER": _f(1),
             "xyz:BRENTOIL": _f(99), "xyz:EUR": _f(50), "para:10Y": _f(50)}
    m = r.market_state(feats)
    assert m["abnormal_any_count"] == 4                       # SP500, GOLD, COPPER, BRENT (XYZ100 not in the breadth basket)
    assert m["classic_risk_off_count"] == 3                   # SP500 down, GOLD up, COPPER down; oil has no a-priori direction
    assert m["btc_aligned_count_in_sample"] == 3              # SP500 down, COPPER down, BRENT up
    assert m["elevated"] and m["elevated_sensitivity"] == {"2": True, "3": True, "4": True}
    assert m["equity_stress"] == ["xyz:SP500:down", "xyz:XYZ100:down"] and m["rates_stress"] == []
    feats["xyz:BRENTOIL"] = {"status": "INACTIVE", "pct_6h": 99}
    feats["xyz:GOLD"] = _f(50)
    m2 = r.market_state(feats)
    assert m2["abnormal_any_count"] == 2 and not m2["elevated"] and m2["inactive"] == ["xyz:BRENTOIL"]


def test_btc_pairs():
    feats = {"xyz:SP500": _f(50), "xyz:XYZ100": _f(4), "para:10Y": _f(98)}
    p = r.btc_pairs(_f(1), feats)
    assert p["btc_down_equity_down"] and p["equity_down_symbols"] == ["xyz:XYZ100"] and p["btc_down_10y_up"]
    assert not r.btc_pairs(_f(50), feats)["btc_down_equity_down"]


# ---------------- GDELT component ----------------
def test_geo_at_point_in_time_and_persistence():
    q = 15 * 60_000
    grid = [{"t_ms": i * q, "status": "OK", "geo_shock_score": s, "elevated": s >= 90} for i, s in enumerate([50, 92, 95, 91, 40])]
    assert r.geo_at(grid, 3 * q + 1)["persistence_h"] == 0.75
    assert r.geo_at(grid, 3 * q - 1)["geo_shock_score"] == 95         # never reads a later grid point
    assert r.geo_at(grid, -1)["status"] == "NO_DATA"
    assert r.geo_at(grid, 10 * q)["status"] == "NO_DATA"              # grid ended: no stale carry-forward


def test_classify_state():
    assert [r.classify_state(a, b) for a, b in ((1, 1), (1, 0), (0, 1), (0, 0))] == ["A_BOTH", "B_GEO_ONLY", "C_MARKET_ONLY", "D_NEITHER"]


def test_proximity_symmetric_vs_trailing():
    geo = [(i * H, i == 10) for i in range(30)]
    mkt = [(i * H, i == 13) for i in range(30)]
    p = r.proximity(10 * H, geo, mkt)
    assert p["0h"]["symmetric"] == "B_GEO_ONLY"
    assert p["3h"]["symmetric"] == "A_BOTH"           # market 3h LATER: descriptive only
    assert p["3h"]["trailing"] == "B_GEO_ONLY"        # not usable at t
    assert p["24h"]["trailing"] == "B_GEO_ONLY"
    assert r.proximity(13 * H, geo, mkt)["3h"]["trailing"] == "A_BOTH"


def test_cooccurrence_and_shift_null():
    geo = [(i * H, i % 50 == 0) for i in range(500)]
    mkt = [(i * H, i % 50 == 1) for i in range(500)]
    c = r.cooccurrence(geo, mkt, 1)
    assert c == {"geo_hours": 10, "with_market_within": 10, "share": 1.0}
    assert r.cooccurrence(geo, mkt, 0)["share"] == 0.0
    nul = r.shift_null(geo, mkt, 1)
    assert nul["observed"] == 1.0 and nul["null_n"] > 0 and nul["share_of_null_at_or_above_observed"] < 1.0


def test_episodes():
    assert r.episodes([(0, True), (1, True), (2, False), (3, True)]) == [
        {"start": 0, "end": 1, "hours": 2}, {"start": 3, "end": 3, "hours": 1}]


def test_group_performance():
    rows = [{"k": "A", "p_up": 0.8, "realized_up": 0, "realized_return": -1.0},
            {"k": "A", "p_up": 0.2, "realized_up": 0, "realized_return": -0.5},
            {"k": "D", "p_up": 0.5, "realized_up": 1, "realized_return": 0.2},
            {"k": "D", "p_up": 0.9, "realized_up": None, "realized_return": None}]
    g = r.group_performance(rows, "k")
    assert g["A"]["n"] == 2 and g["A"]["hit_rate"] == 0.5 and g["A"]["brier"] == pytest.approx((0.64 + 0.04) / 2, abs=1e-4)
    assert g["D"]["n"] == 1 and g["D"]["hits"] == 0 and g["A"]["small_sample"]


# ---------------- runner (FIXTURE inputs) ----------------
def test_runner_end_to_end_with_fixtures(tmp_path, monkeypatch):
    e = run.EVENT15["event_ts_ms"]
    start = e - 12 * 24 * H
    v1_ts = [e - k * 5 * H for k in range(60, 0, -1)] + [e + 60_000]
    q = 15 * 60_000

    def fake_grid(ts, cache):
        rows = []
        t = ts[0] - (ts[0] % q)
        while t <= ts[-1]:
            score = 95.0 if e - 3 * H <= t <= e else 40.0       # FIXTURE geo spike in the 3h before the event
            rows.append({"t_ms": t, "status": "OK", "geo_shock_score": score, "elevated": score >= 90})
            t += q
        return {"rows": rows, "fetch_status_counts": {"CACHED": 1}, "batches": 1}

    def fake_inst(ts, cache):
        out = {}
        for s in ["BTC"] + list(r.BASKET):
            closes = [100 + (i % 9) * 0.05 for i in range(24 * 14)]
            k = (e - start) // H - 2
            for j in range(k, len(closes)):
                closes[j] = 95.0 if s in ("BTC", "xyz:SP500", "xyz:GOLD", "xyz:COPPER") else closes[j]
            out[s] = r.Instrument(s, candles(closes, start))
        return out, {"candleSnapshot_CACHED": 1}

    monkeypatch.setattr(run, "gdelt_grid", fake_grid)
    monkeypatch.setattr(run, "load_instruments", fake_inst)
    v1 = [{"ts": t, "g": 50, "m": 50, "o": 50, "y": 50, "n": 50, "s": 50, "u": 50, "fd": 50, "ls": 50, "hf": 50,
           "score": 60.0} for t in v1_ts]
    preds = [{"id": 1, "ts": e - 10 * H, "horizon_h": 24, "p_up": 0.7, "realized_up": 0, "realized_return": -1.0},
             {"id": 2, "ts": e + H, "horizon_h": 12, "p_up": 0.3, "realized_up": 0, "realized_return": -0.4}]
    (tmp_path / "v1.json").write_text(json.dumps(v1))
    (tmp_path / "p.json").write_text(json.dumps(preds))
    out = tmp_path / "a.json"
    rc = run.main(["--v1", str(tmp_path / "v1.json"), "--predictions", str(tmp_path / "p.json"), "--gdelt-cache", str(tmp_path),
                   "--hl-cache", str(tmp_path), "--out", str(out)])
    a = json.loads(out.read_text())
    assert rc == 0, a.get("trace")
    assert "score" not in json.dumps(a["declared_rules"]).lower() or "geo_shock_score" in json.dumps(a["declared_rules"])
    ev = a["event15"]
    assert ev["at_event"]["market"]["elevated"] and ev["at_event"]["geo"]["elevated"]
    assert not ev["at_boundary"]["market"]["elevated"] and not ev["at_boundary"]["geo"]["elevated"]
    assert ev["proximity_at_boundary"]["24h"]["trailing"] == "D_NEITHER"
    assert a["v1_observations"]["classes_same_time"].get("A_BOTH", 0) >= 1
    assert a["v1_predictions"]["same_time"]["D_NEITHER"]["n"] == 1
    rows = json.loads((tmp_path / "risk_regime_shock_v1_rows.json").read_text())
    assert len(rows) == len(v1_ts) and {"class", "geo_shock_score", "breadth", "btc_fwd_24h_pct"} <= set(rows[0])
    assert "RiskRegimeShock" not in out.read_text()


def test_runner_failure_makes_no_claims(tmp_path, monkeypatch):
    def boom(ts, cache):
        raise RuntimeError("GDELT unreachable (FIXTURE)")
    monkeypatch.setattr(run, "gdelt_grid", boom)
    (tmp_path / "v1.json").write_text(json.dumps([{"ts": 1790000000000}]))
    (tmp_path / "p.json").write_text("[]")
    out = tmp_path / "a.json"
    rc = run.main(["--v1", str(tmp_path / "v1.json"), "--predictions", str(tmp_path / "p.json"), "--gdelt-cache", str(tmp_path),
                   "--hl-cache", str(tmp_path), "--out", str(out)])
    a = json.loads(out.read_text())
    assert rc == 2 and a["status"] == "FAILED" and "event15" not in a and "hourly" not in a
