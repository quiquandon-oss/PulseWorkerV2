"""OKX BTC-USDT-SWAP funding: normaliser and paginated collector. Synthetic TEST data and a mocked opener only:
no live API call is made anywhere in this file."""
import json
import urllib.parse
from collections import Counter

import pytest

import risk_regime_sources as s

H = 3_600_000
T0 = 1_791_590_400_000          # 2026-10-10T00:00:00Z (an OKX settlement time)


def rec(ts, rate="0.0001", realized="0.0001", inst="BTC-USDT-SWAP"):
    r = {"instId": inst, "instType": "SWAP", "fundingTime": str(ts), "fundingRate": rate, "formulaType": "withRate", "method": "current_period"}
    if realized is not None:
        r["realizedRate"] = realized
    return r


def okx_body(rows, code="0"):
    return json.dumps({"code": code, "msg": "", "data": rows}).encode()


class FakeOkx:
    """Serves newest-first pages of `rows` honouring `after` (= records older than the cursor)."""

    def __init__(self, rows, limit=100, script=None):
        self.rows = sorted(rows, key=lambda r: -int(r["fundingTime"]))
        self.limit = limit
        self.script = list(script or [])     # optional leading responses (status, body) before real pages
        self.calls = []

    def __call__(self, url, timeout=30):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.calls.append(q)
        if self.script:
            return self.script.pop(0)
        assert q["instId"] == "BTC-USDT-SWAP" and int(q["limit"]) <= 100
        after = int(q["after"]) if "after" in q else None
        page = [r for r in self.rows if after is None or int(r["fundingTime"]) < after][: self.limit]
        return 200, okx_body(page)


def http(opener):
    return s.Http(opener=opener, sleep=lambda _x: None, retries=3, log=Counter())


def settlements(n, step=8 * H, start=T0):
    return [rec(start - i * step, rate=f"{0.0001 + i * 1e-6:.7f}", realized=f"{0.0001 + i * 1e-6:.7f}") for i in range(n)]


# ------------------------------------------------------------------------------------------------- pagination
def test_paginates_backwards_with_after_until_the_window_start():
    rows = settlements(250)
    op = FakeOkx(rows)
    start = T0 - 200 * 8 * H
    res = s.collect_okx_funding(http(op), start, T0 + 1, now_fn=lambda: T0 + 5 * 60_000)
    assert [c.get("after") for c in op.calls] == [None, str(T0 - 99 * 8 * H), str(T0 - 199 * 8 * H)]
    assert res["pages"] == 3 and res["reached_start"] is True
    ts = [int(r["fundingTime"]) for r in res["rows"]]
    assert ts == sorted(ts) and ts[0] == start and ts[-1] == T0 and len(ts) == 201     # only [start, end)
    assert res["irregular_spacing_ms"] == []


def test_stops_on_an_empty_page_and_reports_provider_history_limit():
    op = FakeOkx(settlements(30))
    res = s.collect_okx_funding(http(op), T0 - 100 * 8 * H, T0 + 1, now_fn=lambda: T0)
    assert res["reached_start"] is False and len(res["rows"]) == 30 and res["pages"] == 2


def test_a_page_not_older_than_the_cursor_fails_closed():
    class Broken(FakeOkx):
        def __call__(self, url, timeout=30):
            self.calls.append(url)
            return 200, okx_body(settlements(100))           # ignores `after`: same page forever
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(Broken([])), T0 - 400 * 8 * H, T0 + 1)
    assert e.value.status == "PAGINATION_ERROR"


def test_same_fundingTime_with_different_content_is_malformed():
    class Twice(FakeOkx):
        def __call__(self, url, timeout=30):
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            if "after" not in q:
                return 200, okx_body([rec(T0), rec(T0 - 8 * H)])
            return 200, okx_body([rec(T0 - 8 * H, rate="0.0009")]) if int(q["after"]) > T0 - 16 * H else (200, okx_body([]))
    # second page repeats T0-8h with another value but is not older than the cursor -> fails closed either way
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(Twice([])), T0 - 30 * 8 * H, T0 + 1)
    assert e.value.status in ("PAGINATION_ERROR", "MALFORMED_RESPONSE")


# --------------------------------------------------------------------------------- rate limits, errors, payloads
def test_http_429_and_okx_rate_limit_code_are_retried():
    op = FakeOkx(settlements(5), script=[(429, b"{}"), (200, okx_body([], code="50011"))])
    h = http(op)
    res = s.collect_okx_funding(h, T0 - 10 * 8 * H, T0 + 1)
    assert len(res["rows"]) == 5
    assert h.log["http_429"] == 1 and h.log["okx_rate_limited"] == 1


def test_rate_limit_that_never_clears_is_an_error_not_an_empty_result():
    op = FakeOkx([], script=[(200, okx_body([], code="50011"))] * 10)
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(op), T0 - 8 * H, T0 + 1)
    assert e.value.status == "PROVIDER_ERROR"


