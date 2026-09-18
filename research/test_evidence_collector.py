"""
Tests for research/evidence_collector.py. All fetching is injected via a
fake fetcher -- zero live network calls anywhere in this file, matching
the same no-live-internet-in-tests discipline as the rest of this project.

Run with: python3 -m pytest research/test_evidence_collector.py -v
"""
import inspect
import os
import re
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import evidence_collector as ec  # noqa: E402


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE research_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
        event_ts INTEGER NOT NULL, detection_ts INTEGER NOT NULL, category TEXT NOT NULL
    )""")
    conn.executescript(open(
        os.path.join(os.path.dirname(__file__), "..", ".ai", "migrations", "0007_research_event_evidence.sql")
    ).read())
    return conn


DAY = 24 * 3600000
HOUR = 3600000


def rss_xml(items):
    """items: list of (title, link, pubdate_rfc822_str)"""
    body = "".join(
        f"<item><title>{t}</title><link>{l}</link><pubDate>{p}</pubDate></item>"
        for t, l, p in items
    )
    return f"<rss><channel>{body}</channel></rss>"


def make_fetcher(per_feed_xml, fail_feeds=None, fail_count=None):
    """per_feed_xml: {feed_url: xml_string}. fail_feeds: set of feed_urls
    that should return non-200 (or raise) every call. fail_count: dict of
    feed_url -> number of times to fail before succeeding (for retry tests)."""
    fail_feeds = fail_feeds or set()
    fail_count = dict(fail_count or {})
    calls = {"n": 0}

    def fetcher(url):
        calls["n"] += 1
        if url in fail_feeds:
            return 502, ""
        if url in fail_count and fail_count[url] > 0:
            fail_count[url] -= 1
            return 502, ""
        return 200, per_feed_xml.get(url, rss_xml([]))

    fetcher.calls = calls
    return fetcher


ALL_FEED_URLS = [u for u, _ in ec.FEEDS.values()]


def rfc822(ms):
    from email.utils import formatdate
    return formatdate(ms / 1000, usegmt=True)


# ---- 1. Exact PRE_EVENT / SAME_WINDOW / POST_EVENT boundaries ----

def test_relation_boundaries_exact():
    event_ts = 100_000_000_000
    assert ec.classify_relation(event_ts - ec.SAME_WINDOW_TOLERANCE_MS - 1, event_ts) == "PRE_EVENT"
    assert ec.classify_relation(event_ts - ec.SAME_WINDOW_TOLERANCE_MS, event_ts) == "SAME_WINDOW"
    assert ec.classify_relation(event_ts, event_ts) == "SAME_WINDOW"
    assert ec.classify_relation(event_ts + ec.SAME_WINDOW_TOLERANCE_MS, event_ts) == "SAME_WINDOW"
    assert ec.classify_relation(event_ts + ec.SAME_WINDOW_TOLERANCE_MS + 1, event_ts) == "POST_EVENT"


# ---- 2. Exact event-window boundaries ----

def test_event_window_boundaries_inclusive():
    event_ts = 100_000_000_000
    assert ec.in_event_window(event_ts - ec.WINDOW_LOOKBACK_MS, event_ts) is True
    assert ec.in_event_window(event_ts - ec.WINDOW_LOOKBACK_MS - 1, event_ts) is False
    assert ec.in_event_window(event_ts + ec.WINDOW_LOOKAHEAD_MS, event_ts) is True
    assert ec.in_event_window(event_ts + ec.WINDOW_LOOKAHEAD_MS + 1, event_ts) is False


# ---- 3. Canonical hash determinism ----

def test_content_hash_deterministic_and_normalized():
    h1 = ec._content_hash("CoinTelegraph", "https://x.com/a", 1000, "Bitcoin Rises")
    h2 = ec._content_hash("cointelegraph", "https://x.com/a", 1000, "  bitcoin   rises  ")
    assert h1 == h2
    h3 = ec._content_hash("CoinTelegraph", "https://x.com/a", 1000, "Bitcoin Falls")
    assert h1 != h3


# ---- 4. Duplicate article handling (same event) ----

def test_duplicate_article_same_event_is_deduplicated_not_double_stored():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    pubdate = rfc822(event_ts)
    xml = rss_xml([("Bitcoin surges", "https://a.com/1", pubdate)])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    counters1 = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters1["articles_stored"] == 1
    counters2 = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters2["articles_stored"] == 0
    assert counters2["articles_deduplicated"] == 1
    n_rows = conn.execute("SELECT COUNT(*) FROM research_event_evidence").fetchone()[0]
    assert n_rows == 1
    conn.close()


# ---- 5. Same article associated with multiple events ----

def test_same_article_can_be_stored_for_two_different_events():
    conn = fresh_db()
    e1_ts, e2_ts = 100_000_000_000, 100_000_000_000 + 10 * HOUR
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (e1_ts, e1_ts))
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp2', ?, ?, 'LARGE_MOVE')", (e2_ts, e2_ts))
    conn.commit()
    pubdate = rfc822(e1_ts + 5 * HOUR)
    xml = rss_xml([("Shared headline", "https://a.com/shared", pubdate)])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    c1 = ec.collect_evidence_for_event(conn, 1, e1_ts, fetcher=fetcher)
    c2 = ec.collect_evidence_for_event(conn, 2, e2_ts, fetcher=fetcher)
    assert c1["articles_stored"] == 1
    assert c2["articles_stored"] == 1
    rows = conn.execute("SELECT event_id FROM research_event_evidence ORDER BY event_id").fetchall()
    assert [r[0] for r in rows] == [1, 2]
    conn.close()


# ---- 6. Optional keyword scoring failure does not prevent storage ----

def test_storage_succeeds_even_though_scoring_returns_none():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    xml = rss_xml([("Some headline", "https://a.com/1", rfc822(event_ts))])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_stored"] == 1
    row = conn.execute("SELECT keyword_score FROM research_event_evidence").fetchone()
    assert row[0] is None
    conn.close()


def test_score_headline_optional_never_raises():
    result = ec.score_headline_optional("any headline at all")
    assert result is None


# ---- 7. Retry policy ----

def test_retry_once_then_succeeds():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml}, fail_count={ALL_FEED_URLS[0]: 1})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_stored"] == 1
    assert counters["failed_responses"] == 1  # the one failed attempt on the retried feed
    assert counters["successful_responses"] == len(ALL_FEED_URLS)  # all 5 feeds eventually succeed
    conn.close()


def test_no_retry_after_success_never_calls_twice_on_first_try():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    fetcher = make_fetcher({u: xml for u in ALL_FEED_URLS})
    ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert fetcher.calls["n"] == len(ALL_FEED_URLS)
    conn.close()


def test_feed_fails_twice_fails_closed_for_that_feed_only():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    good_xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    per_feed = {u: good_xml for u in ALL_FEED_URLS}
    fetcher = make_fetcher(per_feed, fail_feeds={ALL_FEED_URLS[0]})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["failed_responses"] == 2
    assert counters["articles_stored"] == len(ALL_FEED_URLS) - 1
    conn.close()


# ---- 8. Request/article caps and fail-closed behavior ----

def test_cap_applies_after_window_filter_not_to_raw_feed_order():
    """The exact bug from review: a feed listing 10 items OUTSIDE the
    event window before 10 items INSIDE it must not lose the inside-window
    ones to a raw-order truncation. All 10 qualifying items must be found."""
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    outside_items = [(f"Unrelated{i}", f"https://a.com/out{i}", rfc822(event_ts + 100 * DAY))
                      for i in range(10)]
    inside_items = [(f"Relevant{i}", f"https://a.com/in{i}", rfc822(event_ts))
                     for i in range(10)]
    # Outside-window items listed FIRST in feed order, deliberately
    xml = rss_xml(outside_items + inside_items)
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_seen"] == 20
    assert counters["articles_stored"] == 10, (
        "all 10 inside-window items must be found and stored, regardless of "
        "their position after 10 unrelated items earlier in the feed"
    )
    conn.close()


def test_per_feed_cap_applies_to_qualifying_candidates_not_raw_items():
    """25 items, ALL inside the window (so all 25 are qualifying candidates)
    -- the cap of 15 must apply here, to the qualifying set, not be silently
    bypassed or misapplied because there are more than 15 raw items."""
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    items = [(f"H{i}", f"https://a.com/{i}", rfc822(event_ts)) for i in range(25)]
    xml = rss_xml(items)
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_seen"] == 25
    assert counters["articles_stored"] == ec.MAX_ARTICLES_PER_FEED
    assert counters["articles_skipped_by_cap"] == 25 - ec.MAX_ARTICLES_PER_FEED
    conn.close()


def test_parse_rss_items_no_longer_truncates_raw_items():
    """Structural check: parse_rss_items itself must return all real items
    up to the generous safety bound, not the old 15-item cap -- the cap
    is now the caller's job, applied after window filtering."""
    xml_items = [(f"H{i}", f"https://a.com/{i}", rfc822(100_000_000_000)) for i in range(25)]
    xml = rss_xml(xml_items)
    parsed = ec.parse_rss_items(xml, "https://feed", "Pub")
    assert len(parsed) == 25, "parse_rss_items must not truncate at MAX_ARTICLES_PER_FEED anymore"


