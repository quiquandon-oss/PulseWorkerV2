"""GDELT offline mode: archived master-list index, no network, explicit missing/corrupt handling, determinism."""
import gzip
import hashlib
import json
import sys
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

import gdelt_geo_shock as g
import gdelt_research_run as run
import risk_regime_reconstruction as rr
from test_gdelt_geo_shock import E, UTC, _fake_gdelt, row, zip_of

sys.path.insert(0, str(Path(__file__).resolve().parent / "archive"))
import gdelt_index  # noqa: E402

B = E.replace(minute=0, second=0, microsecond=0)


@pytest.fixture(autouse=True)
def online_after_each_test():
    yield
    run.use_offline_index(None)


def no_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network used")
    monkeypatch.setattr(urllib.request, "urlopen", boom)


def write_index(tmp_path, files, fetched=None):
    """files = {batch datetime: bytes}; returns the archived index path built from a provider-style list."""
    lines = [f"{len(d)} {g.md5_hex(d)} {g.export_url(b)}" for b, d in sorted(files.items())]
    lines += [f"7 {'0' * 32} {g.export_url(b).replace('.export.', '.mentions.')}" for b in files]   # not export: dropped
    doc = run.archive_master_index("\n".join(lines), min(files), fetched or {"sha256": "x"})
    p = tmp_path / "index.json"
    p.write_text(json.dumps(doc, sort_keys=True))
    return p, doc


def test_archived_index_keeps_only_export_lines_in_range_verbatim_and_sorted(tmp_path):
    data = {B + timedelta(minutes=15 * i): zip_of([row(eid=i)]) for i in (2, 0, 1)}
    _, doc = write_index(tmp_path, data)
    assert doc["format"] == run.INDEX_FORMAT and len(doc["lines"]) == 3
    assert [ln.split()[2] for ln in doc["lines"]] == sorted(g.export_url(b) for b in data)
    assert doc["first_batch"] == g.stamp(B) and doc["last_batch"] == g.stamp(B + timedelta(minutes=30))
    idx, _ = run.load_offline_index(tmp_path / "index.json")
    assert idx == {g.export_url(b): (len(d), g.md5_hex(d)) for b, d in data.items()}


def test_wrong_format_or_unparseable_index_is_refused(tmp_path):
    (tmp_path / "x.json").write_text(json.dumps({"format": "other", "lines": []}))
    with pytest.raises(run.OfflineError, match="not a"):
        run.load_offline_index(tmp_path / "x.json")
    (tmp_path / "y.json").write_text(json.dumps({"format": run.INDEX_FORMAT, "lines": ["12 abc not-a-url"]}))
    with pytest.raises(run.OfflineError, match="unparseable"):
        run.load_offline_index(tmp_path / "y.json")


def test_offline_fetch_reads_cache_and_reports_missing_corrupt_and_outside(tmp_path, monkeypatch):
    no_network(monkeypatch)
    b0, b1, b2 = B, B + timedelta(minutes=15), B + timedelta(minutes=30)
    data = {b: zip_of([row(eid=i)], name=f"{g.stamp(b)}.export.CSV") for i, b in enumerate((b0, b1, b2))}
    p, _ = write_index(tmp_path, data)
    cache = tmp_path / "c"
    cache.mkdir()
    (cache / g.export_url(b0).rsplit("/", 1)[-1]).write_bytes(data[b0])
    corrupt = cache / g.export_url(b2).rsplit("/", 1)[-1]
    corrupt.write_bytes(b"corrupt")
    run.use_offline_index(p)
    idx = run.master_index(b0)
    assert run.fetch_batch(b0, idx, cache)[0] == "CACHED"
    assert run.fetch_batch(b1, idx, cache)[0] == "MISSING_OFFLINE"
    assert run.fetch_batch(b2, idx, cache)[0] == "CORRUPT_OFFLINE"
    assert corrupt.read_bytes() == b"corrupt"                  # evidence kept: offline never deletes a cache file
    assert run.fetch_batch(b2 + timedelta(minutes=15), idx, cache)[0] == "OUTSIDE_INDEX"   # after covered_until
    assert run.fetch_batch(b0 - timedelta(minutes=15), idx, cache)[0] == "OUTSIDE_INDEX"   # before the first line
    with pytest.raises(run.OfflineError, match="refusing network"):
        run.http_get("http://data.gdeltproject.org/gdeltv2/masterfilelist.txt")
    with pytest.raises(run.OfflineError, match="starts"):
        run.master_index(b0 - timedelta(minutes=15))          # index does not reach back far enough
    _, _, log = run.build_series([b0, b1, b2], idx, cache, b2)
    assert [f["status"] for f in run.offline_problems(log)] == ["MISSING_OFFLINE", "CORRUPT_OFFLINE"]


def test_online_behaviour_is_unchanged_when_no_index_is_given(tmp_path, monkeypatch):
    data = zip_of([row(eid=1)])
    idx = {g.export_url(B): (len(data), g.md5_hex(data))}
    calls = []
    monkeypatch.setattr(run, "http_get", lambda u, *a, **k: (calls.append(u), (200, data))[1])
    run.use_offline_index(None)
    assert run.fetch_batch(B, idx, tmp_path)[0] == "FETCHED" and len(calls) == 1


