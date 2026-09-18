"""
PR4: internet evidence collection.

PR3 event -> bounded time window -> fixed 5-feed set across 4 feed
categories -> RSS items -> provenance + 3 timestamps -> canonicalization
-> deduplication -> optional keyword score -> research_event_evidence.

Explicitly NOT: causal explanation, LLM interpretation, semantic
relevance judgment, coefficient optimization, V1/V2 modification,
automatic hypothesis generation, production workflow scheduling.

Reuses the exact RSS feed URLs already proven in production
(quiquandon-oss/PulseWorker's /news-proxy handler, verified directly
from source) -- but writes its own parser, since that existing handler
discards <link> and <pubDate> entirely (confirmed by reading it), and
PR4's whole purpose requires exactly those provenance fields.

Unlike research/resolver.py and research/event_detector.py (which only
return rows), this module DOES write -- to research_event_evidence only,
nothing else. That's an explicit, deliberate difference: PR2/PR3 are
pure analysis; PR4's job is to persist a durable evidence record.
"""
import hashlib
import re
import sqlite3
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ---- Fixed, frozen feed set -- every event checks all four, always.
# No category-to-feed mapping: that would itself be a relevance judgment.
# URLs verbatim from PulseWorker's real /news-proxy handler (verified by
# reading worker.js directly, not assumed).
FEEDS = {
    "crypto": ("https://cointelegraph.com/rss", "CoinTelegraph"),
    "macro": ("https://www.investing.com/rss/news_14.rss", "Investing.com Economy"),
    "geopolitics": ("https://feeds.bbci.co.uk/news/world/rss.xml", "BBC World News"),
    "regulatory_coindesk": ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
    "regulatory_theblock": ("https://www.theblock.co/rss.xml", "The Block"),
}

# ---- Frozen bounds ----
MAX_ARTICLES_PER_FEED = 15  # reuses the exact existing cap already used by
# PulseWorker's own parseRssTitles (`items.length >= 15) break` -- verified
# in source, not invented fresh here. CORRECTED per review: this caps
# QUALIFYING candidates (post-window-filter) per feed per event, not raw
# RSS item order -- applying it before the window filter would bias
# evidence toward whatever ordering a feed happens to use.
MAX_ARTICLES_STORED_PER_EVENT = 40  # bounded even though 5 feeds x 15
# qualifying items = 75 max candidates after window filtering -- generous
# but capped.
MAX_RETRIES_PER_FEED = 1  # exactly one retry, on failure only, never on success
WINDOW_LOOKBACK_MS = 48 * 3600000  # reuses event_detector.MAX_OBSERVATION_GAP_MS's
# own 48h, for consistency across the research modules
WINDOW_LOOKAHEAD_MS = 24 * 3600000
SAME_WINDOW_TOLERANCE_MS = 1 * 3600000  # +/-1h band around event_ts


