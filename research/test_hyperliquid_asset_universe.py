"""Tests for the Hyperliquid asset-universe audit.

Every payload below is a FIXTURE: synthetic data shaped like the Info API responses, used only to test
classification, point-in-time access and the runner. None of it is, or stands in for, real Hyperliquid data.
"""
import json
from datetime import datetime, timezone

import pytest

import hyperliquid_asset_universe as u
import hyperliquid_asset_universe_run as run

H = u.HOUR


def candle(t, c, v=1.0, o=None):
    return {"t": t, "T": t + H - 1, "o": c if o is None else o, "h": c, "l": c, "c": c, "v": v, "n": 1}


def series(start, closes, v=1.0):
    return [candle(start + i * H, c, v) for i, c in enumerate(closes)]


# ---------------- classification ----------------
@pytest.mark.parametrize("desc,cat,expected", [
    ("GOLD references the USD spot price of 1 troy ounce of gold (XAU/USD).", "commodities", "GOLD"),
    ("BRENTOIL references the USD price of 1 barrel of Brent crude oil.", "commodities", "OIL / ENERGY"),
    ("10Y tracks the yield on the most recently issued (on-the-run) 10-year US Treasury note", "rates", "INTEREST RATE / BOND"),
    ("EURUSD references the EUR/USD spot exchange rate", "fx", "FX / USD"),
    ("S&P500 references the S&P 500 Index", "indices", "EQUITY INDEX"),
    ("COPPER references the USD price of 1 pound of high-grade copper.", "commodities", "COMMODITY"),
    ("BTC.D tracks Bitcoin's percentage share of total crypto market capitalization", "crypto", "CRYPTO"),
    ("ANSEM is a community-driven memecoin on Solana", "crypto", "CRYPTO"),
    ("STRC references 1 share of Strategy Inc's Perpetual Stretch Preferred Stock; bitcoin as its primary treasury asset", "stocks", "UNKNOWN"),
])
def test_classify_uses_the_deployers_description(desc, cat, expected):
    assert u.classify("x:Y", {"description": desc, "category": cat}, cat) == expected


def test_unannotated_asset_is_unknown_not_guessed_from_ticker():
    assert u.classify("mkts:BVIV", None, None) == "UNKNOWN"       # FIXTURE: no annotation -> no guess
    assert u.classify("xyz:GOLDISH", {}, None) == "UNKNOWN"


def test_instrument_type_never_calls_hip3_tokenized():
    assert u.instrument_type("xyz:GOLD", "xyz") == u.HIP3_SYNTHETIC
    assert u.instrument_type("PAXG", None) == u.NATIVE_PERP
    assert "TOKENIZED" not in u.HIP3_SYNTHETIC


def test_price_consistency():
    assert u.price_consistency(0.37, 4159.0) == "NOT CONSISTENT WITH NAMED UNDERLYING"
    assert u.price_consistency(4150.0, 4159.0) == "CONSISTENT"
    assert u.price_consistency(None, 4159.0) == "NOT CHECKABLE"


# ---------------- point-in-time ----------------
def test_last_closed_never_uses_an_open_candle():
    cs = series(0, [1, 2, 3])
    assert u.last_closed(cs, H - 1) is None                 # first candle closes at H-1: not strictly before
    assert u.last_closed(cs, H)["c"] == 1
    assert u.last_closed(cs, 3 * H)["c"] == 3


def test_return_features_and_staleness():
    cs = series(0, [100.0] * 200 + [110.0])
    r6 = u.hourly_returns(cs, 6)
    f = u.return_features(cs, 201 * H, r6)
    assert f["status"] == "OK" and f["price"] == 110.0
    assert f["ret_6h"] == pytest.approx(10.0) and f["ret_6h_pctile_7d"] == 100.0
    assert u.flags(f) == {"ret_6h_low": False, "ret_6h_high": True}
    stale = u.return_features(cs, 201 * H + 3 * H, r6)
    assert stale["status"] == "STALE"
    assert u.return_features(cs, 0, r6)["status"] == "NO_DATA"


def test_ret_pct_requires_a_real_predecessor():
    cs = series(0, [100.0, 101.0])
    assert u.ret_pct(cs, 2 * H, 24) is None


