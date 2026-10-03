"""
Deny-by-default network policy for history_harness.py, kept pure (no browser, no network) so CI can test it.

The pinned CryptoPulse page talks to 26 endpoints of the production V1 Worker (several of them writes:
/history POST, /portfolio-log, /txs-backup, /alert-configs, /gemini-outlook...), to a Google Apps Script write
endpoint, and to public market-data APIs. decide() classifies every request the page makes:

  LOCAL    the harness's own 127.0.0.1 server delivering the pinned index.html
  PUBLIC   an allowlisted public data API; the browser fetches it itself (read-only use)
  CAPTURE  POST <V1>/history -- the composite payload. Captured in memory, answered locally, NEVER sent
  MOCK     GET <V1>/history -- answered locally with an empty history (never reads production)
  SHIM     GET <V1>/macro-proxy | /news-proxy | /strc-proxy -- answered locally by relay_shims.py, which reads
           the same public upstreams those stateless V1 relays read (never the Worker itself)
  BLOCK    everything else: every other V1 path (including /foufi-latest, which reads production D1), any other
           *.workers.dev host, script.google.com, unknown hosts, and any non-GET to a public host
"""
import urllib.parse

V1_PRODUCTION_HOST = "sentiment-ff75.quiquandon.workers.dev"
BLOCKED_HOSTS = frozenset({
    V1_PRODUCTION_HOST,
    "pulseworker-v2.quiquandon.workers.dev",
    "script.google.com",
    "script.googleusercontent.com",
})

# Hosts the page reads composite inputs (and boot data) from. GET only, except Hyperliquid's read API.
PUBLIC_GET_HOSTS = frozenset({
    "api.alternative.me",        # Fear & Greed (fgValue)
    "api.coingecko.com",         # global market cap; BTC 365d market_chart -> technical score
    "mempool.space",             # on-chain difficulty
    "openapi.sosovalue.com",     # news + BTC ETF flows
    "api.frankfurter.dev",       # EUR/USD (boot, display only)
})
PUBLIC_POST_ENDPOINTS = frozenset({("api.hyperliquid.xyz", "/info")})  # funding, long/short, oil, usd, HYPE

SHIM_PATHS = frozenset({"/macro-proxy", "/news-proxy", "/strc-proxy"})
# Upstreams relay_shims.py may read (exactly the URLs the V1 relays fetch).
SHIM_UPSTREAM_HOSTS = frozenset({
    "api.stlouisfed.org",
    "cointelegraph.com",
    "www.investing.com",
    "feeds.bbci.co.uk",
    "www.coindesk.com",
    "www.theblock.co",
    "query1.finance.yahoo.com",
})

LOCAL_HOSTS = frozenset({"127.0.0.1"})

LOCAL, PUBLIC, CAPTURE, MOCK, SHIM, BLOCK = "LOCAL", "PUBLIC", "CAPTURE", "MOCK", "SHIM", "BLOCK"


def decide(method, url, local_port=None):
    """Returns (action, reason). Unknown -> BLOCK."""
    method = (method or "").upper()
    parsed = urllib.parse.urlsplit(url)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    if parsed.scheme in ("data", "blob", "about"):
        return LOCAL, "inline resource"
    if host in LOCAL_HOSTS and parsed.scheme == "http" and (local_port is None or parsed.port == local_port):
        return (LOCAL, "pinned page") if method == "GET" else (BLOCK, "non-GET to the local page server")
    if host == V1_PRODUCTION_HOST:
        if path == "/history" and method == "POST":
            return CAPTURE, "composite payload captured locally; never transmitted"
        if path == "/history" and method == "GET":
            return MOCK, "empty history answered locally; production history is never read"
        if path in SHIM_PATHS and method == "GET":
            return SHIM, "stateless public relay answered locally from the same public upstream"
        return BLOCK, f"production V1 Worker {method} {path} is never contacted"
    if host in BLOCKED_HOSTS or host.endswith(".workers.dev") or host.endswith("script.google.com"):
        return BLOCK, "production / workers.dev / Apps Script host"
    if parsed.scheme != "https":
        return BLOCK, "non-https request"
    if method == "POST" and (host, path) in PUBLIC_POST_ENDPOINTS:
        return PUBLIC, "public read API (POST by protocol)"
    if host in PUBLIC_GET_HOSTS:
        return (PUBLIC, "public data API") if method == "GET" else (BLOCK, f"{method} to a public host")
    return BLOCK, "host not on the allowlist"


def check_shim_upstream(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in SHIM_UPSTREAM_HOSTS:
        raise PermissionError(f"shim upstream not allowed: {url}")
    return url


def redact_url(url):
    """Drops query values that carry API keys (they are public in the page, but never logged)."""
    parsed = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(k, "<redacted>" if "key" in k.lower() else v) for k, v in query]
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(redacted)))