def _normalize(s):
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _content_hash(publisher, article_url, publication_ts, headline):
    """Frozen canonicalization -- identical input always produces the
    identical hash, verified by a dedicated determinism test, not just
    documented here."""
    payload = "\n".join([
        _normalize(publisher),
        _normalize(article_url),
        str(publication_ts),
        _normalize(headline),
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def in_event_window(publication_ts, event_ts):
    """Inclusive on both ends, exactly as specified for review:
    event_ts - 48h <= publication_ts <= event_ts + 24h."""
    return (event_ts - WINDOW_LOOKBACK_MS) <= publication_ts <= (event_ts + WINDOW_LOOKAHEAD_MS)


def classify_relation(publication_ts, event_ts):
    """Frozen exactly as specified for review -- SAME_WINDOW is a terminal
    classification, never subsequently reinterpreted as pre/post."""
    if publication_ts < event_ts - SAME_WINDOW_TOLERANCE_MS:
        return "PRE_EVENT"
    if publication_ts > event_ts + SAME_WINDOW_TOLERANCE_MS:
        return "POST_EVENT"
    return "SAME_WINDOW"


def _parse_pubdate_ms(pubdate_str):
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def parse_rss_items(xml_text, feed_url, publisher):
    """Regex-based, matching the same resilience-over-strictness approach
    already used in PulseWorker's own parser (verified in source) -- RSS
    feeds vary enough in namespace/encoding quirks that a lenient regex
    extraction is more robust here than a strict XML parser. Extracts
    title, link, and pubDate -- the exact three fields the existing
    parseRssTitles discards, which is why this parser exists separately
    rather than reusing that one."""
    items = []
    blocks = re.findall(r"<item[\s\S]*?</item>", xml_text)
    # No cap here -- per review, truncating raw RSS items before applying
    # the event window would bias evidence toward whatever ordering a feed
    # happens to use. The 15-per-feed cap is applied downstream, in
    # collect_evidence_for_event, AFTER the event window filter -- against
    # qualifying candidates, not raw feed order.
    # RAW_SAFETY_BOUND below is a 200-item raw parsing safety bound against
    # a pathological feed -- it is NOT the evidence cap. The evidence cap
    # remains exactly 15 qualifying articles/feed/event, applied later.
    RAW_SAFETY_BOUND = 200
    for block in blocks[:RAW_SAFETY_BOUND]:
        title_m = re.search(r"<title>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</title>", block)
        link_m = re.search(r"<link>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</link>", block)
        pubdate_m = re.search(r"<pubDate>(?:<!\[CDATA\[)?([\s\S]*?)(?:\]\]>)?</pubDate>", block)
        if not title_m or not link_m or not pubdate_m:
            continue  # skip items missing required provenance -- never fabricate a timestamp
        publication_ts = _parse_pubdate_ms(pubdate_m.group(1).strip())
        if publication_ts is None:
            continue
        items.append({
            "feed_url": feed_url,
            "article_url": link_m.group(1).strip(),
            "publisher": publisher,
            "headline": title_m.group(1).strip(),
            "publication_ts": publication_ts,
        })
    return items


def _default_fetcher(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return resp.status, resp.read().decode("utf-8", errors="replace")


def _is_expected_uniqueness_conflict(exc):
    """Distinguishes the ONE expected conflict -- UNIQUE(event_id,
    content_hash), meaning this exact article was already collected for
    this exact event -- from every other kind of sqlite3.IntegrityError
    (NOT NULL, FOREIGN KEY, CHECK, or any other integrity failure), which
    must propagate rather than being miscounted as a duplicate.

    Verified directly against real sqlite3 exception message formats
    (not assumed): a UNIQUE violation reads exactly
    'UNIQUE constraint failed: research_event_evidence.event_id,
    research_event_evidence.content_hash'; NOT NULL reads
    'NOT NULL constraint failed: <table>.<column>'; FOREIGN KEY reads
    'FOREIGN KEY constraint failed' with no column detail at all. These
    prefixes are distinct and stable in sqlite3's own error reporting."""
    msg = str(exc)
    return (
        msg.startswith("UNIQUE constraint failed")
        and "event_id" in msg
        and "content_hash" in msg
    )


def _fetch_feed_with_retry(feed_url, fetcher, counters):
    """1 retry per feed, on failure only, never on success. Fails closed
    for this one feed (returns None) without aborting the whole run."""
    for attempt in range(MAX_RETRIES_PER_FEED + 1):
        counters["http_requests"] += 1
        try:
            status, body = fetcher(feed_url)
            if status == 200:
                counters["successful_responses"] += 1
                return body
            counters["failed_responses"] += 1
        except Exception:
            counters["failed_responses"] += 1
    return None


def score_headline_optional(headline):
    """Optional enrichment. Deliberately NOT wired to V1's SENT_LEXICON
    port in this PR -- that lexicon lives client-side in a different repo
    and porting it is a separate, explicitly out-of-scope concern per
    review ("don't make PR4's evidence layer a V1 scoring layer"). Returns
    None always in this version; the column and code path exist so a
    later, separately-reviewed enrichment can be added without a schema
    change or touching the collection/storage logic below."""
    return None


def collect_evidence_for_event(conn, event_id, event_ts, fetcher=None):
    """Writes to research_event_evidence ONLY -- no other table. Returns
    the 9 frozen counters. Deterministic given the same fetcher output and
    the same event_ts: identical article identity, timestamps, hash, and
    relation every time (collection_ts is the sole intentional exception)."""
    if fetcher is None:
        fetcher = _default_fetcher

    counters = {
        "feeds_attempted": 0, "http_requests": 0,
        "successful_responses": 0, "failed_responses": 0,
        "articles_seen": 0, "articles_new": 0, "articles_deduplicated": 0,
        "articles_stored": 0, "articles_skipped_by_cap": 0,
    }

    for _key, (feed_url, publisher) in FEEDS.items():
        counters["feeds_attempted"] += 1
        body = _fetch_feed_with_retry(feed_url, fetcher, counters)
        if body is None:
            continue  # fail closed for this feed, continue with the others

        # CORRECTED per review: parse the full feed first, THEN filter by
        # the event window, THEN apply the per-feed cap to the resulting
        # QUALIFYING candidates -- not to raw RSS item order. A relevant
        # older article must not be discarded just because a feed lists
        # 15+ unrelated recent items ahead of it.
        qualifying_this_feed = 0
        for item in parse_rss_items(body, feed_url, publisher):
            counters["articles_seen"] += 1
            if not in_event_window(item["publication_ts"], event_ts):
                continue  # outside the frozen window -- not a candidate at all

            if qualifying_this_feed >= MAX_ARTICLES_PER_FEED:
                counters["articles_skipped_by_cap"] += 1
                continue
            if counters["articles_stored"] >= MAX_ARTICLES_STORED_PER_EVENT:
                counters["articles_skipped_by_cap"] += 1
                continue

            qualifying_this_feed += 1
            content_hash = _content_hash(
                item["publisher"], item["article_url"], item["publication_ts"], item["headline"]
            )
            relation = classify_relation(item["publication_ts"], event_ts)
            collection_ts = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
            keyword_score = score_headline_optional(item["headline"])

            counters["articles_new"] += 1
            try:
                conn.execute(
                    "INSERT INTO research_event_evidence "
                    "(event_id, feed_url, article_url, publisher, publication_ts, collection_ts, "
                    "headline, keyword_score, evidence_relation, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (event_id, item["feed_url"], item["article_url"], item["publisher"],
                     item["publication_ts"], collection_ts, item["headline"], keyword_score,
                     relation, content_hash),
                )
                counters["articles_stored"] += 1
            except sqlite3.IntegrityError as e:
                if _is_expected_uniqueness_conflict(e):
                    counters["articles_deduplicated"] += 1
                else:
                    # NOT NULL, FOREIGN KEY, CHECK, or any other integrity
                    # failure -- NOT the same thing as an already-collected
                    # duplicate. Must propagate, not be silently miscounted.
                    raise

    return counters
