#!/usr/bin/env python3
"""Historical analysis over the staged archive with DuckDB, with no D1, no credentials and no network.

    python3 research/archive/offline_queries.py --stage STAGE --repo . [--out report.json]

Every query reads Parquet from 20_parquet/. The market-move candles are counted only (no returns), and only
rows available before the sealed evaluation period starts are read, so nothing here can expose sealed
evaluation results.
"""
import argparse
import json
import socket
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive  # noqa: E402

EVAL_SEAL_MS = 1784678400000          # 2026-07-22T00:00:00Z, evaluation_plan.json split.evaluation_scored_utc[0]


def block_network():
    """Fail loudly if anything below tries to open a socket."""
    def refuse(*_a, **_k):
        raise RuntimeError("network access attempted during an offline archive run")
    socket.socket.connect = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def pq_glob(root, table, dataset, pattern="*.parquet"):
    return str(root / "20_parquet" / table / f"dataset={dataset}" / "*" / pattern)


def run(stage, repo):
    import duckdb
    root = Path(stage) / archive.ARCHIVE_ROOT
    # in-memory; never downloads or auto-loads an extension (httpfs etc.), so only local files are readable
    con = duckdb.connect(config={"autoinstall_known_extensions": False, "autoload_known_extensions": False})
    out = {}

    # 1. Candle coverage per asset and month (replaces the monthly gap query that read 142,396 D1 rows).
    out["candle_coverage_development"] = con.execute(f"""
        WITH c AS (
          SELECT asset, CAST(open_ts AS BIGINT) AS t FROM read_parquet('{pq_glob(root, 'candles_1h', 'mm_candles_1h')}')
          WHERE _available_at_ms < {EVAL_SEAL_MS})
        SELECT asset, strftime(make_timestamp(t * 1000), '%Y-%m') AS month, count(*) AS candles,
               (max(t) - min(t)) / 3600000 + 1 - count(*) AS missing_hours
        FROM c GROUP BY ALL ORDER BY asset, month""").fetchall()

    # 2. Production btc_data extract: readings per UTC day (the B0 'not_evaluable' input check, offline).
    out["btc_data_days"] = con.execute(f"""
        SELECT count(*) AS days, min(n) AS min_readings, max(n) AS max_readings,
               sum(CASE WHEN n = 0 THEN 1 ELSE 0 END) AS empty_days
        FROM (SELECT day, count(r.ts) AS n
              FROM (SELECT unnest(range(min_d, max_d + 1)) AS day FROM
                     (SELECT min(_ts_ms) // 86400000 AS min_d, max(_ts_ms) // 86400000 AS max_d
                      FROM read_parquet('{pq_glob(root, 'd1_extract', 'd1_extracts_2026_10_08', 'research__archive__session_extracts__inputs__btc_data.json.parquet')}'))) d
              LEFT JOIN read_parquet('{pq_glob(root, 'd1_extract', 'd1_extracts_2026_10_08', 'research__archive__session_extracts__inputs__btc_data.json.parquet')}') r
                ON r._ts_ms // 86400000 = d.day
              GROUP BY day)""").fetchall()

    # 3. Learning exports: resolved share per prediction table (replaces repeated COUNT queries on D1).
    out["exports_resolution"] = con.execute(f"""
        SELECT regexp_extract(filename, 'data-exports__([a-z_]+)\\.csv', 1) AS export, count(*) AS rows,
               sum(CASE WHEN coalesce(resolved_ts, '') <> '' THEN 1 ELSE 0 END) AS resolved
        FROM read_parquet('{pq_glob(root, 'd1_export', 'learning_exports')}', filename = true, union_by_name = true)
        GROUP BY ALL ORDER BY export""").fetchall()

    # 4. Point-in-time join: latest Hyperliquid BTC funding available at each V1 observation (ASOF join).
    out["asof_funding_at_v1"] = con.execute(f"""
        WITH v1 AS (SELECT _ts_ms AS ts FROM read_parquet('{pq_glob(root, 'd1_extract', 'd1_extracts_2026_10_08', 'research__archive__session_extracts__inputs__v1_full.json.parquet')}')),
             f AS (SELECT _available_at_ms AS avail, _value_f64 AS funding
                   FROM read_parquet('{pq_glob(root, 'observations', 'rr_raw_observations')}')
                   WHERE source = 'hyperliquid_btc_funding' AND metric = 'funding_rate')
        SELECT count(*) AS v1_rows, count(f.funding) AS with_funding,
               max(v1.ts - f.avail) / 60000 AS max_age_minutes
        FROM v1 ASOF LEFT JOIN f ON v1.ts >= f.avail""").fetchall()

    # 5. Production event detectors, unchanged, on the archived btc_data extract (sqlite in memory).
    sys.path.insert(0, str(Path(repo) / "research"))
    import event_detector as ed
    btc = con.execute(f"""SELECT ts, btc_price FROM read_parquet('{pq_glob(root, 'd1_extract', 'd1_extracts_2026_10_08', 'research__archive__session_extracts__inputs__btc_data.json.parquet')}') ORDER BY ts""").fetchall()
    events = con.execute(f"""SELECT fingerprint, category FROM read_parquet('{pq_glob(root, 'd1_extract', 'd1_extracts_2026_10_08', 'research__archive__session_extracts__inputs__research_events.json.parquet')}')""").fetchall()
    lite = sqlite3.connect(":memory:")
    lite.execute("CREATE TABLE btc_data (ts INTEGER, btc_price REAL)")
    lite.executemany("INSERT INTO btc_data VALUES (?, ?)", btc)
    lo, hi = btc[0][0] + ed.LOOKBACK_BUFFER_MS, btc[-1][0] + 1
    found = set()
    for fn in (ed.detect_large_moves, ed.detect_regime_reversals, ed.detect_volatility_expansion):
        found |= {e["fingerprint"] for e in fn(lite, lo, hi)}
    btc_only = {"LARGE_MOVE", "REGIME_REVERSAL", "VOLATILITY_EXPANSION"}
    in_window = {fp for fp, cat in events if cat in btc_only and lo <= int(fp.split("|")[1]) < hi}
    out["event_replay"] = {"window_utc": [archive.iso(lo), archive.iso(hi)],
                           "production_events_in_window": sorted(in_window),
                           "reproduced": sorted(in_window & found),
                           "not_reproduced": sorted(in_window - found),
                           "extra_offline": sorted(found - in_window)}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", required=True)
    ap.add_argument("--repo", default=".")
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    block_network()
    res = run(a.stage, a.repo)
    text = json.dumps(res, indent=1, default=str)
    if a.out:
        Path(a.out).write_text(text + "\n")
    print(text)


if __name__ == "__main__":
    main()
