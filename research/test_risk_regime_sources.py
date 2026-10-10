"""Tests for the Risk Regime €0 source collectors.

Every payload below is a FIXTURE shaped like the provider's documented response, used only to test parsing,
pagination, retry and failure handling. None of it is, or stands in for, real Bybit / Binance / Deribit /
DeFiLlama / GDELT / Hyperliquid data.
"""
import json
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse

import pytest

import risk_regime_data as d
import risk_regime_sources as s

H = d.HOUR
NOW = 1791300000000
T0 = 1790000000000 - 1790000000000 % H


class FakeOpener:
    """Routes URLs to handlers; records calls. Handlers return (status, payload) or raise."""

    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, url):
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        self.calls.append((u.netloc + u.path, q))
        status, payload = self.handler(u.netloc + u.path, q)
        return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()


def http(handler, **kw):
    op = FakeOpener(handler)
    return s.Http(opener=op, sleep=lambda x: None, **kw), op


# ---------------- registry ----------------
def test_registry_metadata_complete_and_honest():
    required = ("provider", "endpoint", "docs", "auth", "cost_eur", "resolution", "rate_limit", "history", "availability_rule", "v1_overlap")
    for sid, m in s.SOURCES.items():
        assert m["dimension"], sid
        if m.get("status") in ("NOT_AVAILABLE", "RESEARCH_REQUIRED"):
            assert m["reason"], sid
            continue
        for f in required:
            assert f in m, (sid, f)
        assert m["cost_eur"] == 0 and m["auth"] == "none", sid
    assert "30 DAYS" in s.SOURCES["binance_oi"]["history"]
    assert "LIVE ONLY" in s.SOURCES["deribit_options_snapshot"]["history"]
    assert "NOT exchange" in s.SOURCES["defillama_stablecoins"]["semantics"]
    for k in ("whale_flows", "miner_flows", "lth_flows", "exchange_flows"):
        assert s.SOURCES[k]["status"] == "NOT_AVAILABLE"
    assert s.SOURCES["liquidations_xoomar"]["status"] == "RESEARCH_REQUIRED"


# ---------------- HTTP behaviour ----------------
def test_http_retries_429_then_succeeds():
    n = Counter()
    def h(path, q):
        n["c"] += 1
        return (429, {"msg": "slow down"}) if n["c"] == 1 else (200, {"ok": True})
    cli, _ = http(h)
    assert cli.get_json("https://x.example/a") == {"ok": True}
    assert cli.log["http_429"] == 1 and cli.log["http_200"] == 1


def test_http_blocked_proxy_is_not_retried_and_is_named():
    def boom(url):
        raise OSError("<urlopen error Tunnel connection failed: 403 Forbidden>")
    cli = s.Http(opener=boom, sleep=lambda x: None)
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://api.bybit.com/v5/x")
    assert e.value.status == "BLOCKED_BY_NETWORK_POLICY" and "api.bybit.com" in str(e.value)


def test_http_provider_geo_block_is_named_and_not_retried():
    body = b'{\n    error:The Amazon CloudFront distribution is configured to block access from your country\n}'
    cli, op = http(lambda p, q: (403, body))
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://api.bybit.com/v5/x")
    assert e.value.status == "BLOCKED_BY_PROVIDER_GEO_RESTRICTION" and "api.bybit.com" in str(e.value)
    assert len(op.calls) == 1
    cli, _ = http(lambda p, q: (451, b'{"code": 0, "msg": "Service unavailable from a restricted location"}'))
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://fapi.binance.com/x")
    assert e.value.status == "BLOCKED_BY_PROVIDER_GEO_RESTRICTION"
    cli, _ = http(lambda p, q: (403, {"err": "forbidden"}))      # a plain 403 stays a provider error
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://x.example/a")
    assert e.value.status == "PROVIDER_ERROR"


def test_http_gdelt_rate_limit_text_is_retried_as_429():
    n = Counter()
    def h(path, q):
        n["c"] += 1
        return (200, b"Please limit requests to one every 5 seconds") if n["c"] == 1 else (200, {"ok": True})
    cli, _ = http(h)
    assert cli.get_json("https://api.gdeltproject.org/api/v2/doc/doc") == {"ok": True} and n["c"] == 2


