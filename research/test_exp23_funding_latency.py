"""Latency / coverage report for the hourly intraday funding collection. Mocked OKX provider, TEST data only."""
import early_warning_12h as ew
import exp23_funding_latency as lat
import risk_regime_forward as fw
from test_risk_regime_forward import fetcher, okx_http, okx_rec

H = ew.H


def test_report_measures_latency_missed_runs_and_fresh_hours_without_reading_the_sealed_period(tmp_path, monkeypatch):
    day0 = ew.T1 + 2 * H                                              # 2026-10-10 00:00, an OKX settlement
    rows = [okx_rec(ts) for ts in range(day0 - 40 * 24 * H, day0 + 24 * H + 1, 8 * H)]
    missed = day0 + 8 * H + 17 * 60_000                               # the 08:17 run never happens
    runs = [day0 + k * H + 17 * 60_000 for k in range(25) if day0 + k * H + 17 * 60_000 != missed]
    for now in runs:
        monkeypatch.setattr(fw.s.time, "time", lambda now=now: now / 1000)   # the run's wall clock = its start
        fw.run_collection(tmp_path, ["okx_btc_funding"], now=now, max_days=40, cutoffs={}, fetch=fetcher({}),
                          okx_http=okx_http([r for r in rows if int(r["fundingTime"]) < now]), open_day=True)
    rep = lat.report(str(tmp_path), day0 + 24 * H + 30 * 60_000)
    assert rep["runs"]["hourly_runs"] == 24 and rep["runs"]["expected_hours"] == 25 and rep["runs"]["missed_hours"] == 1
    assert rep["runs"]["start_delay_ms"]["max"] == 0
    okx = rep["sources"]["OKX"]
    assert okx["settlements"] == 4                                    # 10-10 00/08/16 and 10-11 00; nothing before T1
    assert okx["latency_ms"]["p50"] == 17 * 60_000 and okx["latency_ms"]["max"] == 77 * 60_000
    assert okx["share_within_1h"] == 3 / 4 and okx["share_within_staleness_limit"] == 1.0
    assert okx["decision_hours"] == 24 and okx["fresh_hours"] == 23  # 09:00: 00:00 is 9 h old, 08:00 seen at 09:17
    assert okx["u_computable_hours"] == 23
    assert rep["sources"]["HL"]["settlements"] == 0 and rep["sources"]["HL"]["decision_hours"] == 0