def test_runner_offline_reproduces_the_online_result_without_network(tmp_path, monkeypatch):
    start, end = B - timedelta(days=5), B + timedelta(hours=14)
    real_http_get = run.http_get
    _fake_gdelt(monkeypatch, start, end, spike_from=B - timedelta(hours=2))
    ms = lambda d: int(d.timestamp() * 1000)   # noqa: E731
    v1 = [{"ts": ms(E + timedelta(hours=h)), "g": 10, "m": 30, "o": 60, "y": 9, "n": 62, "s": 62, "u": 48, "score": 55} for h in (-30, -12, -6, 1, 5)]
    (tmp_path / "v1.json").write_text(json.dumps(v1))
    cache = tmp_path / "c"
    assert run.main(["--v1", str(tmp_path / "v1.json"), "--cache", str(cache), "--out", str(tmp_path / "online.json")]) == 0
    online = json.loads((tmp_path / "online.json").read_text())
    status, master = run.http_get(g.GDELT_SOURCE["master_file_list"])            # the fake provider's list
    fetched = {"sha256": hashlib.sha256(master).hexdigest(), "bytes": len(master), "http_status": status,
               "retrieved_at": "2026-10-01T00:00:00Z"}                                # after every requested batch
    (tmp_path / "index.json").write_text(json.dumps(gdelt_index.build_index(master, fetched, cache)))
    monkeypatch.setattr(run, "http_get", real_http_get)                            # fake provider gone
    no_network(monkeypatch)
    off_args = ["--v1", str(tmp_path / "v1.json"), "--cache", str(cache), "--offline-index", str(tmp_path / "index.json")]
    o1, o2 = tmp_path / "r1" / "off.json", tmp_path / "r2" / "off.json"
    rc = run.main(off_args + ["--out", str(o1)])
    assert rc == 0, json.loads(o1.read_text()).get("error")
    assert run.main(off_args + ["--out", str(o2)]) == 0
    off = json.loads(o1.read_text())
    expected = {("CACHED" if k == "FETCHED" else k): v for k, v in online["fetch"]["fetch_status_counts"].items()}
    assert off["status"] == "OK" and off["fetch"]["fetch_status_counts"] == expected      # same batches, same statuses
    assert expected.get("NOT_LISTED")                                                     # unpublished batches stay NOT_LISTED
    for k in ("event15", "false_positives", "v1_coverage", "required_range"):
        assert off[k] == online[k]
    assert sorted(p.name for p in o1.parent.iterdir()) == sorted(p.name for p in o2.parent.iterdir())
    for p in o1.parent.iterdir():                                                   # artifact and sidecars
        assert p.read_bytes() == (o2.parent / p.name).read_bytes()                  # deterministic
    victim = sorted(cache.glob("*.export.CSV.zip"))[3]
    victim.write_bytes(b"x")                                                         # corrupt one archived file
    assert run.main(off_args + ["--out", str(tmp_path / "bad.json")]) == 2
    bad = json.loads((tmp_path / "bad.json").read_text())
    assert bad["status"] == "OFFLINE_INPUT_FAILED" and "CORRUPT_OFFLINE" in bad["error"] and "event15" not in bad
    assert victim.read_bytes() == b"x"


def test_gdelt_index_check_reports_every_mismatch(tmp_path):
    data = {B + timedelta(minutes=15 * i): zip_of([row(eid=i)], name=f"{g.stamp(B + timedelta(minutes=15 * i))}.export.CSV") for i in range(4)}
    p, _ = write_index(tmp_path, data)
    cache = tmp_path / "c"
    cache.mkdir()
    names = [g.export_url(b).rsplit("/", 1)[-1] for b in sorted(data)]
    (cache / names[0]).write_bytes(data[sorted(data)[0]])
    (cache / names[1]).write_bytes(b"short")
    d2 = bytearray(data[sorted(data)[2]])
    d2[-1] ^= 0xFF
    (cache / names[2]).write_bytes(bytes(d2))
    (cache / "20991231234500.export.CSV.zip").write_bytes(b"?")
    res = gdelt_index.check_cache(p, cache)
    assert (res["ok"], res["size_mismatch"], res["md5_mismatch"]) == (1, [names[1]], [names[2]])
    assert res["not_listed"] == ["20991231234500.export.CSV.zip"] and res["listed_not_cached"] == [names[3]]
    assert res["complete"] is False


def test_recorded_retrieval_time_overrides_file_mtimes(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    assert rr._mtime(tmp_path) != 1791464943118
    (tmp_path / rr.RETRIEVED_AT_FILE).write_text(json.dumps({"retrieved_at_ms": 1791464943118}))
    assert rr._mtime(tmp_path) == 1791464943118


def test_gzip_output_is_byte_identical_for_identical_content(tmp_path):
    for n in ("a.gz", "b.gz"):
        with rr._gz_text(tmp_path / n) as f:
            f.write("same\n")
    assert gzip.decompress((tmp_path / "a.gz").read_bytes()) == b"same\n"
    hdr = lambda n: (tmp_path / n).read_bytes()[4:8]   # noqa: E731  gzip MTIME field
    assert hdr("a.gz") == hdr("b.gz") == b"\x00\x00\x00\x00"


def test_reconstruction_refuses_offline_with_live(tmp_path):
    args = []
    for a in ("--v1", "--predictions", "--events", "--btc", "--hl-cache", "--hl-funding-cache", "--gdelt-cache", "--out-dir"):
        args += [a, str(tmp_path)]
    with pytest.raises(SystemExit):
        rr.main(args + ["--live", "--gdelt-index", str(tmp_path / "i.json")])
