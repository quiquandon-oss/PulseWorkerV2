"""Tests for the Risk Regime historical reconstruction.

Every series below is a FIXTURE built for the test; none is, or stands in for, real V1, prediction, GDELT or
Hyperliquid data.
"""
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pytest

import risk_regime_data as d
import risk_regime_reconstruction as rc
import risk_regime_sources as s

H = d.HOUR
B, E = rc.EVENT15["prediction_boundary_ms"], rc.EVENT15["event_ms"]


def mk(ts, v, avail=None, source="hyperliquid_hip3", inst="BTC", metric="close"):
    return d.make_obs(source=source, provider="P", instrument=inst, metric=metric, timestamp=ts, value=v, unit="u",
                      interval="1h", retrieved_at=E + 100 * H, historical_or_live=d.HISTORICAL, source_url="x",
                      available_at=avail if avail is not None else ts + H)


# ---------------- Event #15 constants ----------------
def test_event15_constants_match_the_brief():
    assert datetime.fromtimestamp(B / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-09-27 12:01"
    assert datetime.fromtimestamp(E / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M") == "2026-09-28 03:01"
    labels = [x[0] for x in rc.TIMELINE]
    assert labels == ["-24h", "-12h", "-6h", "-3h", "-1h", "-30m", "prediction boundary", "event", "+1h", "+3h", "+6h", "+12h", "+24h"]


def test_timeline_aligns_offsets_and_never_reads_ahead():
    st = d.ObservationStore()
    hour = B - B % H
    st.add([mk(hour - k * H, 100.0 + k) for k in range(0, 30)] + [mk(hour + k * H, 200.0 + k) for k in range(1, 30)])
    tl = rc.timeline(st, B, E, st.keys())
    row = {r["label"]: r for r in tl}
    assert row["prediction boundary"]["t_ms"] == B and row["event"]["t_ms"] == E and row["+24h"]["t_ms"] == E + 24 * H
    assert row["prediction boundary"]["before_prediction"] and not row["event"]["before_prediction"]
    v = row["prediction boundary"]["values"]["hyperliquid_hip3|BTC|close"]
    # the bar opening at hour-1h closed before B; the bar opening at `hour` closes after B and must not be used
    assert v["value"] == 101.0 and v["timestamp"] == d.iso(hour - H) and v["change_vs_previous"] == -1.0
    for r in tl:
        val = r["values"]["hyperliquid_hip3|BTC|close"]
        assert d.iso(r["t_ms"]) >= val["available_at"]


def test_snapshot_reports_missing_and_stale():
    st = d.ObservationStore()
    st.add([mk(B - 10 * H, 1.0)])
    snap = rc.snapshot(st, B, st.keys() + [("bybit_oi", "BTCUSDT", "open_interest")])
    assert snap["hyperliquid_hip3|BTC|close"]["status"] == "STALE"              # 10 h old > 2 h limit, still shown
    assert snap["hyperliquid_hip3|BTC|close"]["value"] == 1.0
    assert snap["bybit_oi|BTCUSDT|open_interest"] == {"status": "NOT_AVAILABLE_AT_T"}


def test_compact_timeline_shape():
    st = d.ObservationStore()
    st.add([mk(B - k * H, float(k)) for k in range(30)])
    t = rc.compact_timeline(st, B, st.keys())
    assert t["columns"] == ["hyperliquid_hip3|BTC|close"] and len(t["rows"]) == 11
    assert t["rows"][0]["label"] == "-24h" and t["rows"][-1]["values"][0][2] == "STALE"


# ---------------- outcomes (outcome_engine reused) ----------------
def test_v1_outcomes_reuse_outcome_engine():
    v1 = [{"ts": 1000 * H, "score": 60.0}, {"ts": 1001 * H, "score": 40.0}, {"ts": 1100 * H, "score": 55.0}]
    btc = [{"ts": 1000 * H, "btc_price": 100.0}, {"ts": 1001 * H, "btc_price": 101.0}, {"ts": 1024 * H, "btc_price": 90.0},
           {"ts": 1025 * H, "btc_price": 99.0}]
    o = rc.v1_outcomes(v1, btc)
    assert o[1000 * H]["realized_direction"] == "DOWN" and o[1000 * H]["forward_return_pct"] == pytest.approx(-10.0)
    assert o[1001 * H]["realized_btc_price"] == 99.0
    assert o[1100 * H]["outcome_status"] == "UNRESOLVED_NO_FUTURE_PRICE_POINT"     # no later price: not resolved, not dropped


def test_v1_direction_is_the_exp005_rule():
    assert rc.v1_direction(50) == "UP" and rc.v1_direction(49.9) == "DOWN" and rc.v1_direction(None) is None


# ---------------- failure index ----------------
def test_failure_index_definitions_and_stable_ids():
    v1 = [{"ts": 10 * H, "score": 70.0}, {"ts": 11 * H, "score": 30.0}, {"ts": 12 * H, "score": 70.0}]
    outs = {10 * H: {"outcome_status": "RESOLVED", "realized_direction": "DOWN", "forward_return_pct": -1.0},
            11 * H: {"outcome_status": "RESOLVED", "realized_direction": "DOWN", "forward_return_pct": -2.0},
            12 * H: {"outcome_status": "RESOLVED", "realized_direction": "FLAT", "forward_return_pct": 0.0}}
    preds = [{"id": 1, "ts": 1 * H, "horizon_h": 24, "p_up": 0.8, "realized_up": 0, "realized_return": -1},
             {"id": 2, "ts": 2 * H, "horizon_h": 24, "p_up": 0.2, "realized_up": 1, "realized_return": 1},
             {"id": 3, "ts": 3 * H, "horizon_h": 24, "p_up": 0.7, "realized_up": 1, "realized_return": 1},
             {"id": 4, "ts": 4 * H, "horizon_h": 24, "p_up": 0.5, "realized_up": 0, "realized_return": -1},
             {"id": 5, "ts": 5 * H, "horizon_h": 12, "p_up": 0.9, "realized_up": None, "realized_return": None}]
    events = [{"event_id": 15, "event_ts": E, "detection_ts": E + H, "category": "V2_FAILURE_CLUSTER", "direction": "BTC_24h",
               "intensity": 5, "trigger_metric": "consecutive_incorrect_predictions", "trigger_threshold": 5, "trigger_version": "pr3-v1"}]
    ix = {x["id"]: x for x in rc.failure_index(v1, outs, preds, events)}
    assert ix["RE-15"]["type"] == "V2_FAILURE_CLUSTER" and ix["RE-15"]["anchor_ts"] == E
    assert ix["PRED-1"]["type"] == "PRED_UP_BTC_DOWN" and ix["PRED-2"]["type"] == "PRED_DOWN_BTC_UP"
    assert "PRED-3" not in ix and "PRED-4" not in ix and "PRED-5" not in ix          # correct / p=0.5 / unresolved
    assert ix["PREDRUN-24h-1"]["run_length"] == 2 and ix["PREDRUN-24h-1"]["prediction_ids"] == [1, 2]
    assert ix[f"V1OBS-{10 * H}"]["type"] == "V1_UP_BTC_DOWN"
    assert f"V1OBS-{11 * H}" not in ix and f"V1OBS-{12 * H}" not in ix                # correct DOWN call; FLAT outcome


# ---------------- page ----------------
def test_embed_page_cannot_break_out_of_script():
    html = rc.embed_page("<script>const EMBEDDED = /*__EMBEDDED_DATA__*/null;</script>", {"title": "</script><img src=x>"})
    assert html.count("</script>") == 1 and "<\\/script>" in html


def test_event_research_page_has_no_score():
    page = (Path(rc.__file__).resolve().parent / "event_research.html").read_text()
    assert "/*__EMBEDDED_DATA__*/null" in page and "no Risk Regime Shock score" in page
    assert "weight" not in page.lower().replace("no weights", "")


# ---------------- runner end-to-end (FIXTURE inputs, providers failing) ----------------
def test_runner_end_to_end_with_fixtures(tmp_path, monkeypatch):
    import gdelt_research_run as gr
    import hyperliquid_asset_universe_run as ur
    import hyperliquid_research_run as hr
    start = B - 12 * 24 * H - (B % H)
    def fake_candles(sym, interval, lo, hi, cache, log):
        log["candleSnapshot_CACHED"] += 1
        return [{"t": start + i * H, "T": start + i * H + H - 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 100.0 + i, "v": 1.0, "n": 1}
                for i in range(24 * 14)]
    monkeypatch.setattr(ur, "candles", fake_candles)
    monkeypatch.setattr(hr, "fetch_funding", lambda lo, hi, cache, log: [{"t": start + i * H, "funding": 1.25e-5, "premium": -2e-4} for i in range(24 * 14)])
    monkeypatch.setattr(gr, "master_index", lambda earliest: {})
    def fake_series(batches, idx, cache, ev):
        return ({b: {"total_events": 100, "geo_events": 5, "escalation_events": 1, "corridor_escalation_events": 0, "geo_sources": 3,
                     "geo_articles": 4, "categories": {"fight": 1}} for b in batches[-200:]}, [], {"fetch_status_counts": {"CACHED": 200}})
    monkeypatch.setattr(gr, "build_series", fake_series)
    monkeypatch.setattr(rc, "gdelt_raw_events", lambda windows, cache, idx, ret, cap=None: {
        k: {"window": [d.iso(a), d.iso(b)], "relevant_events": 1, "stored": 1, "storage_cap": (cap or {}).get(k),
            "events": [{"global_event_id": 1, "first_seen": B - H, "available_at": B - 45 * 60_000, "num_mentions": 9,
                        "cameo_code": "190", "category": "fight", "goldstein_sign": -1, "source_url": "https://news.example/x"}]}
        for k, (a, b) in windows.items()})
    def boom(url):
        raise OSError("Tunnel connection failed: 403 Forbidden")
    monkeypatch.setattr(s.Http, "_urlopen", staticmethod(boom))
    v1 = [{"ts": B - k * 3 * H, "score": 55.0, "g": 10, "m": 50, "o": 50, "y": 50, "n": 50, "s": 50, "u": 50, "fd": 50, "ls": 50, "hf": 50}
          for k in range(40, 0, -1)] + [{"ts": B + 60_000, "score": 60.0}]
    preds = [{"id": 1125, "ts": B, "horizon_h": 12, "p_up": 0.7333, "realized_up": 0, "realized_return": -0.57}]
    events = [{"event_id": 15, "event_ts": E, "detection_ts": E + H, "category": "V2_FAILURE_CLUSTER", "direction": "BTC_24h",
               "intensity": 5, "trigger_metric": "consecutive_incorrect_predictions", "trigger_threshold": 5, "trigger_version": "pr3-v1"}]
    btc = [{"ts": B - k * H, "btc_price": 84000.0 + k} for k in range(200)] + [{"ts": B + 30 * H, "btc_price": 83000.0}]
    files = {}
    for name, obj in (("v1", v1), ("p", preds), ("e", events), ("b", btc)):
        files[name] = tmp_path / f"{name}.json"
        files[name].write_text(json.dumps(obj))
    for c in ("hl", "hf", "gd"):
        (tmp_path / c).mkdir()
        (tmp_path / c / "f").write_text("x")
    out = tmp_path / "out"
    rc_ = rc.main(["--v1", str(files["v1"]), "--predictions", str(files["p"]), "--events", str(files["e"]), "--btc", str(files["b"]),
                   "--hl-cache", str(tmp_path / "hl"), "--hl-funding-cache", str(tmp_path / "hf"), "--gdelt-cache", str(tmp_path / "gd"),
                   "--out-dir", str(out), "--live"])
    assert rc_ == 0
    inv = json.loads((out / "risk_regime_source_inventory.json").read_text())
    assert inv["sources"]["hyperliquid_hip3"]["status"] == "OK"
    assert inv["sources"]["bybit_oi"]["status"] == "BLOCKED_BY_NETWORK_POLICY" and inv["sources"]["bybit_oi"]["observations"] == 0
    assert inv["sources"]["liquidations_xoomar"]["status"] == "RESEARCH_REQUIRED"
    ev = json.loads((out / "risk_regime_event15.json").read_text())
    assert [r["label"] for r in ev["timeline"]][6] == "prediction boundary"
    assert ev["gdelt_relevant_events_available_before_boundary"] == 1
    assert "bybit_oi|BTCUSDT|open_interest" not in ev["timeline"][0]["values"]       # nothing invented for blocked sources
    hist = json.loads((out / "risk_regime_history.json").read_text())
    assert hist["v1_observations"] == len(v1) and hist["rows"][0]["v1_direction"] == "UP"
    assert "PRED-1125" in {x["id"] for x in hist["failure_index"]}
    assert (out / "risk_regime_raw" / "observations.jsonl.gz").exists()
    assert (out / "risk_regime_event_research.html").exists()
    assert "score" not in json.dumps(inv["contract_fields"])
