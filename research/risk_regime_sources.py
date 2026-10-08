"""Risk Regime research data layer: €0 source registry and raw collectors. RESEARCH ONLY.

Each collector turns one public, key-free endpoint into observations of the common contract
(`risk_regime_data.make_obs`). Collectors never substitute another source when one fails: they return their
status (OK / BLOCKED_BY_NETWORK_POLICY / BLOCKED_BY_PROVIDER_GEO_RESTRICTION / PROVIDER_ERROR / EMPTY) and no
observations. Sources with no legitimate €0 history are registered as NOT_AVAILABLE or RESEARCH_REQUIRED with the reason.

Existing verified research is reused, not re-implemented:
- Hyperliquid hourly candles: `hyperliquid_asset_universe_run.candles` (same cache, same request bodies).
- Hyperliquid BTC funding/premium: `hyperliquid_research_run.fetch_funding`.
- GDELT 2.0 events: `gdelt_geo_shock.parse_export_zip` / `classify` / `aggregate_batch` and the frozen
  geo_shock grid; raw event rows are exported from the same MD5-verified export files.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from risk_regime_data import DAY, HISTORICAL, HOUR, LIVE, iso, make_obs

UTC = timezone.utc
USER_AGENT = "CryptoPulse-research/1.0 (read-only research; contact via repository)"

# ---------------- source registry ----------------
NEW = "NEW DIMENSION"
HIGHER_RES = "HIGHER-RESOLUTION VERSION OF EXISTING V1 DIMENSION"
V1_DUP = "SAME INSTRUMENT AS AN EXISTING V1 SOURCE"
CONTEXT = "OUTCOME / CONTEXT (not a candidate signal)"

SOURCES: Dict[str, Dict] = {
    "bybit_oi": {
        "dimension": NEW, "group": "derivatives", "provider": "Bybit", "instrument": "BTCUSDT linear perpetual",
        "endpoint": "GET https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h",
        "docs": "https://bybit-exchange.github.io/docs/v5/market/open-interest",
        "auth": "none", "cost_eur": 0, "resolution": "1h (5min/15min/30min/1h/4h/1d available)",
        "pagination": "limit <= 200 rows per call; nextPageCursor", "rate_limit": "public market endpoints: 600 requests / 5 s per IP (IP rate limit, Bybit docs)",
        "history": "investigated by paging backwards until the API returns nothing (depth recorded in the run)",
        "availability_rule": "bar timestamp + 1 interval (an hourly OI reading is used only after its hour closed)",
        "unit": "BTC (open interest in base coin)", "v1_overlap": "none: V1 has no open-interest source",
    },
    "bybit_funding": {
        "dimension": HIGHER_RES, "group": "derivatives", "provider": "Bybit", "instrument": "BTCUSDT linear perpetual",
        "endpoint": "GET https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT",
        "docs": "https://bybit-exchange.github.io/docs/v5/market/history-fund-rate",
        "auth": "none", "cost_eur": 0, "resolution": "8h settlement", "pagination": "limit <= 200; page by endTime",
        "rate_limit": "600 requests / 5 s per IP", "history": "full funding history of the symbol",
        "availability_rule": "funding settlement timestamp (the rate is final at settlement)",
        "unit": "rate per 8h interval", "v1_overlap": "separate venue of V1's funding dimension (V1 reads Hyperliquid funding)",
    },
    "binance_oi": {
        "dimension": NEW, "group": "derivatives", "provider": "Binance USD-M Futures", "instrument": "BTCUSDT perpetual",
        "endpoint": "GET https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h",
        "docs": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Open-Interest-Statistics",
        "auth": "none", "cost_eur": 0, "resolution": "1h (5m..1d)", "pagination": "limit <= 500; page by startTime",
        "rate_limit": "1,000 requests / 5 min per IP for /futures/data endpoints",
        "history": "ONLY THE LATEST 30 DAYS (Binance documented retention). Older history is NOT available and is not claimed.",
        "availability_rule": "bar timestamp + 1 interval", "unit": "BTC (sumOpenInterest) and USD (sumOpenInterestValue)",
        "v1_overlap": "none: V1 has no open-interest source",
    },
    "binance_funding": {
        "dimension": HIGHER_RES, "group": "derivatives", "provider": "Binance USD-M Futures", "instrument": "BTCUSDT perpetual",
        "endpoint": "GET https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT",
        "docs": "https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Get-Funding-Rate-History",
        "auth": "none", "cost_eur": 0, "resolution": "8h settlement", "pagination": "limit <= 1000; page by startTime",
        "rate_limit": "shared 500 requests / 5 min per IP for this endpoint", "history": "full funding history",
        "availability_rule": "fundingTime", "unit": "rate per 8h interval",
        "v1_overlap": "separate venue of V1's funding dimension (V1 reads Hyperliquid funding)",
    },
    "deribit_dvol": {
        "dimension": NEW, "group": "options", "provider": "Deribit", "instrument": "BTC DVOL index",
        "endpoint": "GET https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600",
        "docs": "https://docs.deribit.com/#public-get_volatility_index_data",
        "auth": "none", "cost_eur": 0, "resolution": "1h candles (open/high/low/close)", "pagination": "<= 1,000 candles per call; `continuation`",
        "rate_limit": "non-matching-engine credit pool (~20 requests / s sustained)", "history": "DVOL history since 2021",
        "availability_rule": "candle timestamp + 1 interval (the close is final at the end of the hour)",
        "unit": "annualised implied volatility, %", "v1_overlap": "none: V1 has no implied-volatility source",
    },
    "deribit_options_snapshot": {
        "dimension": NEW, "group": "options", "provider": "Deribit", "instrument": "BTC options (all strikes/expiries)",
        "endpoint": "GET https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
        "docs": "https://docs.deribit.com/#public-get_book_summary_by_currency",
        "auth": "none", "cost_eur": 0, "resolution": "point-in-time snapshot", "pagination": "none",
        "rate_limit": "as above", "history": "LIVE ONLY: the API returns the current book; no historical options OI, put/call or max pain exists for free",
        "availability_rule": "retrieval time (live snapshot; never usable for a past timestamp)",
        "unit": "contracts (BTC)", "v1_overlap": "none",
    },
    "defillama_stablecoins": {
        "dimension": NEW, "group": "stablecoins", "provider": "DeFiLlama", "instrument": "all stablecoins / USDT / USDC / chain totals",
        "endpoint": "GET https://stablecoins.llama.fi/stablecoincharts/all ; /stablecoin/{id} ; /stablecoincharts/{chain}",
        "docs": "https://api-docs.defillama.com/#tag/stablecoins",
        "auth": "none", "cost_eur": 0, "resolution": "1d", "pagination": "none (full series per call)",
        "rate_limit": "free API, fair use (documented ~500 requests/min)", "history": "daily since 2017-2020 depending on series",
        "availability_rule": "daily bucket start + 24h (a day's value is complete only after the day ends)",
        "unit": "USD circulating", "v1_overlap": "none (V1 'global' is total crypto market cap, not stablecoin supply)",
        "semantics": "supply/circulation, NOT exchange inflow/outflow",
    },
    "defillama_dex": {
        "dimension": NEW, "group": "defi", "provider": "DeFiLlama", "instrument": "all DEXs (aggregate)",
        "endpoint": "GET https://api.llama.fi/overview/dexs?excludeTotalDataChartBreakdown=true&dataType=dailyVolume",
        "docs": "https://api-docs.defillama.com/#tag/volumes", "auth": "none", "cost_eur": 0, "resolution": "1d",
        "pagination": "none", "rate_limit": "fair use", "history": "daily since 2020",
        "availability_rule": "daily bucket start + 24h", "unit": "USD volume per day", "v1_overlap": "none",
        "semantics": "on-chain DEX activity, NOT exchange flows",
    },
    "defillama_tvl": {
        "dimension": NEW, "group": "defi", "provider": "DeFiLlama", "instrument": "DeFi TVL (all chains, and per chain)",
        "endpoint": "GET https://api.llama.fi/v2/historicalChainTvl ; /v2/historicalChainTvl/{chain}",
        "docs": "https://api-docs.defillama.com/#tag/tvl", "auth": "none", "cost_eur": 0, "resolution": "1d",
        "pagination": "none", "rate_limit": "fair use", "history": "daily since 2018",
        "availability_rule": "daily bucket start + 24h", "unit": "USD", "v1_overlap": "none",
    },
    "hyperliquid_hip3": {
        "dimension": "MIXED: GOLD/BRENTOIL/EUR are V1 instruments; SP500/XYZ100/10Y are higher-resolution versions of V1 dimensions; SILVER/COPPER are new",
        "group": "cross_asset", "provider": "Hyperliquid", "instrument": "BTC, xyz:SP500, xyz:XYZ100, xyz:GOLD, xyz:SILVER, xyz:COPPER, xyz:BRENTOIL, xyz:EUR, para:10Y",
        "endpoint": "POST https://api.hyperliquid.xyz/info {type: candleSnapshot}", "docs": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint",
        "auth": "none", "cost_eur": 0, "resolution": "1h", "pagination": "500 rows per call; only the most recent 5,000 candles per interval",
        "rate_limit": "1,200 weight / min per IP", "history": "1h back ~208 days (rolling); HIP-3 macro perps listed Dec 2025-Aug 2026",
        "availability_rule": "candle close time T + 1 ms", "unit": "price / base volume",
        "v1_overlap": "see HYPERLIQUID_ASSET_UNIVERSE.md: oil=xyz:BRENTOIL, usd=xyz:EUR, gold=xyz:GOLD in V1; sp500/nasdaq/yield10y are FRED daily in V1",
    },
    "hyperliquid_btc_funding": {
        "dimension": V1_DUP, "group": "derivatives", "provider": "Hyperliquid", "instrument": "BTC perpetual",
        "endpoint": "POST https://api.hyperliquid.xyz/info {type: fundingHistory, coin: BTC}", "docs": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals",
        "auth": "none", "cost_eur": 0, "resolution": "1h", "pagination": "500 rows per call", "rate_limit": "as above",
        "history": "full", "availability_rule": "funding record time", "unit": "rate per hour; premium",
        "v1_overlap": "V1 funding/longshort read the same venue's current funding (HYPERLIQUID_LEVERAGE_STRESS.md)",
    },
    "gdelt_events": {
        "dimension": NEW, "group": "geopolitical", "provider": "GDELT Project", "instrument": "GDELT 2.0 Event exports",
        "endpoint": "GET http://data.gdeltproject.org/gdeltv2/{YYYYMMDDHHMMSS}.export.CSV.zip (masterfilelist.txt for MD5)",
        "docs": "http://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf", "auth": "none", "cost_eur": 0,
        "resolution": "15 min batches", "pagination": "one file per batch", "rate_limit": "none stated; fair use",
        "history": "2015-present", "availability_rule": "batch time + 15 min (frozen GDELT component lag); event date (SQLDATE) is day-level only",
        "unit": "event rows / counts", "v1_overlap": "independent of V1 geopolitics (Spearman -0.07, RISK_REGIME_SHOCK_RESEARCH.md)",
    },
    "gdelt_doc": {
        "dimension": NEW, "group": "news", "provider": "GDELT Project", "instrument": "GDELT DOC 2.0 article search",
        "endpoint": "GET https://api.gdeltproject.org/api/v2/doc/doc?mode=TimelineVolRaw|ArtList&format=json",
        "docs": "https://blog.gdeltproject.org/gdelt-doc-2-0-api-debuts/", "auth": "none", "cost_eur": 0,
        "resolution": "15 min timelines for short spans; article seen-dates to the minute",
        "pagination": "ArtList max 250 articles per call (split by time window)",
        "rate_limit": "GDELT asks for at most one request every 5 seconds", "history": "ROLLING LAST ~3 MONTHS only (DOC 2.0 search window)",
        "availability_rule": "article seendate (first seen by GDELT) + 15 min", "unit": "article counts; normalised = per monitored articles",
        "v1_overlap": "partly overlaps V1 news sources (cryptonews, geopolitics, sosovalue headlines) as text-volume, not as scores",
    },
    "liquidations_xoomar": {
        "dimension": NEW, "group": "liquidations", "provider": "Xoomar (as named in the brief)", "status": "RESEARCH_REQUIRED",
        "reason": "No public 'Xoomar' endpoint could be identified: not referenced anywhere in the CryptoPulse repositories and not found by web search. Needs the exact URL from the requester. No substitute used.",
    },
    "liquidations_exchange_rest": {
        "dimension": NEW, "group": "liquidations", "provider": "Bybit / Binance", "status": "NOT_AVAILABLE",
        "reason": "Neither exchange offers free historical liquidation data over REST: Binance's public forceOrders history endpoint was withdrawn, and Bybit/Binance publish liquidations only on live WebSocket streams. That supports prospective collection only, which is out of scope here.",
    },
    "deribit_options_history": {
        "dimension": NEW, "group": "options", "provider": "Deribit", "status": "NOT_AVAILABLE",
        "reason": "Historical options open interest, put/call ratio and max pain are not provided by the free API (only the current book). They are not reconstructed or fabricated.",
    },
    "whale_flows": {"dimension": NEW, "group": "on_chain", "status": "NOT_AVAILABLE",
                    "reason": "Entity-labelled whale flows need address clustering (Glassnode, CryptoQuant, Arkham, Whale Alert history): paid or key-gated. Further source research required."},
    "miner_flows": {"dimension": NEW, "group": "on_chain", "status": "NOT_AVAILABLE",
                    "reason": "Miner-to-exchange flows need miner address labelling (paid providers). Free mempool.space / blockchain.com data covers hashrate, fees and pool block counts, which are miner ACTIVITY, not flows, and are not substituted."},
    "lth_flows": {"dimension": NEW, "group": "on_chain", "status": "NOT_AVAILABLE",
                  "reason": "Long-term-holder supply/flows need UTXO-age analytics (Glassnode, CryptoQuant): paid. Further source research required."},
    "exchange_flows": {"dimension": NEW, "group": "on_chain", "status": "NOT_AVAILABLE",
                       "reason": "Genuine exchange inflow/outflow needs exchange wallet labelling (CryptoQuant, Glassnode, Nansen): paid or key-gated. DeFiLlama stablecoin/DEX data is NOT a substitute and is not labelled as flows."},
}


# ---------------- HTTP ----------------
class SourceError(RuntimeError):
    def __init__(self, status: str, message: str):
        super().__init__(f"{status}: {message}")
        self.status = status


RATE_LIMIT_TEXT = b"Please limit requests"
GEO_BLOCK_MARKERS = (b"block access from your country", b"restricted location")


class Http:
    """Minimal JSON GET with retry/backoff on 429 and 5xx. A proxy CONNECT refusal is reported as
    BLOCKED_BY_NETWORK_POLICY (not retried as if it were the provider); a provider's own refusal of the caller's
    country is BLOCKED_BY_PROVIDER_GEO_RESTRICTION. `opener` and `sleep` are injectable."""

    def __init__(self, opener: Optional[Callable] = None, sleep: Callable = time.sleep, retries: int = 3,
                 min_interval_s: float = 0.0, log: Optional[Counter] = None, max_backoff_s: Optional[float] = None,
                 cache_dir: Optional[Path] = None):
        self.opener = opener or self._urlopen
        self.sleep = sleep
        self.retries = retries
        self.min_interval_s = min_interval_s
        self.max_backoff_s = max_backoff_s
        self.log = log if log is not None else Counter()
        self._last = 0.0
        # Optional response cache: only successful JSON bodies, stored with their real retrieval time so that a
        # rerun resumes without re-requesting and provenance keeps the original fetch time.
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.last_retrieved_at: Optional[int] = None

    @staticmethod
    def _urlopen(url: str, timeout: int = 30) -> Tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read() if hasattr(e, "read") else b""

    def get_json(self, url: str, params: Optional[Dict] = None):
        full = url + ("?" + urllib.parse.urlencode(params) if params else "")
        cpath = self.cache_dir / (hashlib.sha256(full.encode()).hexdigest() + ".json") if self.cache_dir else None
        if cpath is not None and cpath.exists():
            hit = json.loads(cpath.read_text())
            self.log["cached"] += 1
            self.last_retrieved_at = hit["retrieved_at"]
            return hit["body"]
        last = None
        for attempt in range(self.retries):
            if self.min_interval_s:
                wait = self._last + self.min_interval_s - time.time()
                if wait > 0:
                    self.sleep(wait)
            try:
                status, body = self.opener(full)
                self._last = time.time()
            except Exception as e:  # network-level failure
                msg = str(e)
                if "Tunnel connection failed: 403" in msg or "CONNECT tunnel failed" in msg:
                    self.log["blocked"] += 1
                    raise SourceError("BLOCKED_BY_NETWORK_POLICY", f"{urllib.parse.urlparse(url).netloc} refused by the environment proxy")
                last = msg
                self.log["network_error"] += 1
                self.sleep(2 ** attempt)
                continue
            self.log[f"http_{status}"] += 1
            if body[:64].lstrip().startswith(RATE_LIMIT_TEXT):
                status = 429                     # GDELT DOC sends its rate-limit notice as plain text
            if status in (403, 451) and any(m in body[:500].lower() for m in GEO_BLOCK_MARKERS):
                self.log["geo_blocked"] += 1
                raise SourceError("BLOCKED_BY_PROVIDER_GEO_RESTRICTION",
                                  f"{urllib.parse.urlparse(url).netloc} refuses this environment's egress location (HTTP {status}): {body[:160]!r}")
            if status == 429 or 500 <= status < 600:
                last = f"HTTP {status}"
                b = 5 * (attempt + 1)
                self.sleep(min(b, self.max_backoff_s) if self.max_backoff_s else b)
                continue
            if status != 200:
                raise SourceError("PROVIDER_ERROR", f"HTTP {status} from {urllib.parse.urlparse(url).netloc}: {body[:200]!r}")
            try:
                parsed = json.loads(body)
            except ValueError:
                raise SourceError("MALFORMED_RESPONSE", f"non-JSON body from {url}")
            self.last_retrieved_at = int(time.time() * 1000)
            if cpath is not None:
                cpath.parent.mkdir(parents=True, exist_ok=True)
                cpath.write_text(json.dumps({"url": full, "retrieved_at": self.last_retrieved_at, "body": parsed}))
            return parsed
        raise SourceError("PROVIDER_ERROR", f"{url}: {last} after {self.retries} attempts")


def _num(v) -> float:
    f = float(v)
    if f != f:
        raise ValueError("NaN")
    return f


def _mk(src: str, instrument: str, metric: str, ts: int, value, unit: str, interval: str, retrieved_at: int,
        url: str, available_at: int, raw: Dict, live: bool = False) -> Dict:
    return make_obs(source=src, provider=SOURCES[src]["provider"], instrument=instrument, metric=metric, timestamp=ts,
                    value=value, unit=unit, interval=interval, retrieved_at=retrieved_at,
                    historical_or_live=LIVE if live else HISTORICAL, source_url=url, available_at=available_at, raw=raw)


def _result(src: str, obs: List[Dict], requests: int, pages: int, note: str = "") -> Dict:
    return {"source": src, "status": "OK" if obs else "EMPTY", "observations": obs, "requests": requests, "pages": pages, "note": note}


def run_collector(fn: Callable, *args, **kw) -> Dict:
    """Runs one collector; a failure becomes a status, never an exception or a substitute."""
    try:
        return fn(*args, **kw)
    except SourceError as e:
        return {"source": kw.get("src") or getattr(fn, "source_id", fn.__name__), "status": e.status, "error": str(e),
                "observations": [], "requests": 0, "pages": 0}
    except (KeyError, TypeError, ValueError) as e:
        return {"source": getattr(fn, "source_id", fn.__name__), "status": "MALFORMED_RESPONSE", "error": f"{type(e).__name__}: {e}",
                "observations": [], "requests": 0, "pages": 0}


# ---------------- Bybit ----------------
BYBIT = "https://api.bybit.com"


def parse_bybit(payload: Dict) -> Dict:
    if not isinstance(payload, dict) or payload.get("retCode") != 0:
        raise SourceError("PROVIDER_ERROR", f"Bybit retCode {payload.get('retCode') if isinstance(payload, dict) else '?'}: {payload.get('retMsg') if isinstance(payload, dict) else payload!r}")
    return payload["result"]


def collect_bybit_oi(http: Http, start_ms: int, end_ms: int, retrieved_at: int, max_pages: int = 400) -> Dict:
    """Hourly OI, newest first, paged with nextPageCursor until the window start or until the API stops."""
    url = f"{BYBIT}/v5/market/open-interest"
    params = {"category": "linear", "symbol": "BTCUSDT", "intervalTime": "1h", "startTime": start_ms, "endTime": end_ms, "limit": 200}
    obs, pages = [], 0
    while pages < max_pages:
        res = parse_bybit(http.get_json(url, params))
        pages += 1
        rows = res.get("list") or []
        for r in rows:
            ts = int(r["timestamp"])
            obs.append(_mk("bybit_oi", "BTCUSDT", "open_interest", ts, _num(r["openInterest"]), "BTC", "1h", retrieved_at,
                           url, ts + HOUR, {"openInterest": r["openInterest"], "timestamp": r["timestamp"]}))
        cur = res.get("nextPageCursor")
        if not rows or not cur:
            break
        params = {**params, "cursor": cur}
    return _result("bybit_oi", obs, pages, pages)


collect_bybit_oi.source_id = "bybit_oi"


def collect_bybit_funding(http: Http, start_ms: int, end_ms: int, retrieved_at: int, max_pages: int = 100) -> Dict:
    url = f"{BYBIT}/v5/market/funding/history"
    obs, pages, end = [], 0, end_ms
    while pages < max_pages and end > start_ms:
        res = parse_bybit(http.get_json(url, {"category": "linear", "symbol": "BTCUSDT", "startTime": start_ms, "endTime": end, "limit": 200}))
        pages += 1
        rows = res.get("list") or []
        if not rows:
            break
        for r in rows:
            ts = int(r["fundingRateTimestamp"])
            obs.append(_mk("bybit_funding", "BTCUSDT", "funding_rate", ts, _num(r["fundingRate"]), "rate/8h", "8h",
                           retrieved_at, url, ts, dict(r)))
        oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
        if oldest - 1 >= end:
            break
        end = oldest - 1
    return _result("bybit_funding", obs, pages, pages)


collect_bybit_funding.source_id = "bybit_funding"


# ---------------- Binance ----------------
BINANCE = "https://fapi.binance.com"


def collect_binance_oi(http: Http, start_ms: int, end_ms: int, retrieved_at: int) -> Dict:
    """openInterestHist keeps only the latest 30 days; requests before that are clipped, not faked."""
    url = f"{BINANCE}/futures/data/openInterestHist"
    floor = retrieved_at - 30 * DAY + HOUR
    start = max(start_ms, floor)
    obs, pages = [], 0
    while start < end_ms and pages < 200:
        rows = http.get_json(url, {"symbol": "BTCUSDT", "period": "1h", "limit": 500, "startTime": start, "endTime": end_ms})
        pages += 1
        if not isinstance(rows, list):
            raise SourceError("MALFORMED_RESPONSE", f"openInterestHist returned {type(rows).__name__}")
        if not rows:
            break
        for r in rows:
            ts = int(r["timestamp"])
            for metric, field, unit in (("open_interest", "sumOpenInterest", "BTC"), ("open_interest_usd", "sumOpenInterestValue", "USD")):
                obs.append(_mk("binance_oi", "BTCUSDT", metric, ts, _num(r[field]), unit, "1h", retrieved_at, url, ts + HOUR, dict(r)))
        nxt = max(int(r["timestamp"]) for r in rows) + 1
        if nxt <= start:
            break
        start = nxt
    note = "clipped to Binance's 30-day retention" if start_ms < floor else ""
    return _result("binance_oi", obs, pages, pages, note)


collect_binance_oi.source_id = "binance_oi"


def collect_binance_funding(http: Http, start_ms: int, end_ms: int, retrieved_at: int) -> Dict:
    url = f"{BINANCE}/fapi/v1/fundingRate"
    obs, pages, start = [], 0, start_ms
    while start < end_ms and pages < 100:
        rows = http.get_json(url, {"symbol": "BTCUSDT", "startTime": start, "endTime": end_ms, "limit": 1000})
        pages += 1
        if not isinstance(rows, list):
            raise SourceError("MALFORMED_RESPONSE", "fundingRate did not return a list")
        if not rows:
            break
        for r in rows:
            ts = int(r["fundingTime"])
            obs.append(_mk("binance_funding", "BTCUSDT", "funding_rate", ts, _num(r["fundingRate"]), "rate/8h", "8h",
                           retrieved_at, url, ts, dict(r)))
        nxt = max(int(r["fundingTime"]) for r in rows) + 1
        if nxt <= start:
            break
        start = nxt
    return _result("binance_funding", obs, pages, pages)


collect_binance_funding.source_id = "binance_funding"


# ---------------- Deribit ----------------
DERIBIT = "https://www.deribit.com/api/v2/public"


def _deribit_result(payload):
    if not isinstance(payload, dict) or "result" not in payload:
        raise SourceError("PROVIDER_ERROR", f"Deribit error: {payload.get('error') if isinstance(payload, dict) else payload!r}")
    return payload["result"]


def collect_deribit_dvol(http: Http, start_ms: int, end_ms: int, retrieved_at: int) -> Dict:
    """DVOL hourly candles; pages backwards with `continuation`."""
    url = f"{DERIBIT}/get_volatility_index_data"
    obs, pages, end = [], 0, end_ms
    while pages < 100 and end and end > start_ms:
        res = _deribit_result(http.get_json(url, {"currency": "BTC", "start_timestamp": start_ms, "end_timestamp": end, "resolution": "3600"}))
        pages += 1
        rows = res.get("data") or []
        for r in rows:
            ts = int(r[0])
            for i, m in ((1, "dvol_open"), (2, "dvol_high"), (3, "dvol_low"), (4, "dvol_close")):
                obs.append(_mk("deribit_dvol", "BTC DVOL", m, ts, _num(r[i]), "% annualised", "1h", retrieved_at, url, ts + HOUR,
                               {"row": r}))
        cont = res.get("continuation")
        if not rows or not cont or int(cont) >= end:
            break
        end = int(cont)
    return _result("deribit_dvol", obs, pages, pages)


collect_deribit_dvol.source_id = "deribit_dvol"


def parse_option_name(name: str) -> Optional[Tuple[str, float, str]]:
    """'BTC-27SEP26-80000-C' -> ('27SEP26', 80000.0, 'C'); None if the name is not an option."""
    parts = name.split("-")
    if len(parts) != 4 or parts[3] not in ("C", "P"):
        return None
    try:
        return parts[1], float(parts[2]), parts[3]
    except ValueError:
        return None


def max_pain(oi_by_strike: Dict[float, Dict[str, float]]) -> Optional[float]:
    """Strike minimising total intrinsic value owed to option holders (calls + puts), from current OI only."""
    strikes = sorted(oi_by_strike)
    if not strikes:
        return None
    def pain(s):
        return sum(v.get("C", 0) * max(0.0, s - k) + v.get("P", 0) * max(0.0, k - s) for k, v in oi_by_strike.items())
    return min(strikes, key=pain)


def collect_deribit_options_snapshot(http: Http, retrieved_at: int) -> Dict:
    """LIVE ONLY. Totals, put/call OI ratio and per-expiry max pain from the current book."""
    url = f"{DERIBIT}/get_book_summary_by_currency"
    rows = _deribit_result(http.get_json(url, {"currency": "BTC", "kind": "option"}))
    calls = puts = 0.0
    by_exp: Dict[str, Dict[float, Dict[str, float]]] = {}
    for r in rows:
        p = parse_option_name(r.get("instrument_name", ""))
        if not p:
            continue
        exp, strike, cp = p
        oi = _num(r.get("open_interest") or 0)
        calls += oi if cp == "C" else 0
        puts += oi if cp == "P" else 0
        by_exp.setdefault(exp, {}).setdefault(strike, {"C": 0.0, "P": 0.0})[cp] += oi
    obs = []
    mk = lambda m, v, unit, raw: _mk("deribit_options_snapshot", "BTC options", m, retrieved_at, v, unit, "snapshot",
                                     retrieved_at, url, retrieved_at, raw, live=True)
    if rows:
        obs += [mk("options_open_interest_total", calls + puts, "BTC", {"instruments": len(rows)}),
                mk("put_call_oi_ratio", round(puts / calls, 6) if calls else None, "ratio", {"calls": calls, "puts": puts})]
        for exp, strikes in sorted(by_exp.items()):
            obs.append(mk(f"max_pain_{exp}", max_pain(strikes), "USD strike", {"strikes": len(strikes)}))
    return _result("deribit_options_snapshot", obs, 1, 1, "live snapshot only")


collect_deribit_options_snapshot.source_id = "deribit_options_snapshot"


# ---------------- DeFiLlama ----------------
LLAMA_STABLE = "https://stablecoins.llama.fi"
LLAMA = "https://api.llama.fi"
STABLECOIN_IDS = {"USDT": 1, "USDC": 2}
STABLECOIN_CHAINS = ("Ethereum", "Tron", "Solana", "BSC", "Arbitrum")


def _daily(src, instrument, metric, date_s, value, retrieved_at, url, raw, unit="USD"):
    ts = int(date_s) * 1000
    return _mk(src, instrument, metric, ts, _num(value), unit, "1d", retrieved_at, url, ts + DAY, raw)


def parse_stablecoin_chart(rows, instrument: str, retrieved_at: int, url: str) -> List[Dict]:
    if not isinstance(rows, list):
        raise SourceError("MALFORMED_RESPONSE", "stablecoincharts did not return a list")
    out = []
    for r in rows:
        v = (r.get("totalCirculatingUSD") or {}).get("peggedUSD")
        if v is None:
            continue
        out.append(_daily("defillama_stablecoins", instrument, "circulating_usd", r["date"], v, retrieved_at, url,
                          {"date": r["date"], "peggedUSD": v}))
    return out


def parse_stablecoin_asset(payload, symbol: str, retrieved_at: int, url: str) -> List[Dict]:
    """/stablecoin/{id}: uses the asset-level `tokens` history when present, otherwise the sum of
    `chainBalances[*].tokens` per date (both shapes are documented by DeFiLlama)."""
    if not isinstance(payload, dict):
        raise SourceError("MALFORMED_RESPONSE", "stablecoin asset did not return an object")
    hist = payload.get("tokens")
    per_date: Dict[str, float] = {}
    if isinstance(hist, list) and hist:
        for r in hist:
            v = (r.get("circulating") or {}).get("peggedUSD")
            if v is not None:
                per_date[str(r["date"])] = _num(v)
    else:
        for chain in (payload.get("chainBalances") or {}).values():
            for r in chain.get("tokens") or []:
                v = (r.get("circulating") or {}).get("peggedUSD")
                if v is not None:
                    per_date[str(r["date"])] = per_date.get(str(r["date"]), 0.0) + _num(v)
    return [_daily("defillama_stablecoins", symbol, "circulating_usd", d, v, retrieved_at, url, {"date": d}) for d, v in sorted(per_date.items())]


def collect_defillama_stablecoins(http: Http, start_ms: int, end_ms: int, retrieved_at: int) -> Dict:
    obs, req = [], 0
    url = f"{LLAMA_STABLE}/stablecoincharts/all"
    obs += parse_stablecoin_chart(http.get_json(url), "ALL_STABLECOINS", retrieved_at, url); req += 1
    for sym, i in STABLECOIN_IDS.items():
        url = f"{LLAMA_STABLE}/stablecoin/{i}"
        obs += parse_stablecoin_asset(http.get_json(url), sym, retrieved_at, url); req += 1
    for chain in STABLECOIN_CHAINS:
        url = f"{LLAMA_STABLE}/stablecoincharts/{chain}"
        obs += parse_stablecoin_chart(http.get_json(url), f"chain:{chain}", retrieved_at, url); req += 1
    obs = [o for o in obs if start_ms - 30 * DAY <= o["timestamp"] <= end_ms]
    return _result("defillama_stablecoins", obs, req, req)


collect_defillama_stablecoins.source_id = "defillama_stablecoins"


def collect_defillama_dex(http: Http, start_ms: int, end_ms: int, retrieved_at: int) -> Dict:
    url = f"{LLAMA}/overview/dexs"
    p = http.get_json(url, {"excludeTotalDataChart": "false", "excludeTotalDataChartBreakdown": "true", "dataType": "dailyVolume"})
    rows = p.get("totalDataChart") if isinstance(p, dict) else None
    if not isinstance(rows, list):
        raise SourceError("MALFORMED_RESPONSE", "overview/dexs has no totalDataChart")
    obs = [_daily("defillama_dex", "ALL_DEXS", "dex_volume_usd", r[0], r[1], retrieved_at, url, {"row": r}) for r in rows
           if start_ms - 30 * DAY <= int(r[0]) * 1000 <= end_ms]
    return _result("defillama_dex", obs, 1, 1)


collect_defillama_dex.source_id = "defillama_dex"


def collect_defillama_tvl(http: Http, start_ms: int, end_ms: int, retrieved_at: int, chains: Sequence[str] = ("Ethereum", "Solana", "Tron")) -> Dict:
    obs, req = [], 0
    for chain in (None,) + tuple(chains):
        url = f"{LLAMA}/v2/historicalChainTvl" + (f"/{chain}" if chain else "")
        rows = http.get_json(url); req += 1
        if not isinstance(rows, list):
            raise SourceError("MALFORMED_RESPONSE", f"{url} did not return a list")
        obs += [_daily("defillama_tvl", f"chain:{chain}" if chain else "ALL_CHAINS", "tvl_usd", r["date"], r["tvl"], retrieved_at, url,
                       {"date": r["date"], "tvl": r["tvl"]}) for r in rows if start_ms - 30 * DAY <= int(r["date"]) * 1000 <= end_ms]
    return _result("defillama_tvl", obs, req, req)


collect_defillama_tvl.source_id = "defillama_tvl"


# ---------------- GDELT DOC 2.0 ----------------
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"
DOC_GDELT_LAG = 15 * 60_000
# Fixed, pre-declared topic queries (not tuned to any event).
DOC_TOPICS = {
    "crypto": '(bitcoin OR cryptocurrency OR "crypto market")',
    "monetary_policy": '("federal reserve" OR "interest rate" OR "rate cut" OR "rate hike")',
    "geopolitical_conflict": '(airstrike OR invasion OR "military strike" OR missile OR sanctions)',
    "energy": '("oil price" OR opec OR "crude oil")',
}


def _doc_ts(s: str) -> int:
    """'20260927T121500Z' -> epoch ms."""
    return int(datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC).timestamp() * 1000)


def _doc_dt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y%m%d%H%M%S")


def parse_doc_timeline(payload, topic: str, retrieved_at: int, url: str) -> List[Dict]:
    if not isinstance(payload, dict):
        raise SourceError("MALFORMED_RESPONSE", "DOC timeline is not an object")
    out = []
    for series in payload.get("timeline") or []:
        for d in series.get("data") or []:
            ts = _doc_ts(d["date"])
            raw = {"date": d["date"], "value": d.get("value"), "norm": d.get("norm"), "series": series.get("series")}
            out.append(_mk("gdelt_doc", f"topic:{topic}", "article_count", ts, _num(d["value"]), "articles", "15m", retrieved_at, url,
                           ts + 15 * 60_000 + DOC_GDELT_LAG, raw))
            if d.get("norm"):
                out.append(_mk("gdelt_doc", f"topic:{topic}", "monitored_articles_total", ts, _num(d["norm"]), "articles", "15m",
                               retrieved_at, url, ts + 15 * 60_000 + DOC_GDELT_LAG, raw))
    return out


def parse_doc_articles(payload, topic: str, retrieved_at: int, url: str) -> List[Dict]:
    """Article provenance records (not numeric observations): seen time, url, domain, title, language, country."""
    if not isinstance(payload, dict):
        raise SourceError("MALFORMED_RESPONSE", "DOC article list is not an object")
    out = []
    for a in payload.get("articles") or []:
        ts = _doc_ts(a["seendate"])
        out.append({"source": "gdelt_doc", "topic": topic, "first_seen": ts, "available_at": ts + DOC_GDELT_LAG,
                    "retrieved_at": retrieved_at, "url": a.get("url"), "domain": a.get("domain"), "title": a.get("title"),
                    "language": a.get("language"), "source_country": a.get("sourcecountry"), "query_url": url})
    return out


def collect_gdelt_doc(http: Http, windows: Sequence[Tuple[int, int]], retrieved_at: int, articles: bool = True) -> Dict:
    """Timeline volume (raw counts + monitored total) per topic, and article lists, for each window. The DOC API
    only searches its rolling ~3-month window: anything older returns empty and is reported, not filled."""
    obs, arts, req, failed = [], [], 0, []
    for lo, hi in windows:
        for topic, q in DOC_TOPICS.items():
            jobs = [("TimelineVolRaw", {"query": q, "mode": "TimelineVolRaw", "format": "json", "STARTDATETIME": _doc_dt(lo), "ENDDATETIME": _doc_dt(hi)}, parse_doc_timeline)]
            if articles:
                jobs.append(("ArtList", {"query": q, "mode": "ArtList", "format": "json", "maxrecords": 250, "sort": "DateAsc",
                                         "STARTDATETIME": _doc_dt(lo), "ENDDATETIME": _doc_dt(hi)}, parse_doc_articles))
            for mode, params, parse in jobs:
                req += 1
                try:
                    payload = http.get_json(GDELT_DOC, params)
                except SourceError as e:
                    # One rate-limited request no longer discards the others: it is named as a gap, nothing is filled.
                    failed.append({"window": [iso(lo), iso(hi)], "topic": topic, "mode": mode, "status": e.status, "error": str(e)})
                    continue
                got = parse(payload, topic, http.last_retrieved_at or retrieved_at, GDELT_DOC)
                (obs if mode == "TimelineVolRaw" else arts).extend(got)
    if failed and not obs and not arts:
        raise SourceError(failed[-1]["status"], f"all {req} DOC requests failed; last: {failed[-1]['error']}")
    r = _result("gdelt_doc", obs, req, req, "ArtList capped at 250 articles per topic per window")
    if failed:
        r["status"] = "PARTIAL"
        r["failed_requests"] = failed
    r["articles"] = arts
    return r


collect_gdelt_doc.source_id = "gdelt_doc"


# ---------------- Hyperliquid (reuse) ----------------
HL_SYMBOLS = ("BTC", "xyz:SP500", "xyz:XYZ100", "xyz:GOLD", "xyz:SILVER", "xyz:COPPER", "xyz:BRENTOIL", "xyz:EUR", "para:10Y")


def hyperliquid_obs(candles_by_symbol: Dict[str, List[Dict]], retrieved_at: int) -> List[Dict]:
    """Close and volume of each closed hourly candle; available after the candle's close time."""
    url = "https://api.hyperliquid.xyz/info"
    out = []
    for sym, cs in candles_by_symbol.items():
        for c in cs:
            raw = {k: c[k] for k in ("t", "T", "o", "h", "l", "c", "v", "n")}
            out.append(_mk("hyperliquid_hip3", sym, "close", c["t"], c["c"], "price", "1h", retrieved_at, url, c["T"] + 1, raw))
            out.append(_mk("hyperliquid_hip3", sym, "volume", c["t"], c["v"], "base units", "1h", retrieved_at, url, c["T"] + 1, raw))
    return out


