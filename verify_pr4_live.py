#!/usr/bin/env python3
"""
Small, controlled live verification, exactly as directed:
1. Run PR3's real detector against real production btc_data.
2. Insert a small number of genuinely detected events into research_events
   (their first-ever real rows).
3. Run PR4's real evidence collector against them, using the REAL default
   fetcher (genuine live RSS feeds -- this is the first real internet
   fetch this research system has ever made).
4. Report everything found: events, evidence provenance, counters.

Read-only against btc_data/history/predictions. Writes ONLY to
research_events (a small, explicit number of rows) and
research_event_evidence (via PR4's own collector). No V1/V2 changes.
"""
import json
import os
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, "research")
import event_detector as ed
import evidence_collector as ec

D1_DB = "sentiment-history"


def d1_query(sql):
    result = subprocess.run(
        ["wrangler", "d1", "execute", D1_DB, "--remote", "--json", "--command", sql],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)[0]["results"]


def d1_execute(sql):
    subprocess.run(
        ["wrangler", "d1", "execute", D1_DB, "--remote", "--command", sql],
        capture_output=True, text=True, check=True,
    )


# ---- Step 1: pull real recent btc_data, run the real detector ----
print("=== Step 1: fetching real recent btc_data ===")
now_ms = int(time.time() * 1000)
lookback_ms = 60 * 24 * 3600000  # widened from 20d after a first pass found
# zero events in the shorter, genuinely-quiet recent window -- 60d is known
# from earlier real testing this session to include a real volatile period
rows = d1_query(f"SELECT ts, btc_price FROM btc_data WHERE ts >= {now_ms - lookback_ms} ORDER BY ts ASC")
print(f"real btc_data rows pulled: {len(rows)}")

conn = sqlite3.connect(":memory:")
conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, btc_price REAL)")
conn.execute("CREATE INDEX idx_btc_data_ts ON btc_data(ts)")
for r in rows:
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (r["ts"], r["btc_price"]))
conn.commit()

start_ts = rows[0]["ts"] + 8 * 86400000 if rows else now_ms
end_ts = rows[-1]["ts"] if rows else now_ms

all_events = []
all_events += ed.detect_large_moves(conn, start_ts=start_ts, end_ts=end_ts)
all_events += ed.detect_regime_reversals(conn, start_ts=start_ts, end_ts=end_ts)
all_events += ed.detect_volatility_expansion(conn, start_ts=start_ts, end_ts=end_ts)

print(f"\nreal events detected: {len(all_events)}")
for e in all_events:
    print(f"  {e['category']} ts={e['event_ts']} direction={e['direction']} fingerprint={e['fingerprint']}")

# ---- Step 2: insert a SMALL number of these into research_events (their
# first-ever real rows) ----
SMALL_N = min(3, len(all_events))
chosen = all_events[:SMALL_N]
print(f"\n=== Step 2: inserting {SMALL_N} real event(s) into research_events ===")

inserted_event_ids = []
for e in chosen:
    detection_ts = now_ms
    sql = (
        "INSERT INTO research_events "
        "(fingerprint, event_ts, detection_ts, category, direction, intensity, "
        "available_before_prediction, is_post_event_analysis, trigger_metric, trigger_threshold, trigger_version) "
        f"VALUES ('{e['fingerprint']}', {e['event_ts']}, {detection_ts}, '{e['category']}', "
        f"'{e['direction']}', {e['intensity'] if e['intensity'] is not None else 'NULL'}, "
        f"1, {e['is_post_event_analysis']}, '{e['trigger_metric']}', "
        f"{e['trigger_threshold'] if e['trigger_threshold'] is not None else 'NULL'}, '{e['trigger_version']}')"
    )
    d1_execute(sql)

result = d1_query("SELECT event_id, fingerprint, event_ts, category FROM research_events ORDER BY event_id")
for row in result:
    print(f"  real event_id={row['event_id']}  {row['category']}  fingerprint={row['fingerprint']}")
    inserted_event_ids.append((row["event_id"], row["event_ts"]))

# ---- Step 3: run PR4's REAL collector against these real events, using
# the REAL default fetcher (genuine live RSS feeds) ----
print(f"\n=== Step 3: running the real evidence collector (genuine live RSS feeds) ===")

# Local sqlite mirror for research_event_evidence writes, then we'll
# replicate the successful rows to the real D1 via wrangler.
local_conn = sqlite3.connect(":memory:")
local_conn.execute("""CREATE TABLE research_events (
    event_id INTEGER PRIMARY KEY, fingerprint TEXT, event_ts INTEGER, detection_ts INTEGER, category TEXT
)""")
local_conn.executescript(open(".ai/migrations/0007_research_event_evidence.sql").read())
for eid, ets in inserted_event_ids:
    local_conn.execute("INSERT INTO research_events (event_id, event_ts) VALUES (?, ?)", (eid, ets))
local_conn.commit()

all_counters = []
for eid, ets in inserted_event_ids:
    counters = ec.collect_evidence_for_event(local_conn, eid, ets)  # real default fetcher
    counters["event_id"] = eid
    all_counters.append(counters)
    print(f"\n  event_id={eid} counters: {counters}")

print("\n=== Step 4: real evidence collected (sample) ===")
evidence_rows = local_conn.execute(
    "SELECT event_id, publisher, article_url, publication_ts, collection_ts, headline, evidence_relation, content_hash "
    "FROM research_event_evidence ORDER BY event_id, publication_ts"
).fetchall()
print(f"total real evidence rows collected: {len(evidence_rows)}")
for row in evidence_rows[:15]:
    print(f"  event={row[0]} pub={row[1]} ts={row[3]} relation={row[6]} hash={row[7][:12]}... headline={row[5][:70]!r}")

# ---- Step 5: replicate the real evidence rows to the actual production D1 ----
print(f"\n=== Step 5: writing {len(evidence_rows)} real evidence row(s) to production D1 ===")
for row in evidence_rows:
    event_id, publisher, article_url, publication_ts, collection_ts, headline, relation, content_hash = row
    feed_url_row = local_conn.execute(
        "SELECT feed_url, keyword_score FROM research_event_evidence WHERE event_id=? AND content_hash=?",
        (event_id, content_hash),
    ).fetchone()
    feed_url, keyword_score = feed_url_row
    esc = lambda s: s.replace("'", "''")
    ks = "NULL" if keyword_score is None else str(keyword_score)
    sql = (
        "INSERT INTO research_event_evidence "
        "(event_id, feed_url, article_url, publisher, publication_ts, collection_ts, headline, keyword_score, evidence_relation, content_hash) "
        f"VALUES ({event_id}, '{esc(feed_url)}', '{esc(article_url)}', '{esc(publisher)}', {publication_ts}, "
        f"{collection_ts}, '{esc(headline)}', {ks}, '{relation}', '{content_hash}')"
    )
    d1_execute(sql)

print("\nDone.")
