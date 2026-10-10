"""Offline research archive: immutability, exact round trips, pinned hashes, sync safety and no network."""
import gzip
import hashlib
import json
import socket
import subprocess
from pathlib import Path

import pytest

import archive

HERE = Path(__file__).resolve().parent


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """A throwaway git repo with one branch holding a frozen registration, observations and a CSV export."""
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "data")
    git(r, "config", "user.email", "t@example.invalid")
    git(r, "config", "user.name", "t")
    reg = {"registered_utc": "2026-10-09T16:00:00Z", "rule": "fixed"}
    (r / "reg.json").write_text(json.dumps(reg, indent=2))
    (r / "rules.py").write_text("import sys\nsys.exit('must never be executed')\nPREREGISTRATION = {'a': [1, 2], 'b': 'x'}\n")
    rows = [
        {"source": "hl", "instrument": "BTC", "metric": "close", "interval": "1h", "timestamp": "1790000000000",
         "available_at": "1790003600000", "retrieved_at": "1790100000000", "value": "86002.10", "raw": "{'c': 86002.1}"},
        {"source": "hl", "instrument": "BTC", "metric": "close", "interval": "1h", "timestamp": "1790003600000",
         "available_at": "1790007200000", "retrieved_at": "1790100000000", "value": "1.25e-05", "raw": "{'c': 'é'}"},
        {"source": "hl", "instrument": "BTC", "metric": "close", "interval": "1h", "timestamp": "1790014400000",
         "available_at": "1790018000000", "retrieved_at": "1790100000000", "value": "0.1", "raw": "{}"},
    ]
    (r / "obs.jsonl.gz").write_bytes(gzip.compress("".join(json.dumps(x) + "\n" for x in rows).encode()))
    (r / "export.csv").write_text("id,ts,p_up,resolved_ts\n1,2026-10-01T00:00:00Z,0.61,\n2,2026-10-02T00:00:00Z,0.40,2026-10-03T00:00:00Z\n")
    extract = [{"ts": 1, "score": 50, "sources_json": "{\"a\":1}"}, {"ts": 2, "score": 50.5, "extra": {"n": [1, None]}}]
    (r / "extract.json").write_text(json.dumps(extract))
    git(r, "add", "-A")
    git(r, "commit", "-q", "-m", "seed")
    reg_hash = hashlib.sha256(json.dumps(reg, sort_keys=True).encode()).hexdigest()
    const_hash = hashlib.sha256(json.dumps({"a": [1, 2], "b": "x"}, sort_keys=True).encode()).hexdigest()
    catalog = {"catalog_version": "t", "datasets": [
        {"id": "prereg", "status": "PREREGISTRATION", "format": "json", "provenance": "test",
         "source": {"kind": "git", "ref": "data", "paths": ["reg.json", "rules.py"]},
         "pinned_sha256": {"reg.json": {"rule": "json_sort_keys", "sha256": reg_hash},
                           "rules.py": {"rule": "python_constant_json_sort_keys:PREREGISTRATION", "sha256": const_hash}}},
        {"id": "obs", "status": "FROZEN_SNAPSHOT", "format": "jsonl.gz", "table": "observations", "provenance": "test",
         "source": {"kind": "git", "ref": "data", "paths": ["obs.jsonl.gz"]}, "time_field": "timestamp",
         "available_field": "available_at", "series_key": ["source", "instrument", "metric", "interval"],
         "step_by_interval_ms": {"1h": 3600000}},
        {"id": "exp", "status": "MUTABLE_EXPORT", "format": "csv", "table": "d1_export", "provenance": "test",
         "source": {"kind": "git", "ref": "data", "paths": ["export.csv"]}, "time_field": "ts"},
        {"id": "ext", "status": "FROZEN_SNAPSHOT", "format": "json", "table": "d1_extract", "provenance": "test",
         "source": {"kind": "git", "ref": "data", "paths": ["extract.json"]}, "time_field": "ts"},
    ]}
    return r, catalog


def test_inventory_counts_rows_ranges_gaps_and_checks_pins(repo):
    r, cat = repo
    inv = {d["id"]: d for d in archive.inventory(r, None, cat)["datasets"]}
    assert all(f["pinned"]["ok"] for f in inv["prereg"]["files"])
    obs = inv["obs"]["stats"]
    assert obs["rows"] == 3 and obs["first"] == "2026-09-21T14:13:20Z"
    (s,) = obs["series"]
    assert (s["gaps"], s["missing_steps"], s["largest_gap_h"]) == (1, 2, 3.0)
    assert inv["exp"]["stats"]["rows"] == 2 and inv["exp"]["stats"]["last"] == "2026-10-02T00:00:00Z"


def test_timestamp_jitter_is_not_a_gap():
    ds = {"time_field": "t", "series_key": ["k"], "expected_step_ms": 3600000}
    rows = [{"k": "f", "t": str(3600000 * i + (42 if i % 2 else 0))} for i in range(10)]
    (s,) = archive.series_stats(rows, ds)["series"]
    assert s["gaps"] == 0 and s["missing_steps"] == 0