def test_overall_per_event_storage_cap():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    per_feed = {}
    for i, u in enumerate(ALL_FEED_URLS):
        items = [(f"F{i}H{j}", f"https://a.com/{i}/{j}", rfc822(event_ts)) for j in range(15)]
        per_feed[u] = rss_xml(items)
    fetcher = make_fetcher(per_feed)
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_stored"] == ec.MAX_ARTICLES_STORED_PER_EVENT
    assert counters["articles_skipped_by_cap"] == (5 * 15) - ec.MAX_ARTICLES_STORED_PER_EVENT
    n_rows = conn.execute("SELECT COUNT(*) FROM research_event_evidence").fetchone()[0]
    assert n_rows == ec.MAX_ARTICLES_STORED_PER_EVENT
    conn.close()


def test_articles_outside_window_are_not_seen_as_candidates_at_all():
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    far_outside = rfc822(event_ts - 100 * DAY)
    xml = rss_xml([("Old news", "https://a.com/1", far_outside)])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    assert counters["articles_seen"] == 1
    assert counters["articles_stored"] == 0
    conn.close()


# ---- Error handling: only the expected UNIQUE conflict is a "duplicate" ----

def test_unexpected_db_error_propagates_not_silently_counted_as_duplicate():
    """The exact bug from review: a genuine, unexpected database error must
    raise, not be swallowed and miscounted as a successful deduplication."""
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    # Break the table AFTER setup so the INSERT itself fails for a reason
    # that has nothing to do with the UNIQUE(event_id, content_hash)
    # constraint -- a missing required column.
    conn.execute("DROP TABLE research_event_evidence")
    conn.execute("CREATE TABLE research_event_evidence (evidence_id INTEGER PRIMARY KEY)")
    xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    with pytest.raises(Exception):
        ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)
    conn.close()


