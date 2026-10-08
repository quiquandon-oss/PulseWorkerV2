import risk_regime_event_research as er

H = er.HOUR


def obs(src, inst, metric, ts, value, avail, raw=None):
    return {"source": src, "instrument": inst, "metric": metric, "timestamp": ts, "value": value, "available_at": avail, "raw": raw or {}}


def hourly(inst, n, value_fn, t0=0):
    return [obs("hyperliquid_hip3", inst, "close", t0 + k * H, value_fn(k), t0 + (k + 1) * H) for k in range(n)]


def test_asof_never_reads_ahead():
    p = er.PIT(hourly("BTC", 10, lambda k: 100.0 + k))
    assert p.asof("hyperliquid_hip3|BTC|close", 5 * H)["value"] == 104.0       # candle 4 closes at 5h
    assert p.asof("hyperliquid_hip3|BTC|close", 5 * H - 1)["value"] == 103.0
    assert p.asof("hyperliquid_hip3|BTC|close", 0) is None


def test_abnormal_uses_trailing_history_only_and_existing_convention():
    n = 9 * 24
    rows = hourly("BTC", n, lambda k: 100.0 * (1.0 + 0.001 * (k % 2)))              # flat, tiny oscillation
    rows += [obs("hyperliquid_hip3", "BTC", "close", n * H, 120.0, (n + 1) * H)]  # one jump, published at n+1
    p = er.PIT(rows)
    v1 = er.V1([])
    before = er.assess(p, v1, "BTC.chg24", n * H)            # the jump is not yet available
    after = er.assess(p, v1, "BTC.chg24", (n + 1) * H)
    assert before["abnormal"] is False and after["abnormal"] is True and after["pct_rank"] > er.HI_PCT
    assert er.percentile_rank.__module__ == "hyperliquid_leverage_stress"


def test_insufficient_history_is_inconclusive_not_filled():
    p = er.PIT(hourly("BTC", 30, lambda k: 100.0 + k))
    a = er.assess(p, er.V1([]), "BTC.chg24", 30 * H)
    assert a["abnormal"] is None


def test_dimension_flag_any_measure_and_none_when_unknown():
    ms = {m: {"abnormal": None} for m in er.all_measures()}
    ms["GOLD.chg6"] = {"abnormal": True}
    ms["SILVER.chg24"] = {"abnormal": False}
    fl = er.dim_flags(ms)
    assert fl[6] is True and fl[7] is False and fl[8] is None


def test_event15_boundary_is_locked():
    assert er.EVENT15["boundary_ms"] == 1790510487481                         # 2026-09-27 12:01:27 UTC, PRED-1125
    eps = er.episode_boundaries([{"id": "RE-15", "type": "V2_FAILURE_CLUSTER", "subtype": "BTC_24h", "anchor_ts": 1790564484213}])
    assert eps[0]["boundary_ms"] == 1790510487481 and eps[0]["boundary_rule"].startswith("LOCKED")


def test_no_score_or_weights_in_output_contract():
    assert not any(w in er.DIMENSIONS[d][0].lower() for d in er.DIMENSIONS for w in ("score ", "weight"))