def test_http_provider_error_and_malformed():
    cli, _ = http(lambda p, q: (404, {"err": 1}))
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://x.example/a")
    assert e.value.status == "PROVIDER_ERROR"
    cli, _ = http(lambda p, q: (200, b"<html>not json</html>"))
    with pytest.raises(s.SourceError) as e:
        cli.get_json("https://x.example/a")
    assert e.value.status == "MALFORMED_RESPONSE"


def test_http_gives_up_after_retries():
    cli, op = http(lambda p, q: (503, {}), retries=2)
    with pytest.raises(s.SourceError, match="after 2 attempts"):
        cli.get_json("https://x.example/a")
    assert len(op.calls) == 2


def test_http_min_interval_throttles():
    slept = []
    cli = s.Http(opener=FakeOpener(lambda p, q: (200, {})), sleep=slept.append, min_interval_s=5.0)
    cli.get_json("https://x.example/a")
    cli.get_json("https://x.example/b")
    assert slept and slept[0] > 4.0


def test_run_collector_turns_failures_into_status():
    def boom(url):
        raise OSError("Tunnel connection failed: 403")
    r = s.run_collector(s.collect_bybit_oi, s.Http(opener=boom, sleep=lambda x: None), T0, T0 + H, NOW)
    assert r["status"] == "BLOCKED_BY_NETWORK_POLICY" and r["observations"] == [] and r["source"] == "bybit_oi"
    cli, _ = http(lambda p, q: (200, {"retCode": 0, "result": {"list": [{"timestamp": "abc", "openInterest": "1"}]}}))
    r = s.run_collector(s.collect_bybit_oi, cli, T0, T0 + H, NOW)
    assert r["status"] == "MALFORMED_RESPONSE"


# ---------------- Bybit ----------------
def test_bybit_oi_pagination_by_cursor():
    pages = {None: ([T0 + 2 * H, T0 + H], "c1"), "c1": ([T0], "")}
    def h(path, q):
        rows, nxt = pages[q.get("cursor")]
        return 200, {"retCode": 0, "retMsg": "OK", "result": {"list": [{"openInterest": "100.5", "timestamp": str(t)} for t in rows], "nextPageCursor": nxt}}
    cli, op = http(h)
    r = s.collect_bybit_oi(cli, T0, T0 + 3 * H, NOW)
    assert r["status"] == "OK" and r["pages"] == 2 and len(r["observations"]) == 3
    o = min(r["observations"], key=lambda x: x["timestamp"])
    assert o["timestamp"] == T0 and o["available_at"] == T0 + H and o["value"] == 100.5 and o["unit"] == "BTC"
    assert o["raw"] == {"openInterest": "100.5", "timestamp": str(T0)}
    assert op.calls[0][1]["intervalTime"] == "1h" and op.calls[1][1]["cursor"] == "c1"


def test_bybit_error_code_is_provider_error():
    cli, _ = http(lambda p, q: (200, {"retCode": 10001, "retMsg": "params error"}))
    with pytest.raises(s.SourceError) as e:
        s.collect_bybit_oi(cli, T0, T0 + H, NOW)
    assert e.value.status == "PROVIDER_ERROR"


def test_bybit_funding_pages_backwards_and_stops():
    ts = [T0 + 8 * H * i for i in range(5)]
    def h(path, q):
        end = int(q["endTime"])
        rows = sorted([t for t in ts if t <= end], reverse=True)[:2]
        return 200, {"retCode": 0, "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": str(t)} for t in rows]}}
    cli, op = http(h)
    r = s.collect_bybit_funding(cli, T0, T0 + 40 * H, NOW)
    assert sorted(o["timestamp"] for o in r["observations"]) == ts
    assert all(o["available_at"] == o["timestamp"] and o["interval"] == "8h" for o in r["observations"])


def test_bybit_empty_response_is_empty_status():
    cli, _ = http(lambda p, q: (200, {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}))
    assert s.collect_bybit_oi(cli, T0, T0 + H, NOW)["status"] == "EMPTY"


