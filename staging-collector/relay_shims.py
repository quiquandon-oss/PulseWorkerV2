"""
Local stand-ins for three STATELESS relays of the production V1 Worker (PulseWorker worker.js), so the pinned
CryptoPulse page gets the same inputs without the harness ever contacting that Worker. Each one reads only the
public upstream the V1 handler reads and returns the same JSON shape. Ported from PulseWorker worker.js:

  /macro-proxy  (lines 1815-1834): FRED series observations, '.' values dropped
  /news-proxy   (lines 904-946) + parseRssTitles/stripHtml (lines 153-174): RSS titles, max 15 per feed
  /strc-proxy   (lines 1056-1078): Yahoo Finance STRC chart -> {price, prevClose, pct, series, ts}

Not shimmed (BLOCKED by harness_policy): /stock-proxy (9 Magnificent) and /foufi-latest (reads production D1).
The page then excludes those sources from the composite exactly as it does whenever a source fails.

fetch_text(url, headers) is injected so tests run offline; every URL is checked by check_shim_upstream().
"""
import json
import re
import time
import urllib.parse

from harness_policy import check_shim_upstream

YAHOO_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36")
FRED_SERIES_LABELS = {
    "DCOILWTICO": "WTI Oil", "DGS10": "10Y Treasury Yield", "DTWEXBGS": "US Dollar Index (broad)",
    "NASDAQCOM": "Nasdaq Composite", "SP500": "S&P 500",
}
NEWS_FEEDS = {
    "crypto": [("https://cointelegraph.com/rss", "CoinTelegraph")],
    "macro": [("https://www.investing.com/rss/news_14.rss", "Investing.com Economy")],
    "geopolitics": [("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World News")],
    "regulatory": [("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
                   ("https://www.theblock.co/rss.xml", "The Block")],
}

_ITEM_RE = re.compile(r"<item[\s\S]*?</item>")
_TITLE_RE = re.compile(r"<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>")
_DESC_RE = re.compile(r"<description>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</description>")


def strip_html(s):
    s = re.sub(r"<[^>]*>", " ", s)
    s = re.sub(r"&[a-z]+;", " ", s, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", s).strip()


def parse_rss_titles(xml, source_name):
    items = []
    for block in _ITEM_RE.findall(xml):
        title = _TITLE_RE.search(block)
        desc = _DESC_RE.search(block)
        if title:
            items.append({"title": title.group(1).strip(),
                          "description": strip_html(desc.group(1))[:400] if desc else "",
                          "source": source_name})
        if len(items) >= 15:
            break
    return items


def macro_proxy(query, fetch_text):
    series, key = query.get("series"), query.get("key")
    try:
        limit = int(query.get("limit") or 5)
    except ValueError:
        limit = 5
    limit = min(90, max(2, limit or 5))
    if not series or not key:
        return 400, {"error": "series et key requis"}
    url = check_shim_upstream(
        "https://api.stlouisfed.org/fred/series/observations?series_id=" + urllib.parse.quote(series, safe="")
        + "&api_key=" + urllib.parse.quote(key, safe="") + f"&file_type=json&sort_order=desc&limit={limit}")
    try:
        data = json.loads(fetch_text(url, {}))
        obs = [o for o in (data.get("observations") or []) if o.get("value") != "."]
        return 200, {"observations": obs, "series": FRED_SERIES_LABELS.get(series, series)}
    except Exception as e:  # noqa: BLE001 -- the V1 relay maps any failure to 502 {error}
        return 502, {"error": str(e)}


def news_proxy(query, fetch_text):
    source = query.get("source")
    feeds = NEWS_FEEDS.get(source)
    if not feeds:
        return 400, {"error": "source doit être crypto, macro, geopolitics ou regulatory"}
    if source != "regulatory":
        url, name = feeds[0]
        try:
            return 200, {"items": parse_rss_titles(fetch_text(check_shim_upstream(url), {}), name)}
        except Exception as e:  # noqa: BLE001
            return 502, {"error": str(e)}
    per_feed, errors = [], []
    for url, name in feeds:
        try:
            per_feed.append(parse_rss_titles(fetch_text(check_shim_upstream(url), {}), name))
        except Exception as e:  # noqa: BLE001 -- Promise.allSettled: one feed failing is tolerated
            per_feed.append([])
            errors.append(f"{name}: {e}")
    merged = []
    for i in range(max((len(a) for a in per_feed), default=0)):
        for a in per_feed:
            if i < len(a):
                merged.append(a[i])
    if not merged:
        return 502, {"error": "Both regulatory feeds failed: " + "; ".join(errors)}
    return 200, {"items": merged[:15]}


def strc_proxy(query, fetch_text, now_ms=None):
    url = check_shim_upstream("https://query1.finance.yahoo.com/v8/finance/chart/STRC?interval=1d&range=3mo")
    try:
        data = json.loads(fetch_text(url, {"User-Agent": YAHOO_UA}))
        result = ((data.get("chart") or {}).get("result") or [None])[0] or {}
        meta = result.get("meta") or {}
        if meta.get("regularMarketPrice") is None:
            raise ValueError("STRC no price data")
        timestamps = result.get("timestamp") or []
        closes = (((result.get("indicators") or {}).get("quote") or [{}])[0] or {}).get("close") or []
        series = [{"ts": t * 1000, "close": closes[i]} for i, t in enumerate(timestamps)
                  if i < len(closes) and closes[i] is not None]
        if len(series) >= 2:
            prev = series[-2]["close"]
        else:  # JS: meta.previousClose ?? meta.chartPreviousClose
            prev = meta.get("previousClose") if meta.get("previousClose") is not None else meta.get("chartPreviousClose")
        pct = ((meta["regularMarketPrice"] - prev) / prev) * 100 if prev else None
        return 200, {"price": meta["regularMarketPrice"], "prevClose": prev, "pct": pct, "series": series,
                     "ts": now_ms if now_ms is not None else int(time.time() * 1000)}
    except Exception as e:  # noqa: BLE001
        return 502, {"error": str(e)}


SHIMS = {"/macro-proxy": macro_proxy, "/news-proxy": news_proxy, "/strc-proxy": strc_proxy}


def handle(url, fetch_text):
    """(status, body_dict) for a V1 relay URL the policy classified as SHIM."""
    parsed = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    return SHIMS[parsed.path](query, fetch_text)
