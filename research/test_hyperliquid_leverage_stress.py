"""Tests for the Hyperliquid leverage / market-stress research component.

Every payload below is a FIXTURE: synthetic data shaped like the Info API responses, used only to test
parsing, point-in-time access and the runner. None of it is, or stands in for, real Hyperliquid data.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import hyperliquid_leverage_stress as h
import hyperliquid_research_run as run

UTC = timezone.utc
HOUR = 3_600_000
E = run.EVENT15["event_ts_ms"]


# ---- FIXTURE generators ----
def fixture_candles(start_ms, hours, price=84000.0, drift=0.0, vol=100.0, crash_at=None):
    rows, p = [], price
    for i in range(hours):
        t = start_ms + i * HOUR
        if crash_at is not None and t >= crash_at:
            p *= 0.995
        else:
            p *= 1 + drift
        rows.append({"t": t, "T": t + HOUR - 1, "s": "BTC", "i": "1h", "o": str(p), "h": str(p * 1.001), "l": str(p * 0.999),
                     "c": str(p), "v": str(vol * (3 if crash_at and t >= crash_at else 1)), "n": 10})
    return rows


def fixture_funding(start_ms, hours, premium=0.0, spike_at=None):
    rows = []
    for i in range(hours):
        t = start_ms + i * HOUR
        prem = premium - (0.0004 if spike_at is not None and t >= spike_at else 0.0) + (i % 7) * 1e-6
        rate = h.FUNDING_FLOOR_HOURLY if abs(prem) < 0.0002 else h.FUNDING_FLOOR_HOURLY + prem / 8
        rows.append({"coin": "BTC", "fundingRate": repr(rate), "premium": repr(prem), "time": t})
    return rows


FIXTURE_META = [{"universe": [{"name": "ETH"}, {"name": "BTC"}]},
                [{"funding": "0.0000125", "openInterest": "1.0", "markPx": "1", "oraclePx": "1", "premium": "0", "prevDayPx": "1", "dayNtlVlm": "1", "midPx": "1"},
                 {"funding": "0.0000125", "openInterest": "25000.5", "markPx": "84000", "oraclePx": "84010", "premium": "-0.0001",
                  "prevDayPx": "84500", "dayNtlVlm": "1500000000", "midPx": "84001"}]]


# ---- parsing ----
def test_asset_ctx_reads_the_btc_row_not_another_coin():
    c = h.parse_asset_ctx(FIXTURE_META)
    assert c["open_interest"] == 25000.5 and c["mark_px"] == 84000 and c["oracle_px"] == 84010 and c["premium"] == -0.0001
    assert "openInterest" in c["fields_present"]


def test_asset_ctx_missing_field_is_none_not_invented():
    meta = [FIXTURE_META[0], [FIXTURE_META[1][0], {"funding": "0.0000125", "markPx": "84000"}]]
    c = h.parse_asset_ctx(meta)
    assert c["open_interest"] is None and c["premium"] is None and c["funding"] == 0.0000125


def test_parsers_reject_malformed_payloads():
    with pytest.raises(h.ParseError):
        h.parse_asset_ctx({"universe": []})
    with pytest.raises(h.ParseError):
        h.parse_asset_ctx([{"universe": [{"name": "ETH"}]}, [{}]])
    with pytest.raises(h.ParseError):
        h.parse_funding_history([{"coin": "BTC", "fundingRate": "x", "premium": "0", "time": 1}])
    with pytest.raises(h.ParseError):
        h.parse_funding_history([{"coin": "ETH", "fundingRate": "0", "premium": "0", "time": 1}])
    with pytest.raises(h.ParseError):
        h.parse_candles([{"t": 1, "T": 2, "o": "1", "h": "1", "l": "1", "c": "1"}])      # no volume


def test_funding_and_candles_are_sorted_and_deduplicated():
    rows = fixture_funding(0, 3)
    f = h.parse_funding_history(list(reversed(rows)) + rows[:1])
    assert [r["t"] for r in f] == [0, HOUR, 2 * HOUR]
    c = h.parse_candles(fixture_candles(0, 3)[::-1] + fixture_candles(0, 1))
    assert [x["t"] for x in c] == [0, HOUR, 2 * HOUR] and c[0]["T"] == HOUR - 1


# ---- point in time ----
def test_a_candle_is_unusable_until_it_has_closed():
    c = h.parse_candles(fixture_candles(0, 2))
    assert h.candles_usable_at(c, HOUR - 1) == []                 # first candle still open
    assert [x["t"] for x in h.candles_usable_at(c, HOUR)] == [0]   # closed at HOUR-1
    f = h.parse_funding_history(fixture_funding(0, 2))
    assert [r["t"] for r in h.funding_usable_at(f, HOUR - 1)] == [0]


def test_features_never_use_data_after_t():
    start = E - 10 * 24 * HOUR
    c = h.parse_candles(fixture_candles(start, 10 * 24 + 48))
    f = h.parse_funding_history(fixture_funding(start, 10 * 24 + 48))
    a = h.features_at(c, f, E)
    future_changed_c = h.parse_candles(fixture_candles(start, 10 * 24, price=84000) + fixture_candles(start + 10 * 24 * HOUR, 48, price=1.0))
    b = h.features_at(future_changed_c, f, E - 10 * 24 * HOUR + 10 * 24 * HOUR)
    assert a["status"] == "OK" and a["last_candle_close"] < E and a["last_funding_t"] <= E
    assert b["price"] != 1.0                                       # the changed future is invisible at t


def test_stale_or_missing_data_is_reported_not_filled():
    c = h.parse_candles(fixture_candles(0, 10))
    f = h.parse_funding_history(fixture_funding(0, 10))
    assert h.features_at(c, f, 20 * HOUR)["status"] == "STALE_OR_MISSING"
    assert h.features_at([], f, 5 * HOUR)["status"] == "STALE_OR_MISSING"


def test_floor_detection_matches_v1_floor_constant():
    assert h.at_floor(0.0000125) and not h.at_floor(0.0000126) and not h.at_floor(-0.0000125)


def test_baseline_dimensions_need_a_full_week():
    start = E - 3 * 24 * HOUR
    f = h.features_at(h.parse_candles(fixture_candles(start, 3 * 24)), h.parse_funding_history(fixture_funding(start, 3 * 24)), E)
    assert f["status"] == "OK" and "F_premium_pctile_7d" not in f and "H_volume_6h_vs_7d_median" not in f


def test_abnormal_flags_fire_on_a_fixture_shock_only():
    start = E - 10 * 24 * HOUR
    shock = E - 6 * HOUR
    c = h.parse_candles(fixture_candles(start, 10 * 24, crash_at=shock))
    f = h.parse_funding_history(fixture_funding(start, 10 * 24, spike_at=shock))
    quiet = h.abnormal_flags(h.features_at(c, f, shock - 12 * HOUR))
    hit = h.abnormal_flags(h.features_at(c, f, E))
    assert not any(quiet.values())
    assert hit["F_premium_low"] and hit["G_ret_6h_low"] and hit["H_volume_high"]


def test_timing_classification():
    pre, ev = 1000, 2000
    assert h.classify_timing(None, pre, ev) == "UNUSABLE"
    assert h.classify_timing(999, pre, ev) == "PRE-EVENT"
    assert h.classify_timing(1000, pre, ev) == "CONTEMPORANEOUS"
    assert h.classify_timing(2000, pre, ev) == "CONTEMPORANEOUS"
    assert h.classify_timing(2001, pre, ev) == "POST-EVENT"


def test_dimension_registry_covers_a_to_j_and_does_not_claim_liquidations():
    assert {k[0] for k in h.DIMENSIONS} == set("ABCDEFGHIJ")
    assert h.DIMENSIONS["J_liquidations"]["expected"] == "NOT_AVAILABLE"
    assert h.DIMENSIONS["A_oi_level"]["expected"] == "AVAILABLE_LIVE"
    assert h.SOURCE["cost_eur"] == 0 and "not the whole crypto market" in h.SOURCE["venue_scope"]


# ---- runner ----
def _v1_fixture():
    return [{"ts": E + k * HOUR, "fd": 50 if k % 2 else 44, "ls": 48, "hf": 50} for k in (-30, -12, -6, 1, 5, 20)]


def test_runner_failure_is_recorded_without_any_hyperliquid_claim(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("Tunnel connection failed: 403")
    monkeypatch.setattr(run.urllib.request, "urlopen", boom)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    (tmp_path / "v1.json").write_text(json.dumps(_v1_fixture()))
    out = tmp_path / "a.json"
    assert run.main(["--v1", str(tmp_path / "v1.json"), "--cache", str(tmp_path / "c"), "--out", str(out)]) == 2
    a = json.loads(out.read_text())
    assert a["status"] == "LIVE_FETCH_FAILED" and "event15" not in a and a["history"]["measured"] is False
    assert a["v1_only_findings"]["sources"]["funding"]["exactly_50"] == 2      # k = 1 and 5 in the FIXTURE
    assert a["v1_impact"].startswith("NONE")


def test_runner_ok_path_end_to_end_with_fixture_api(tmp_path, monkeypatch):
    start = E - 12 * 24 * HOUR
    shock = E - 5 * HOUR
    candles = fixture_candles(start, 14 * 24, crash_at=shock)
    funding = fixture_funding(start, 14 * 24, spike_at=shock)

    class Resp:
        def __init__(self, data):
            self.data = json.dumps(data).encode()
        def read(self):
            return self.data
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    calls = []

    def fake(req, timeout=30):
        body = json.loads(req.data)
        calls.append(body["type"])
        if body["type"] == "metaAndAssetCtxs":
            return Resp(FIXTURE_META)
        if body["type"] == "fundingHistory":
            rows = [r for r in funding if body["startTime"] <= r["time"] <= body["endTime"]]
            return Resp(rows[:100])                                  # FIXTURE page cap
        if body["type"] == "candleSnapshot":
            q = body["req"]
            return Resp([c for c in candles if q["startTime"] <= c["t"] <= q["endTime"]])
        raise AssertionError(body)

    monkeypatch.setattr(run.urllib.request, "urlopen", fake)
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    (tmp_path / "v1.json").write_text(json.dumps(_v1_fixture()))
    out = tmp_path / "a.json"
    out.write_text(json.dumps({"status": "LIVE_FETCH_FAILED", "error": "403"}))   # earlier failed run
    args = ["--v1", str(tmp_path / "v1.json"), "--cache", str(tmp_path / "c"), "--out", str(out)]
    assert run.main(args) == 0
    a = json.loads(out.read_text())
    assert a["status"] == "OK" and a["previous_runs"] == [{"status": "LIVE_FETCH_FAILED", "error": "403"}]
    assert a["live_snapshot"]["open_interest"] == 25000.5
    assert a["data"]["funding_records"] > 100 and a["data"]["requests"]["fundingHistory_FETCHED"] > 1   # pagination crossed the FIXTURE page cap
    assert a["history"]["v1_observations"] == 6 and a["history"]["usable"] >= 4
    timing = a["event15"]["flag_timing"]
    assert timing["F_premium_low"]["classification"] == "CONTEMPORANEOUS"   # the FIXTURE shock starts after the boundary
    # rerun: all from cache, byte-identical apart from the audit trail
    n = len(calls)
    assert run.main(args) == 0 and len(calls) == n
    b = json.loads(out.read_text())
    assert {k: v for k, v in b.items() if k not in ("previous_runs", "data")} == {k: v for k, v in a.items() if k not in ("previous_runs", "data")}


def test_event_timing_ignores_episode_that_cleared_before_boundary(monkeypatch):
    """FIXTURE flags: an early 3h episode that clears before the pre-event boundary, then one starting after it.
    The early episode could not have warned the first failed prediction, so the flag is not PRE-EVENT."""
    e = run.EVENT15["event_ts_ms"]
    early = range(e - 31 * run.HOUR, e - 28 * run.HOUR, run.HOUR)

    def fake_features(candles, funding, t):
        on = any(abs(t - x) < run.HOUR / 2 for x in early) or t >= e - run.HOUR
        return {"status": "OK", "t": str(t), "flag": on}

    monkeypatch.setattr(run.h, "features_at", fake_features)
    monkeypatch.setattr(run.h, "abnormal_flags", lambda f: {"F_premium_low": f["flag"]})
    t = run.analyse_event([], [])["flag_timing"]["F_premium_low"]
    assert t["active_at_pre_boundary"] is False
    assert t["classification"] == "CONTEMPORANEOUS"
    assert [ep["class"] for ep in t["episodes"]] == ["PRE-EVENT", "CONTEMPORANEOUS"]
    assert t["episodes"][0]["hours"] == 3

    monkeypatch.setattr(run.h, "abnormal_flags", lambda f: {"F_premium_low": True})   # on throughout
    t = run.analyse_event([], [])["flag_timing"]["F_premium_low"]
    assert t["active_at_pre_boundary"] is True and t["classification"] == "PRE-EVENT"