def hyperliquid_funding_obs(funding: List[Dict], retrieved_at: int) -> List[Dict]:
    url = "https://api.hyperliquid.xyz/info"
    out = []
    for r in funding:
        out.append(_mk("hyperliquid_btc_funding", "BTC", "funding_rate", r["t"], r["funding"], "rate/1h", "1h", retrieved_at, url, r["t"], dict(r)))
        out.append(_mk("hyperliquid_btc_funding", "BTC", "premium", r["t"], r["premium"], "fraction", "1h", retrieved_at, url, r["t"], dict(r)))
    return out


# ---------------- GDELT events (reuse) ----------------
GDELT_AGG_METRICS = ("total_events", "geo_events", "escalation_events", "corridor_escalation_events", "geo_sources", "geo_articles")


def gdelt_batch_obs(series: Dict[datetime, Dict], retrieved_at: int, lag_ms: int = 15 * 60_000) -> List[Dict]:
    """Per-15-minute-batch counts from the frozen GDELT aggregation (`aggregate_batch`)."""
    out = []
    for b, agg in series.items():
        ts = int(b.timestamp() * 1000)
        url = f"http://data.gdeltproject.org/gdeltv2/{b.strftime('%Y%m%d%H%M%S')}.export.CSV.zip"
        for m in GDELT_AGG_METRICS:
            out.append(_mk("gdelt_events", "GDELT 2.0 events", m, ts, agg.get(m, 0), "count", "15m", retrieved_at, url, ts + lag_ms,
                           {"categories": agg.get("categories", {})} if m == "geo_events" else {}))
    return out