# ---------------- Binance ----------------
def test_binance_oi_clipped_to_30_day_retention():
    seen = []
    def h(path, q):
        seen.append(int(q["startTime"]))
        st = int(q["startTime"])
        if st > NOW - 29 * d.DAY:
            return 200, []
        return 200, [{"symbol": "BTCUSDT", "sumOpenInterest": "70000", "sumOpenInterestValue": "5.9e9", "timestamp": st + H - st % H}]
    cli, _ = http(h)
    r = s.collect_binance_oi(cli, NOW - 90 * d.DAY, NOW, NOW)
    assert seen[0] >= NOW - 30 * d.DAY                       # never asks for history it does not keep
    assert "retention" in r["note"]
    assert {o["metric"] for o in r["observations"]} == {"open_interest", "open_interest_usd"}


def test_binance_funding_paginates_forward():
    ts = [T0 + 8 * H * i for i in range(3)]
    def h(path, q):
        st = int(q["startTime"])
        rows = [t for t in ts if t >= st][:2]
        return 200, [{"symbol": "BTCUSDT", "fundingTime": t, "fundingRate": "0.0001", "markPrice": "84000"} for t in rows]
    cli, op = http(h)
    r = s.collect_binance_funding(cli, T0, T0 + 30 * H, NOW)
    assert [o["timestamp"] for o in r["observations"]] == ts and len(op.calls) == 3


def test_binance_malformed_object_instead_of_list():
    cli, _ = http(lambda p, q: (200, {"code": -1121, "msg": "Invalid symbol."}))
    r = s.run_collector(s.collect_binance_funding, cli, T0, T0 + H, NOW)
    assert r["status"] == "MALFORMED_RESPONSE"


# ---------------- Deribit ----------------
def test_deribit_dvol_continuation():
    def h(path, q):
        end = int(q["end_timestamp"])
        if end > T0 + 2 * H:
            return 200, {"result": {"data": [[T0 + 2 * H, 40, 41, 39, 40.5], [T0 + 3 * H, 40.5, 42, 40, 41]], "continuation": T0 + 2 * H}}
        return 200, {"result": {"data": [[T0, 38, 39, 37, 38.5], [T0 + H, 38.5, 40, 38, 40]], "continuation": None}}
    cli, op = http(h)
    r = s.collect_deribit_dvol(cli, T0, T0 + 4 * H, NOW)
    closes = sorted((o["timestamp"], o["value"]) for o in r["observations"] if o["metric"] == "dvol_close")
    assert closes == [(T0, 38.5), (T0 + H, 40.0), (T0 + 2 * H, 40.5), (T0 + 3 * H, 41.0)] and len(op.calls) == 2


def test_deribit_error_payload():
    cli, _ = http(lambda p, q: (200, {"error": {"code": 10009, "message": "bad"}}))
    assert s.run_collector(s.collect_deribit_dvol, cli, T0, T0 + H, NOW)["status"] == "PROVIDER_ERROR"


def test_option_name_and_max_pain():
    assert s.parse_option_name("BTC-27SEP26-80000-C") == ("27SEP26", 80000.0, "C")
    assert s.parse_option_name("BTC-PERPETUAL") is None
    # FIXTURE: big call OI at 90k and put OI at 70k -> max pain sits between them
    assert s.max_pain({70000.0: {"C": 0, "P": 100}, 80000.0: {"C": 10, "P": 10}, 90000.0: {"C": 100, "P": 0}}) == 80000.0
    assert s.max_pain({}) is None


def test_options_snapshot_is_live_only():
    rows = [{"instrument_name": "BTC-27SEP26-80000-C", "open_interest": 30}, {"instrument_name": "BTC-27SEP26-80000-P", "open_interest": 15}]
    cli, _ = http(lambda p, q: (200, {"result": rows}))
    r = s.collect_deribit_options_snapshot(cli, NOW)
    by = {o["metric"]: o for o in r["observations"]}
    assert by["put_call_oi_ratio"]["value"] == 0.5 and by["options_open_interest_total"]["value"] == 45
    assert all(o["historical_or_live"] == "live" and o["available_at"] == NOW for o in r["observations"])