def test_python_constant_is_hashed_without_executing_the_module(repo):
    r, cat = repo
    src = archive.GitSource(r, "data")
    pin = cat["datasets"][0]["pinned_sha256"]["rules.py"]
    assert archive.pinned_hash(pin["rule"], src.read("rules.py")) == pin["sha256"]   # the sys.exit never ran


def test_a_changed_registration_is_detected():
    data = json.dumps({"rule": "fixed"}).encode()
    good = archive.pinned_hash("json_sort_keys", data)
    assert archive.pinned_hash("json_sort_keys", json.dumps({"rule": "fixed "}).encode()) != good
    assert archive.pinned_hash("json_sort_keys", b'{ "rule" : "fixed" }') == good   # whitespace is not content


def test_real_catalog_pins_match_the_frozen_values_in_the_studies():
    cat = json.loads(archive.CATALOG.read_text())
    pins = {p["sha256"] for d in cat["datasets"] for p in (d.get("pinned_sha256") or {}).values()}
    for frozen in ("66067b324743bad052f4404d7ecbaaac76e6678116026ddd22dc8d73b22a266d",   # T-A1..T-A3
                   "c2ebdfcb75a1690b25f3a327bf7537f8ee6853e16a0dc6295bebd18b8ac7ae9c",   # addendum 1
                   "5397fe70cf1415aa239beb944350340a48d711244e4d16b1f18edc8c313cb15d",   # erratum 1
                   "46c0d52b47350a7c7e83841e83ff646d8e5d0f5b9146d98908dab32411ff3e93",   # OI pre-registration
                   "c3ef65830c3bf729354749a94c2038c072fa72f99b5977608f41aa6bd19c0b92",   # market-move definition
                   "51d94913b6011a966ffd7b881e3f47509e20dc176b1c6ba4c821dfd084d2e41d"):  # market-move plan
        assert frozen in pins
    assert {d["status"] for d in cat["datasets"]} <= set(archive.STATUS_FOLDER)


def test_write_once_refuses_to_change_an_archived_file(tmp_path):
    p = tmp_path / "a" / "f.bin"
    archive._write_once(p, b"one")
    archive._write_once(p, b"one")                      # identical rewrite is a no-op
    with pytest.raises(RuntimeError, match="immutable"):
        archive._write_once(p, b"two")
    assert p.read_bytes() == b"one" and not list(tmp_path.rglob("*.partial"))


def test_sync_plan_uploads_missing_never_overwrites_or_deletes(tmp_path):
    root = tmp_path / archive.ARCHIVE_ROOT
    (root / "10_raw").mkdir(parents=True)
    (root / "10_raw" / "a").write_bytes(b"a")
    (root / "10_raw" / "b").write_bytes(b"b")
    (root / "10_raw" / "c").write_bytes(b"c")
    archive.write_sums(root)
    sha = lambda b: hashlib.sha256(b).hexdigest()   # noqa: E731
    plan = archive.sync_plan(tmp_path, {"10_raw/a": sha(b"a"), "10_raw/b": sha(b"changed"), "10_raw/old": "x"})
    assert [u["path"] for u in plan["upload"]] == ["10_raw/c", "SHA256SUMS"]       # sums last
    assert plan["already_present"] == ["10_raw/a"]
    assert [c["path"] for c in plan["conflicts"]] == ["10_raw/b"]
    assert plan["delete"] == []                                                    # remote-only files stay


def test_network_is_refused_in_offline_mode(monkeypatch):
    import offline_queries
    for name in ("connect",):
        monkeypatch.setattr(socket.socket, name, socket.socket.connect)
    monkeypatch.setattr(socket, "create_connection", socket.create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", socket.getaddrinfo)
    offline_queries.block_network()
    with pytest.raises(RuntimeError, match="network access attempted"):
        socket.create_connection(("example.invalid", 443))
    with pytest.raises(RuntimeError, match="network access attempted"):
        socket.getaddrinfo("example.invalid", 443)


# ------------------------------------------------------------ Parquet (needs pyarrow; skipped where absent)

def test_parquet_round_trip_is_exact_for_strings_numbers_nested_and_missing(repo):
    pytest.importorskip("pyarrow")
    rows = [{"a": "86002.10", "n": 1, "x": 0.5, "nested": {"k": [1, None]}, "u": "é"},
            {"a": "1.25e-05", "n": 2, "x": 1, "u": None},          # x: int after float, nested absent
            {"a": "", "n": None, "x": None, "nested": [], "u": "日本"}]
    ds = {"id": "t", "time_field": None}
    table = archive.to_table(rows, ds, "p", "s")
    back, meta = archive.rebuild_rows(table)
    assert [archive.row_sha(r) for r in back] == [archive.row_sha(r) for r in rows]
    assert "x" in json.loads(meta["json_columns"]) and table.column("a").to_pylist()[0] == "86002.10"


def test_build_then_verify_detects_tampering(repo, tmp_path):
    pytest.importorskip("pyarrow")
    r, cat = repo
    stage = tmp_path / "stage"
    socket_connect = socket.socket.connect
    try:
        socket.socket.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("network"))
        archive.build(r, None, stage, cat)
        res = archive.verify(stage)
    finally:
        socket.socket.connect = socket_connect
    assert res["ok"], res["problems"]
    assert res["checked"]["parquet"] == 3 and res["checked"]["rows"] == 7 and res["checked"]["pinned"] == 2
    root = stage / archive.ARCHIVE_ROOT
    assert (root / "00_preregistrations" / "prereg").is_dir() and (root / "20_parquet" / "observations").is_dir()
    archive.build(r, None, stage, cat)                       # rebuilding the same snapshot changes nothing
    assert archive.verify(stage)["ok"]
    victim = next((root / "10_raw").rglob("obs.jsonl.gz"))
    data = victim.read_bytes()
    victim.write_bytes(data[:-1] + bytes([data[-1] ^ 0xFF]))      # flip the last byte
    problems = archive.verify(stage)["problems"]
    assert any(p.startswith("checksum mismatch") and "obs.jsonl.gz" in p for p in problems)
    assert any(p.startswith("unreadable") and "obs.jsonl.gz" in p for p in problems)   # reported, not a crash