def test_genuine_uniqueness_conflict_is_still_counted_as_duplicate():
    """Confirms the fix didn't break the legitimate, expected case --
    IntegrityError from the real UNIQUE(event_id, content_hash) constraint
    is still correctly classified as a duplicate, not raised."""
    conn = fresh_db()
    event_ts = 100_000_000_000
    conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                 "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
    conn.commit()
    xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    fetcher = make_fetcher({ALL_FEED_URLS[0]: xml})
    ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)  # first run: stores it
    counters = ec.collect_evidence_for_event(conn, 1, event_ts, fetcher=fetcher)  # second: dupe
    assert counters["articles_deduplicated"] == 1
    conn.close()


# ---- 9. Deterministic rerun ----

def test_deterministic_rerun_identical_except_collection_ts():
    conn1, conn2 = fresh_db(), fresh_db()
    event_ts = 100_000_000_000
    for conn in (conn1, conn2):
        conn.execute("INSERT INTO research_events (fingerprint, event_ts, detection_ts, category) "
                     "VALUES ('fp1', ?, ?, 'LARGE_MOVE')", (event_ts, event_ts))
        conn.commit()
    xml = rss_xml([("Headline", "https://a.com/1", rfc822(event_ts))])
    ec.collect_evidence_for_event(conn1, 1, event_ts, fetcher=make_fetcher({ALL_FEED_URLS[0]: xml}))
    ec.collect_evidence_for_event(conn2, 1, event_ts, fetcher=make_fetcher({ALL_FEED_URLS[0]: xml}))
    r1 = conn1.execute("SELECT article_url, publication_ts, content_hash, evidence_relation, event_id "
                       "FROM research_event_evidence").fetchone()
    r2 = conn2.execute("SELECT article_url, publication_ts, content_hash, evidence_relation, event_id "
                       "FROM research_event_evidence").fetchone()
    assert r1 == r2
    conn1.close()
    conn2.close()


# ---- 10. No writes outside research_event_evidence ----

def test_module_only_writes_to_research_event_evidence():
    src = inspect.getsource(ec)
    assert "INSERT INTO research_event_evidence" in src
    for forbidden_table in ["research_events", "research_analyses", "history", "predictions",
                             "selection_decisions", "btc_data"]:
        assert f"INSERT INTO {forbidden_table}" not in src
        assert f"UPDATE {forbidden_table}" not in src
        assert f"DELETE FROM {forbidden_table}" not in src


def test_module_never_alters_schema():
    src = inspect.getsource(ec)
    assert "CREATE TABLE" not in src
    assert "ALTER TABLE" not in src
    assert "DROP TABLE" not in src


# ---- 11. No LLM/network dependency beyond the fixed RSS feeds ----

def test_no_llm_references_anywhere():
    src = inspect.getsource(ec)
    for forbidden in ["openai", "gemini", "anthropic", "claude", "gpt"]:
        assert forbidden not in src.lower()


def test_only_the_five_frozen_feed_urls_are_ever_referenced():
    src = inspect.getsource(ec)
    urls_in_source = set(re.findall(r"https?://[^\s\"']+", src))
    expected = {u for u, _ in ec.FEEDS.values()}
    assert urls_in_source == expected, f"unexpected URL(s) in module: {urls_in_source - expected}"


def test_fixed_feed_set_has_exactly_five_entries_four_categories():
    assert len(ec.FEEDS) == 5
    publishers = {p for _, p in ec.FEEDS.values()}
    assert publishers == {"CoinTelegraph", "Investing.com Economy", "BBC World News", "CoinDesk", "The Block"}