def test_market_hours_profile_splits_weekend():
    sat = int(datetime(2026, 9, 26, tzinfo=timezone.utc).timestamp() * 1000)    # a Saturday
    mon = int(datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp() * 1000)
    cs = [candle(sat, 5.0, v=0.0), candle(sat + H, 5.0, v=0.0)] + [candle(mon, 5.0, v=1.0, o=4.9)]
    p = u.market_hours_profile(cs)
    assert p["weekend"]["flat_share"] == 1.0 and p["weekend"]["zero_volume_share"] == 1.0
    assert p["weekday"]["flat_share"] == 0.0 and p["gaps_over_1h"] == 1


def test_coverage_counts_missing_and_stale():
    cs = series(0, [100.0] * 300)
    r6 = u.hourly_returns(cs, 6)
    c = u.coverage(cs, [10 * H, 200 * H, 1000 * H], r6)
    assert c["usable"] == 2 and c["missing"] == 1 and c["status"]["STALE"] == 1
    assert c["usable_with_7d_baseline"] == 1                 # only the 200h point has a 7-day baseline


# ---------------- Event #15 ----------------
def test_event_episode_cleared_before_boundary_is_not_pre_event():
    """FIXTURE: a dip 30h before the event that recovers, then a fall into the event."""
    e = 400 * H + 30 * 60_000
    b = e - 15 * H
    closes = [100.0] * 430
    for k in range(366, 370):
        closes[k] = 90.0               # early dip, recovered by k = 370 (well before the boundary at ~k = 385)
    for k in range(396, 430):
        closes[k] = 80.0               # the fall that starts after the boundary
    cs = series(0, closes)
    r6 = u.hourly_returns(cs, 6)
    ev = u.event_analysis(cs, r6, e, b)
    low = ev["flag_timing"]["ret_6h_low"]
    assert low["active_at_boundary"] is False
    assert low["classification"] == "CONTEMPORANEOUS"
    assert [ep["class"] for ep in low["episodes"]][0] == "PRE-EVENT"
    assert ev["overall"] == "CONTEMPORANEOUS"
    assert ev["change_boundary_to_event_pct"] == pytest.approx(-20.0)


def test_event_unusable_without_data():
    ev = u.event_analysis([], {}, 400 * H, 390 * H)
    assert ev["overall"] == "UNUSABLE" and ev["usable"] is False


# ---------------- cross-asset ----------------
def test_joint_condition_counts_and_distinct_days():
    day = 86_400_000
    btc = [{"ret_6h_pctile_7d": p} for p in (1, 1, 1, 50, 50, None)]
    oth = [{"ret_6h_pctile_7d": p} for p in (2, 3, 60, 1, 50, 1)]
    j = u.joint_condition(btc, oth, "down", "down", [0, 60_000, day, 2 * day, 3 * day, 4 * day])
    assert j["observations_both_measurable"] == 5
    assert (j["btc_abnormal"], j["other_abnormal"], j["both"], j["both_distinct_days"]) == (3, 3, 2, 1)
    assert j["both_if_independent"] == pytest.approx(1.8)


def test_spearman():
    assert u.spearman(list(range(20)), list(range(20))) == 1.0
    assert u.spearman(list(range(20)), list(range(20))[::-1]) == -1.0
    assert u.spearman([1, 2], [1, 2]) is None