def test_offline_candle_query_never_reads_the_sealed_period(tmp_path):
    pytest.importorskip("pyarrow")
    duckdb = pytest.importorskip("duckdb")
    import pyarrow.parquet as pq
    import offline_queries
    seal = offline_queries.EVAL_SEAL_MS
    rows = [{"asset": "BTC", "open_ts": str(seal - 7200000 + 3600000 * i), "available_at": str(seal - 3600000 + 3600000 * i)}
            for i in range(4)]                               # two before the seal, two at or after it
    ds = {"id": "mm_candles_1h", "time_field": "open_ts", "available_field": "available_at"}
    d = tmp_path / archive.ARCHIVE_ROOT / "20_parquet" / "candles_1h" / "dataset=mm_candles_1h" / "snapshot=x"
    d.mkdir(parents=True)
    pq.write_table(archive.to_table(rows, ds, "p", "s"), d / "c.parquet")
    con = duckdb.connect()
    n = con.execute(f"SELECT count(*) FROM read_parquet('{offline_queries.pq_glob(tmp_path / archive.ARCHIVE_ROOT, 'candles_1h', 'mm_candles_1h')}') "
                    f"WHERE _available_at_ms < {seal}").fetchone()[0]
    assert n == 1                                            # only the candle available before 2026-07-22T00:00Z
    assert f"_available_at_ms < {{EVAL_SEAL_MS}}" in (HERE / "offline_queries.py").read_text()


def test_secret_scan_flags_tokens_and_ignores_ordinary_data(tmp_path):
    (tmp_path / "ok.json").write_text(json.dumps({"funding": "1.25e-05", "url": "https://api.hyperliquid.xyz/info"}))
    (tmp_path / "bad.jsonl.gz").write_bytes(gzip.compress(b'{"h": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345"}'))
    (tmp_path / "bad2.txt").write_text("CLOUDFLARE_API_TOKEN=abcdEFGH1234ijklMNOP5678")
    found = archive.secret_findings(tmp_path)
    assert len(found) == 2 and all(f.startswith("bad") for f in found)


def test_append_only_partitions_are_stored_once_and_a_rewritten_partition_is_refused(repo, tmp_path):
    pytest.importorskip("pyarrow")
    r, cat = repo
    ds = {"id": "fwd", "status": "PROSPECTIVE_APPEND_ONLY", "format": "jsonl.gz", "table": "observations",
          "provenance": "test", "time_field": "timestamp", "mutable_paths": ["index.json"],
          "source": {"kind": "git", "ref": "data", "paths": ["obs.jsonl.gz", "index.json"]}}
    cat = {"catalog_version": "t", "datasets": [ds]}
    (r / "index.json").write_text('{"runs": 1}')
    git(r, "add", "-A"); git(r, "commit", "-q", "-m", "run 1")
    stage = tmp_path / "s"
    archive.build(r, None, stage, cat)
    (r / "index.json").write_text('{"runs": 2}')               # a later run rewrites only the index
    git(r, "add", "-A"); git(r, "commit", "-q", "-m", "run 2")
    archive.build(r, None, stage, cat)
    root = stage / archive.ARCHIVE_ROOT
    assert len(list(root.rglob("obs.jsonl.gz"))) == 1          # the partition is stored once
    assert len(list(root.rglob("index.json"))) == 2            # one index per sync snapshot
    assert archive.verify(stage)["ok"]
    (r / "obs.jsonl.gz").write_bytes(gzip.compress(b'{"timestamp": "1"}\n'))   # a past partition rewritten
    git(r, "add", "-A"); git(r, "commit", "-q", "-m", "rewrite")
    with pytest.raises(RuntimeError, match="immutable"):
        archive.build(r, None, stage, cat)