def test_404_and_provider_error_codes_become_statuses():
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(FakeOkx([], script=[(404, b"not found")])), T0 - 8 * H, T0 + 1)
    assert e.value.status == "PROVIDER_ERROR"
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(FakeOkx([], script=[(200, okx_body([], code="51001"))])), T0 - 8 * H, T0 + 1)
    assert e.value.status == "PROVIDER_ERROR" and "51001" in str(e.value)


def test_geo_restriction_is_reported_as_such():
    op = FakeOkx([], script=[(403, b"<html>We block access from your country</html>")])
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(op), T0 - 8 * H, T0 + 1)
    assert e.value.status == "BLOCKED_BY_PROVIDER_GEO_RESTRICTION"


@pytest.mark.parametrize("payload", [
    {"data": []},                                                        # no code
    {"code": "0", "msg": "", "data": {"x": 1}},                         # data not a list
    {"code": "0", "data": [rec(T0, inst="ETH-USDT-SWAP")]},             # other instrument
    {"code": "0", "data": [rec(T0, rate="abc")]},                       # non-numeric rate
    {"code": "0", "data": [rec(T0, rate="nan")]},                       # NaN
    {"code": "0", "data": [dict(rec(T0), fundingTime="1791590400.5")]}, # not integer ms
    {"code": "0", "data": [dict(rec(T0), fundingTime="1791590")]},      # seconds, not ms
    {"code": "0", "data": [{k: v for k, v in rec(T0).items() if k != "fundingRate"}]},
])
def test_malformed_payloads_are_rejected(payload):
    with pytest.raises(s.SourceError) as e:
        s.okx_funding_rows(payload)
    assert e.value.status == "MALFORMED_RESPONSE"


def test_non_json_body_is_malformed():
    with pytest.raises(s.SourceError) as e:
        s.collect_okx_funding(http(FakeOkx([], script=[(200, b"<html>maintenance</html>")])), T0 - 8 * H, T0 + 1)
    assert e.value.status == "MALFORMED_RESPONSE"


# ------------------------------------------------------------------------------------------------- normaliser
def test_normaliser_timestamps_rates_provenance_and_observation_time():
    rows = [rec(T0 - 8 * H, rate="0.00012", realized="0.000119"), rec(T0, rate="-0.00003", realized=None)]
    obs = s.okx_funding_obs(rows, retrieved_at=T0 + 60_000, first_observed_at=T0 + 90_000)
    by = {(o["timestamp"], o["metric"]): o for o in obs}
    assert set(by) == {(T0 - 8 * H, "funding_rate"), (T0 - 8 * H, "realized_rate"), (T0, "funding_rate")}
    o = by[(T0 - 8 * H, "funding_rate")]
    assert o["source"] == "okx_btc_funding" and o["instrument"] == "BTC-USDT-SWAP" and o["provider"] == "OKX"
    assert o["value"] == pytest.approx(0.00012) and isinstance(o["timestamp"], int)
    assert o["available_at"] == T0 - 8 * H                     # provider settlement time (never claimed earlier)
    assert o["first_observed_at"] == T0 + 90_000 and o["retrieved_at"] == T0 + 60_000
    assert o["source_url"].endswith("funding-rate-history?instId=BTC-USDT-SWAP")
    assert o["raw"]["formulaType"] == "withRate" and o["raw"]["method"] == "current_period" and o["raw"]["instType"] == "SWAP"
    assert by[(T0 - 8 * H, "realized_rate")]["value"] == pytest.approx(0.000119)
    assert by[(T0, "funding_rate")]["value"] == pytest.approx(-0.00003)


def test_interval_irregularity_is_recorded_not_assumed_away():
    rows = [rec(T0 - 16 * H), rec(T0 - 8 * H), rec(T0 - 4 * H), rec(T0)]          # OKX switched to 4h
    obs = [o for o in s.okx_funding_obs(rows, T0, T0) if o["metric"] == "funding_rate"]
    assert [o["raw"]["spacing_ms"] for o in obs] == [None, 8 * H, 4 * H, 4 * H]
    assert [o["raw"]["spacing_irregular"] for o in obs] == [None, False, True, True]
    res = s.collect_okx_funding(http(FakeOkx(rows)), T0 - 20 * H, T0 + 1)
    assert res["irregular_spacing_ms"] == [4 * H]


def test_first_observed_at_is_taken_after_the_data_arrived():
    clock = {"t": T0}
    class Slow(FakeOkx):
        def __call__(self, url, timeout=30):
            clock["t"] += 10_000                                  # each request takes 10 s
            return super().__call__(url, timeout)
    res = s.collect_okx_funding(http(Slow(settlements(3))), T0 - 10 * 8 * H, T0 + 1, now_fn=lambda: clock["t"])
    assert res["first_observed_at"] == T0 + 20_000               # after both requests


def test_registry_entry_documents_availability_and_limits():
    e = s.SOURCES["okx_btc_funding"]
    assert e["auth"] == "none" and "funding-rate-history" in e["endpoint"] and "first_observed_at" in e["availability_rule"]
