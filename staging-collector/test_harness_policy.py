"""Deny-by-default policy of the browser harness, and the relay shims. Pure: no browser, no network."""
import json

import pytest

import harness_policy as p
import relay_shims as shims

V1 = "https://sentiment-ff75.quiquandon.workers.dev"
# Every V1 path the pinned page references (index.html), except the three shimmed relays and /history.
V1_PATHS = ["/alert-configs", "/alert-configs/delete", "/alert-configs/toggle", "/alert-log", "/analysis",
            "/backfill-sources", "/coin-catalyst-log", "/foufi-latest", "/foufi-recent", "/gemini-outlook",
            "/liquidation-analysis", "/portfolio-history", "/portfolio-log", "/run-technical-eval", "/stock-proxy",
            "/technical-eval", "/test-telegram", "/trader-analysis", "/trader-analysis-gemini", "/txs-backup",
            "/whale-analysis", "/whale-proxy"]


@pytest.mark.parametrize("path", V1_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_every_other_production_v1_path_is_blocked(path, method):
    assert p.decide(method, V1 + path)[0] == p.BLOCK


def test_history_post_is_captured_and_get_is_mocked_never_sent():
    assert p.decide("POST", V1 + "/history")[0] == p.CAPTURE
    assert p.decide("GET", V1 + "/history?limit=5")[0] == p.MOCK
    assert p.decide("DELETE", V1 + "/history")[0] == p.BLOCK


def test_foufi_latest_reads_production_d1_and_is_blocked():
    assert p.decide("GET", V1 + "/foufi-latest")[0] == p.BLOCK


@pytest.mark.parametrize("path", sorted(p.SHIM_PATHS))
def test_only_the_three_stateless_relays_are_shimmed_and_only_for_get(path):
    assert p.decide("GET", V1 + path + "?x=1")[0] == p.SHIM
    assert p.decide("POST", V1 + path)[0] == p.BLOCK


@pytest.mark.parametrize("url", [
    "https://pulseworker-v2.quiquandon.workers.dev/predict",
    "https://pulseworker-v2-staging.quiquandon.workers.dev/btc-backfill",
    "https://anything.workers.dev/",
    "https://script.google.com/macros/s/AKfy/exec",
    "https://community-api.coinmetrics.io/v4/x",
    "https://evil.example/",
    "http://api.coingecko.com/api/v3/global",
])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_workers_dev_apps_script_unknown_and_plain_http_are_blocked(url, method):
    assert p.decide(method, url)[0] == p.BLOCK


@pytest.mark.parametrize("host", sorted(p.PUBLIC_GET_HOSTS))
def test_public_hosts_get_only(host):
    assert p.decide("GET", f"https://{host}/x")[0] == p.PUBLIC
    assert p.decide("POST", f"https://{host}/x")[0] == p.BLOCK


def test_hyperliquid_only_its_read_endpoint():
    assert p.decide("POST", "https://api.hyperliquid.xyz/info")[0] == p.PUBLIC
    assert p.decide("POST", "https://api.hyperliquid.xyz/exchange")[0] == p.BLOCK


def test_local_page_server_get_only_and_only_its_port():
    assert p.decide("GET", "http://127.0.0.1:5555/index.html", local_port=5555)[0] == p.LOCAL
    assert p.decide("GET", "http://127.0.0.1:6666/index.html", local_port=5555)[0] == p.BLOCK
    assert p.decide("POST", "http://127.0.0.1:5555/x", local_port=5555)[0] == p.BLOCK


def test_shim_upstreams_are_allowlisted():
    p.check_shim_upstream("https://api.stlouisfed.org/fred/series/observations?x=1")
    for bad in ("https://sentiment-ff75.quiquandon.workers.dev/macro-proxy", "http://api.stlouisfed.org/",
                "https://script.google.com/x"):
        with pytest.raises(PermissionError):
            p.check_shim_upstream(bad)


def test_api_keys_are_redacted_from_logs():
    out = p.redact_url(V1 + "/macro-proxy?series=DGS10&key=secret&_=1")
    assert "secret" not in out and "series=DGS10" in out


# ---- relay shims reproduce the V1 relays' output shape ----

RSS = ("<rss><channel><item><title><![CDATA[ Bitcoin <b>up</b> ]]></title><description><![CDATA[<p>Hello &amp; "
       "world</p>]]></description></item>" + "".join(f"<item><title>T{i}</title></item>" for i in range(20))
       + "</channel></rss>")


def test_parse_rss_titles_matches_v1():
    items = shims.parse_rss_titles(RSS, "CoinTelegraph")
    assert len(items) == 15
    assert items[0] == {"title": "Bitcoin <b>up</b>", "description": "Hello world", "source": "CoinTelegraph"}


def test_news_proxy_regulatory_interleaves_and_tolerates_one_failed_feed():
    def fetch(url, headers):
        if "coindesk" in url:
            raise OSError("down")
        return RSS
    status, body = shims.news_proxy({"source": "regulatory"}, fetch)
    assert status == 200 and len(body["items"]) == 15 and body["items"][0]["source"] == "The Block"
    assert shims.news_proxy({"source": "nope"}, fetch)[0] == 400


def test_macro_proxy_drops_missing_values_and_requires_key():
    fetch = lambda url, h: json.dumps({"observations": [{"date": "d1", "value": "4.1"}, {"date": "d0", "value": "."},
                                                        {"date": "d-1", "value": "4.0"}]})
    status, body = shims.macro_proxy({"series": "DGS10", "key": "k"}, fetch)
    assert status == 200 and [o["value"] for o in body["observations"]] == ["4.1", "4.0"]
    assert body["series"] == "10Y Treasury Yield"
    assert shims.macro_proxy({"series": "DGS10"}, fetch)[0] == 400


def test_strc_proxy_shape():
    fetch = lambda url, h: json.dumps({"chart": {"result": [{"meta": {"regularMarketPrice": 99.0},
                                      "timestamp": [1, 2], "indicators": {"quote": [{"close": [98.0, 99.0]}]}}]}})
    status, body = shims.strc_proxy({}, fetch, now_ms=5)
    assert status == 200 and body == {"price": 99.0, "prevClose": 98.0, "pct": (99.0 - 98.0) / 98.0 * 100,
                                      "series": [{"ts": 1000, "close": 98.0}, {"ts": 2000, "close": 99.0}], "ts": 5}
    assert shims.strc_proxy({}, lambda u, h: "{}")[0] == 502
