"""
OFFLINE SELF-TEST ONLY. Plausibly-shaped stand-ins for the public APIs the pinned CryptoPulse page reads, so the
harness can be exercised with no network at all (CI, or a container without egress). Values are fixed test
numbers, not market data. A capture produced with these is stamped fixture_mode=true and collector.py refuses to
ingest it; nothing here is ever written to any database.
"""
import json
import time
import urllib.parse

DAY_MS = 86400000


def _json(obj, status=200):
    return status, "application/json", json.dumps(obj)


def public_responder(method, url, post_data):
    parsed = urllib.parse.urlsplit(url)
    host, path = parsed.hostname, parsed.path
    if host == "api.hyperliquid.xyz":
        body = json.loads(post_data or "{}")
        if body.get("dex") == "xyz":
            names = [("xyz:BRENTOIL", "70.5", "70.0"), ("xyz:EUR", "1.171", "1.169"), ("xyz:GOLD", "2650", "2640")]
        else:
            names = [(s, p, p) for s, p in (("BTC", "64000"), ("ETH", "2500"), ("HYPE", "30"), ("SOL", "150"),
                                            ("LINK", "12"))]
        universe = [{"name": n} for n, _, _ in names]
        ctxs = [{"markPx": m, "prevDayPx": p, "funding": "0.0000125", "openInterest": "1000", "oraclePx": m}
                for _, m, p in names]
        return _json([{"universe": universe}, ctxs])
    if host == "api.alternative.me":
        return _json({"data": [{"value": "54", "value_classification": "Neutral"}]})
    if host == "api.coingecko.com":
        if path.endswith("/global"):
            return _json({"data": {"market_cap_change_percentage_24h_usd": 1.2, "total_market_cap": {"usd": 3.1e12}}})
        if "/market_chart" in path:
            now = int(time.time() * 1000)
            prices = [[now - (365 - i) * DAY_MS, 50000 + i * 40 + (i % 7) * 120] for i in range(366)]
            vols = [[t, 2.5e10] for t, _ in prices]
            return _json({"prices": prices, "total_volumes": vols, "market_caps": prices})
        return _json({})
    if host == "mempool.space":
        return _json({"difficultyChange": 1.4, "progressPercent": 50})
    if host == "openapi.sosovalue.com":
        if "/news" in path:
            titles = ["Bitcoin ETF inflows rise", "Fed holds rates steady", "Crypto market steady ahead of CPI"]
            return _json({"data": {"list": [{"title": t} for t in titles]}})
        if "/etfs/summary-history" in path:
            return _json({"data": [{"date": "2026-10-02", "total_net_inflow": 1.5e8},
                                   {"date": "2026-10-01", "total_net_inflow": -5.0e7}]})
        return _json({})
    if host == "api.frankfurter.dev":
        return _json({"base": "EUR", "quote": "USD", "rate": 1.17})
    return 404, "text/plain", "fixture: no response"


def shim_fetch_text(url, headers):
    host = urllib.parse.urlsplit(url).hostname
    if host == "api.stlouisfed.org":
        return json.dumps({"observations": [{"date": "2026-10-02", "value": "4.10"},
                                            {"date": "2026-10-01", "value": "4.12"}]})
    if host == "query1.finance.yahoo.com":
        now_s = int(time.time())
        return json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 99.2},
                                                 "timestamp": [now_s - 86400, now_s],
                                                 "indicators": {"quote": [{"close": [99.0, 99.2]}]}}]}})
    items = "".join(f"<item><title>Headline {i}: markets steady</title><description>d</description></item>"
                    for i in range(5))
    return f"<rss><channel>{items}</channel></rss>"