# ---------------- DeFiLlama ----------------
def test_defillama_stablecoins_both_asset_shapes_and_window():
    day = 1790035200                       # FIXTURE date in seconds (00:00 UTC)
    def h(path, q):
        if path.endswith("/stablecoincharts/all") or "/stablecoincharts/" in path:
            return 200, [{"date": str(day), "totalCirculatingUSD": {"peggedUSD": 3.0e11}}, {"date": str(day - 400 * 86400), "totalCirculatingUSD": {"peggedUSD": 1.0}}]
        if path.endswith("/stablecoin/1"):
            return 200, {"tokens": [{"date": day, "circulating": {"peggedUSD": 1.8e11}}]}
        if path.endswith("/stablecoin/2"):
            return 200, {"chainBalances": {"Ethereum": {"tokens": [{"date": day, "circulating": {"peggedUSD": 4e10}}]},
                                           "Solana": {"tokens": [{"date": day, "circulating": {"peggedUSD": 1e10}}]}}}
        raise AssertionError(path)
    cli, _ = http(h)
    r = s.collect_defillama_stablecoins(cli, day * 1000 - 5 * d.DAY, day * 1000 + d.DAY, NOW)
    by = {(o["instrument"]): o for o in r["observations"]}
    assert by["USDT"]["value"] == 1.8e11 and by["USDC"]["value"] == 5e10
    assert by["ALL_STABLECOINS"]["available_at"] == day * 1000 + d.DAY            # complete only after the day ends
    assert all(o["timestamp"] >= day * 1000 - 35 * d.DAY for o in r["observations"])   # 400-day-old row outside the window


def test_defillama_dex_and_tvl():
    day = 1790035200
    def h(path, q):
        if path.endswith("/overview/dexs"):
            return 200, {"totalDataChart": [[day, 2.5e9]]}
        return 200, [{"date": day, "tvl": 9.0e10}]
    cli, _ = http(h)
    dex = s.collect_defillama_dex(cli, day * 1000 - d.DAY, day * 1000 + d.DAY, NOW)
    tvl = s.collect_defillama_tvl(cli, day * 1000 - d.DAY, day * 1000 + d.DAY, NOW, chains=("Ethereum",))
    assert dex["observations"][0]["value"] == 2.5e9
    assert {o["instrument"] for o in tvl["observations"]} == {"ALL_CHAINS", "chain:Ethereum"}


def test_defillama_malformed():
    cli, _ = http(lambda p, q: (200, {"unexpected": True}))
    assert s.run_collector(s.collect_defillama_dex, cli, T0, T0 + H, NOW)["status"] == "MALFORMED_RESPONSE"


# ---------------- GDELT DOC ----------------
def test_gdelt_doc_timeline_and_articles():
    def h(path, q):
        if q["mode"] == "TimelineVolRaw":
            return 200, {"timeline": [{"series": "Article Count", "data": [{"date": "20260927T120000Z", "value": 12, "norm": 30000}]}]}
        return 200, {"articles": [{"url": "https://news.example/a", "title": "FIXTURE headline", "seendate": "20260927T121500Z",
                                   "domain": "news.example", "language": "English", "sourcecountry": "United States"}]}
    cli, op = http(h)
    r = s.collect_gdelt_doc(cli, [(T0, T0 + H)], NOW)
    t = datetime(2026, 9, 27, 12, tzinfo=timezone.utc).timestamp() * 1000
    counts = [o for o in r["observations"] if o["metric"] == "article_count"]
    assert len(counts) == len(s.DOC_TOPICS) and counts[0]["timestamp"] == t
    assert counts[0]["available_at"] == t + 15 * 60_000 + s.DOC_GDELT_LAG
    a = r["articles"][0]
    assert a["first_seen"] == t + 15 * 60_000 and a["available_at"] == a["first_seen"] + s.DOC_GDELT_LAG and a["domain"] == "news.example"
    assert len(op.calls) == 2 * len(s.DOC_TOPICS)
    assert op.calls[0][1]["STARTDATETIME"] == datetime.fromtimestamp(T0 / 1000, timezone.utc).strftime("%Y%m%d%H%M%S")


