"""
Tests for research/sentiment_archive.py (Experiment 5, Part 1).

All tests execute against a real, in-memory SQLite database shaped
like the proposed migration 0015 schema (mirrors test_hypothesis_gate.py's
/test_source_analysis.py's own fixture convention). No network, no D1
connection.

Run with: python3 -m pytest research/test_sentiment_archive.py -v
"""
import inspect
import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import sentiment_archive as sa  # noqa: E402

DAY = 24 * 3600000


def fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE research_sentiment_archive (
        archive_id INTEGER PRIMARY KEY AUTOINCREMENT,
        observation_ts INTEGER NOT NULL,
        sources_json TEXT,
        score REAL,
        technical_score REAL,
        btc_price REAL,
        gold_regime TEXT,
        source_weights_version TEXT NOT NULL,
        schema_version TEXT NOT NULL,
        written_by TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        archived_ts INTEGER NOT NULL
    )""")
    conn.execute("CREATE UNIQUE INDEX idx_research_sentiment_archive_observation_ts ON research_sentiment_archive(observation_ts)")
    conn.execute("CREATE INDEX idx_research_sentiment_archive_archived_ts ON research_sentiment_archive(archived_ts)")
    return conn


def _archive(conn, ts, sources=None, score=50, weights_version="v1", written_by="test", archived_ts=1000):
    return sa.archive_observation(
        conn, ts, sources or {"fng": 50}, score,
        source_weights_version=weights_version, written_by=written_by, archived_ts=archived_ts,
    )


class TestInsertion:
    def test_basic_insert_round_trips(self):
        conn = fresh_db()
        archive_id = _archive(conn, 1000, {"fng": 60, "onchain": 90}, score=55)
        rows = sa.get_archive_range(conn, 0, 2000)
        assert len(rows) == 1
        assert rows[0]["archive_id"] == archive_id
        assert rows[0]["observation_ts"] == 1000
        assert rows[0]["sources"] == {"fng": 60, "onchain": 90}
        assert rows[0]["score"] == 55

    def test_sources_json_preserved_exactly_not_renormalized(self):
        conn = fresh_db()
        sa.archive_observation(
            conn, 1000, {"z": 1, "a": 2}, 50,
            source_weights_version="v1", written_by="test", archived_ts=1000,
        )
        row = sa.get_archive_range(conn, 0, 2000)[0]
        assert row["sources"] == {"z": 1, "a": 2}

    def test_provenance_fields_required(self):
        conn = fresh_db()
        with pytest.raises(ValueError):
            sa.archive_observation(conn, 1000, {}, 50, source_weights_version=None, written_by="x", archived_ts=1)
        with pytest.raises(ValueError):
            sa.archive_observation(conn, 1000, {}, 50, source_weights_version="v1", written_by=None, archived_ts=1)
        with pytest.raises(ValueError):
            sa.archive_observation(conn, 1000, {}, 50, source_weights_version="v1", written_by="x", archived_ts=None)


class TestIdempotency:
    def test_identical_repeat_is_a_noop_returns_same_id(self):
        conn = fresh_db()
        id1 = _archive(conn, 1000, {"fng": 60}, score=55)
        id2 = _archive(conn, 1000, {"fng": 60}, score=55)
        assert id1 == id2
        rows = sa.get_archive_range(conn, 0, 2000)
        assert len(rows) == 1  # never inserted a second row

    def test_repeat_with_different_content_raises_conflict_never_overwrites(self):
        conn = fresh_db()
        _archive(conn, 1000, {"fng": 60}, score=55)
        with pytest.raises(sa.ArchiveConflictError):
            _archive(conn, 1000, {"fng": 61}, score=55)  # different sources
        # original row must be completely untouched
        rows = sa.get_archive_range(conn, 0, 2000)
        assert len(rows) == 1
        assert rows[0]["sources"] == {"fng": 60}
        assert rows[0]["score"] == 55

    def test_different_score_same_ts_also_raises_conflict(self):
        conn = fresh_db()
        _archive(conn, 1000, {"fng": 60}, score=55)
        with pytest.raises(sa.ArchiveConflictError):
            _archive(conn, 1000, {"fng": 60}, score=99)


class TestAppendOnly:
    def test_module_never_issues_update_or_delete(self):
        source = inspect.getsource(sa)
        for forbidden in ["UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE"]:
            assert forbidden not in source, f"sentiment_archive.py must never issue {forbidden!r}"

    def test_two_different_timestamps_both_persist_independently(self):
        conn = fresh_db()
        _archive(conn, 1000, {"fng": 60}, score=55)
        _archive(conn, 2000, {"fng": 65}, score=60)
        rows = sa.get_archive_range(conn, 0, 3000)
        assert len(rows) == 2
        assert [r["observation_ts"] for r in rows] == [1000, 2000]


class TestContentHash:
    def test_deterministic_identical_input_identical_hash(self):
        h1 = sa.compute_content_hash(1000, '{"fng":60}', 55, None, None, None, "v1", "exp5-v1")
        h2 = sa.compute_content_hash(1000, '{"fng":60}', 55, None, None, None, "v1", "exp5-v1")
        assert h1 == h2

    def test_different_input_different_hash(self):
        h1 = sa.compute_content_hash(1000, '{"fng":60}', 55, None, None, None, "v1", "exp5-v1")
        h2 = sa.compute_content_hash(1000, '{"fng":61}', 55, None, None, None, "v1", "exp5-v1")
        assert h1 != h2

    def test_content_hash_stored_and_retrievable(self):
        conn = fresh_db()
        _archive(conn, 1000, {"fng": 60}, score=55)
        row = sa.get_archive_range(conn, 0, 2000)[0]
        assert len(row["content_hash"]) == 64  # sha256 hex digest


class TestRangeRead:
    def test_range_read_bounds_are_required(self):
        conn = fresh_db()
        with pytest.raises(ValueError):
            sa.get_archive_range(conn, None, 1000)
        with pytest.raises(ValueError):
            sa.get_archive_range(conn, 0, None)

    def test_range_read_excludes_rows_outside_bounds(self):
        conn = fresh_db()
        _archive(conn, 1000, {"fng": 60}, score=55)
        _archive(conn, 5000, {"fng": 70}, score=60)
        rows = sa.get_archive_range(conn, 0, 2000)
        assert len(rows) == 1
        assert rows[0]["observation_ts"] == 1000