def geo_shock_obs(grid_rows: Sequence[Dict], retrieved_at: int) -> List[Dict]:
    """The frozen geo_shock component (already point-in-time at its own t), kept as a raw research series."""
    out = []
    for r in grid_rows:
        if r.get("geo_shock_score") is None:
            continue
        out.append(make_obs(source="gdelt_events", provider="GDELT Project", instrument="geo_shock (frozen GDELT component)",
                            metric="geo_shock_score", timestamp=r["t_ms"], value=r["geo_shock_score"], unit="0-100 percentile median",
                            interval="15m", retrieved_at=retrieved_at, historical_or_live=HISTORICAL,
                            source_url="research/gdelt_geo_shock.py", available_at=r["t_ms"],
                            raw={"elevated": r.get("elevated"), "status": r.get("status")}))
    return out


def gdelt_event_record(e, batch: datetime, c, retrieved_at: int, lag_ms: int = 15 * 60_000) -> Dict:
    """One raw GDELT event row with provenance. `event_date` is GDELT's SQLDATE (day precision only); first_seen
    is the 15-minute batch that published it. Goldstein is kept as its SIGN only: it is a theoretical
    cooperation/conflict potential of the event TYPE, not a measure of severity, and is not usable as one."""
    ts = int(batch.timestamp() * 1000)
    g = e.goldstein
    return {"source": "gdelt_events", "global_event_id": e.event_id, "event_date": e.sql_date, "first_seen": ts,
            "date_added": e.date_added, "available_at": ts + lag_ms, "retrieved_at": retrieved_at,
            "cameo_code": e.code, "cameo_root": e.root, "quad_class": e.quad,
            "goldstein_sign": None if g is None else (1 if g > 0 else (-1 if g < 0 else 0)),
            "actor1_country": e.actor1_country, "actor2_country": e.actor2_country, "actor1_type": e.actor1_type,
            "actor2_type": e.actor2_type, "action_country": e.action_country, "action_location": e.action_name,
            "num_mentions": e.mentions, "num_sources": e.sources, "num_articles": e.articles, "source_url": e.url,
            "category": c.category, "escalation": c.escalation, "corridor": c.corridor, "major_power": c.major_power}