# ---------------- runner (FIXTURE API) ----------------
def _fixture_api(v1_ts):
    start = v1_ts[0] - 12 * u.DAY

    def hourly(coin):
        drift = {"BTC": 0.001, "xyz:GOLD": -0.0005}.get(coin, 0.0)
        return [{"t": start + i * H, "T": start + i * H + H - 1, "o": "1", "h": "1", "l": "1",
                 "c": str(100 * (1 + drift) ** i + (i % 5) * 0.01), "v": "1", "n": 1} for i in range(24 * 30)]

    def respond(body):
        t = body["type"]
        if t == "perpDexs":
            return [None, {"name": "xyz", "fullName": "XYZ", "deployer": "0xD", "oracleUpdater": None}]
        if t == "perpConciseAnnotations":
            return [["xyz:GOLD", {"category": "commodities"}], ["xyz:TSLA", {"category": "stocks"}]]
        if t == "metaAndAssetCtxs":
            ctx = {"markPx": "1", "oraclePx": "1", "midPx": "1", "openInterest": "5", "funding": "0", "dayNtlVlm": "9"}
            if body.get("dex") == "xyz":
                return [{"universe": [{"name": "xyz:GOLD"}, {"name": "xyz:TSLA"}, {"name": "xyz:DXY", "isDelisted": True}]},
                        [dict(ctx, markPx="4159"), ctx, ctx]]
            return [{"universe": [{"name": "BTC"}, {"name": "SPX"}]}, [ctx, ctx]]
        if t == "perpAnnotation":
            return {"category": "commodities", "description": "GOLD references the USD spot price of 1 troy ounce of gold (XAU/USD)."}
        if t == "candleSnapshot":
            q = body["req"]
            rows = hourly(q["coin"]) if q["interval"] == "1h" else hourly(q["coin"])[::24]
            return [r for r in rows if q["startTime"] <= r["t"] <= q["endTime"]]
        if t == "fundingHistory":
            return [{"coin": body["coin"], "fundingRate": "0.0000125", "premium": "0", "time": body["startTime"]}]
        if t == "spotMetaAndAssetCtxs":
            return [{"tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "XAUT0", "fullName": "XAUT0"}],
                     "universe": [{"name": "@1", "tokens": [1, 0]}]}, [{"markPx": "0.37", "dayNtlVlm": "17"}]]
        raise AssertionError(body)
    return respond


def test_runner_end_to_end_with_fixture_api(tmp_path, monkeypatch):
    v1_ts = [run.EVENT15["event_ts_ms"] - k * 3 * H for k in range(40, 0, -1)]
    calls = []
    respond = _fixture_api(v1_ts)
    monkeypatch.setattr(run, "post", lambda body, cache: (calls.append(body) or respond(body), "FETCHED"))
    monkeypatch.setattr(run.time, "sleep", lambda s: None)
    (tmp_path / "v1.json").write_text(json.dumps([{"ts": t} for t in v1_ts]))
    out = tmp_path / "a.json"
    assert run.main(["--v1", str(tmp_path / "v1.json"), "--cache", str(tmp_path / "c"), "--out", str(out)]) == 0
    a = json.loads(out.read_text())
    syms = {c["symbol"]: c for c in a["candidates"]}
    assert set(syms) == {"xyz:GOLD", "SPX"}                      # TSLA (single stock) is not a macro candidate
    gold = syms["xyz:GOLD"]
    assert gold["category"] == "GOLD" and gold["instrument_type"] == u.HIP3_SYNTHETIC
    assert gold["v1_overlap"]["v1_source"] == "gold"
    assert gold["coverage_575"]["usable"] == len(v1_ts)
    assert gold["history"]["open_interest_history"] is False and gold["history"]["funding_history_available"] is True
    assert "cross_asset" in gold and "cross_asset" not in syms["SPX"]
    assert syms["SPX"]["category"] == "CRYPTO"                    # the ticker is not the S&P 500
    assert a["inventory"]["delisted_macro_symbols"] == [] and a["inventory"]["delisted_perps"] == 1
    assert a["spot_rwa_tokens"][0]["price_check"] == "NOT CONSISTENT WITH NAMED UNDERLYING"
    assert not any(b["type"] not in a["api"]["documented_requests"] + a["api"]["undocumented_requests_used"] for b in calls)


def test_runner_failure_makes_no_claims(tmp_path, monkeypatch):
    def boom(body, cache):
        raise RuntimeError("Tunnel connection failed: 403")      # FIXTURE network failure
    monkeypatch.setattr(run, "post", boom)
    (tmp_path / "v1.json").write_text(json.dumps([{"ts": 1790000000000}]))
    out = tmp_path / "a.json"
    assert run.main(["--v1", str(tmp_path / "v1.json"), "--cache", str(tmp_path / "c"), "--out", str(out)]) == 2
    a = json.loads(out.read_text())
    assert a["status"] == "LIVE_FETCH_FAILED" and "candidates" not in a and "inventory" not in a