def test_gdelt_doc_outside_search_window_is_empty_not_filled():
    cli, _ = http(lambda p, q: (200, {}))
    r = s.collect_gdelt_doc(cli, [(T0, T0 + H)], NOW)
    assert r["status"] == "EMPTY" and r["observations"] == [] and r["articles"] == []


def test_gdelt_doc_failed_request_is_a_named_gap_not_a_total_loss():
    def h(path, q):
        if "bitcoin" in q["query"] and q["mode"] == "ArtList":
            return 429, b"Please limit requests to one every 5 seconds"
        return 200, {"timeline": [{"series": "Article Count", "data": [{"date": "20260927T120000Z", "value": 1}]}]} if q["mode"] == "TimelineVolRaw" else {}
    cli, _ = http(h, retries=2)
    r = s.collect_gdelt_doc(cli, [(T0, T0 + H)], NOW)
    assert r["status"] == "PARTIAL" and len(r["observations"]) == len(s.DOC_TOPICS)
    assert [(f["topic"], f["mode"]) for f in r["failed_requests"]] == [("crypto", "ArtList")]
    cli, _ = http(lambda p, q: (429, b"Please limit requests"), retries=1)
    assert s.run_collector(s.collect_gdelt_doc, cli, [(T0, T0 + H)], NOW)["status"] == "PROVIDER_ERROR"


def test_http_cache_resumes_with_original_retrieval_time(tmp_path):
    n = Counter()
    def h(path, q):
        n["c"] += 1
        return 200, {"ok": n["c"]}
    cli, _ = http(h, cache_dir=tmp_path)
    assert cli.get_json("https://x.example/a", {"q": 1}) == {"ok": 1}
    first = cli.last_retrieved_at
    cli2, op2 = http(h, cache_dir=tmp_path)
    assert cli2.get_json("https://x.example/a", {"q": 1}) == {"ok": 1} and op2.calls == []
    assert cli2.last_retrieved_at == first and cli2.log["cached"] == 1
    cli3, _ = http(lambda p, q: (429, {}), cache_dir=tmp_path / "x", retries=1)
    with pytest.raises(s.SourceError):
        cli3.get_json("https://x.example/b")
    assert not (tmp_path / "x").exists()                           # failures are never cached


def test_http_backoff_cap():
    slept = []
    cli = s.Http(opener=FakeOpener(lambda p, q: (429, {})), sleep=slept.append, retries=5, max_backoff_s=10)
    with pytest.raises(s.SourceError):
        cli.get_json("https://x.example/a")
    assert max(slept) == 10


# ---------------- reused sources ----------------
def test_hyperliquid_obs_available_after_close():
    c = {"t": T0, "T": T0 + H - 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 2.0, "v": 3.0, "n": 4}
    o = s.hyperliquid_obs({"xyz:GOLD": [c]}, NOW)
    assert {x["metric"]: x["value"] for x in o} == {"close": 2.0, "volume": 3.0}
    assert all(x["available_at"] == T0 + H for x in o)


def test_gdelt_event_record_keeps_goldstein_sign_only():
    class E:  # FIXTURE event row
        event_id, sql_date, date_added, root, code, quad, goldstein = 1, "20260927", "20260927120000", "19", "190", 4, -10.0
        mentions, sources, articles = 30, 5, 30
        actor1_country, actor2_country, actor1_type, actor2_type = "ISR", "IRN", "MIL", ""
        action_country, action_name, url = "IR", "Tehran, Iran", "https://news.example/x"
    class C:
        category, escalation, corridor, major_power = "fight", True, True, False
    b = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    r = s.gdelt_event_record(E, b, C, NOW)
    assert r["goldstein_sign"] == -1 and "goldstein" not in {k for k in r if k != "goldstein_sign"}
    assert r["first_seen"] == int(b.timestamp() * 1000) and r["available_at"] == r["first_seen"] + 15 * 60_000
    assert r["event_date"] == "20260927"
