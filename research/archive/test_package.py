"""Manual Drive package: verified GDELT re-download, conflicts never overwritten, reviewed credential allowlist."""
import hashlib
import json
import urllib.error

import pytest

import archive
import package


def fake_expected(monkeypatch, files):
    exp = {n: {"bytes": len(b), "md5": hashlib.md5(b).hexdigest(), "sha256": hashlib.sha256(b).hexdigest()} for n, b in files.items()}
    monkeypatch.setattr(package, "expected_gdelt", lambda: exp)
    monkeypatch.setattr(package.time, "sleep", lambda s: None)
    return exp


def test_fetch_downloads_only_verified_bytes_and_is_idempotent(tmp_path, monkeypatch):
    good = {"20260819000000.export.CSV.zip": b"zip-a", "20260819001500.export.CSV.zip": b"zip-b",
            "_geo_grid_x.json": b"{}"}                                   # derived file: never downloaded
    fake_expected(monkeypatch, good)
    served = {package.GDELT_BASE + n: b for n, b in good.items()}
    calls = []
    opener = lambda url: (calls.append(url), served[url])[1]            # noqa: E731
    res = package.fetch_gdelt(tmp_path, opener=opener)
    assert (res["downloaded"], res["complete"], len(calls)) == (2, True, 2)
    assert (tmp_path / "20260819000000.export.CSV.zip").read_bytes() == b"zip-a"
    res = package.fetch_gdelt(tmp_path, opener=opener)                  # second run: nothing fetched
    assert (res["present_ok"], res["downloaded"], len(calls)) == (2, 0, 2)


def test_fetch_rejects_changed_bytes_and_never_overwrites_a_conflict(tmp_path, monkeypatch):
    fake_expected(monkeypatch, {"a.export.CSV.zip": b"right", "b.export.CSV.zip": b"right-b", "c.export.CSV.zip": b"c"})
    (tmp_path / "a.export.CSV.zip").write_bytes(b"local edit")
    def opener(url):
        if url.endswith("c.export.CSV.zip"):
            raise urllib.error.URLError("offline")
        return b"wrong"                                                  # provider now serves different bytes
    res = package.fetch_gdelt(tmp_path, opener=opener, retries=2)
    assert res["conflicts"] == ["a.export.CSV.zip"] and (tmp_path / "a.export.CSV.zip").read_bytes() == b"local edit"
    assert sorted(f["name"] for f in res["failed"]) == ["b.export.CSV.zip", "c.export.CSV.zip"]
    assert not (tmp_path / "b.export.CSV.zip").exists() and not list(tmp_path.glob("*.partial"))
    assert res["complete"] is False


def test_assemble_refuses_a_missing_or_changed_gdelt_file(tmp_path, monkeypatch):
    fake_expected(monkeypatch, {"a.export.CSV.zip": b"right"})
    (tmp_path / "g").mkdir()
    (tmp_path / "g" / "a.export.CSV.zip").write_bytes(b"changed")
    with pytest.raises(SystemExit, match="missing or different"):
        package.assemble(tmp_path, tmp_path / "g", tmp_path / "out", bundle=False)


def test_reviewed_allowlist_only_clears_the_exact_reviewed_strings(tmp_path):
    reviewed = b'"socialimage": "https://x.example/i.jpg?api_key=ksat_9a37fc89631abedf"'
    (tmp_path / "doc.json").write_bytes(reviewed)
    assert archive.secret_findings(tmp_path, allowed=set()) != []
    assert archive.secret_findings(tmp_path, allowed={b"api_key=ksat_9a37fc89631abedf"}) == []
    (tmp_path / "new.json").write_bytes(b"api_key=ksat_9a37fc89631abedff")   # one character more: not reviewed
    assert archive.secret_findings(tmp_path, allowed={b"api_key=ksat_9a37fc89631abedf"}) == ["new.json: matches " + repr(archive.SECRET_PATTERNS[-1].pattern[:40])]


def test_committed_allowlist_entries_are_all_reviewed_public_image_parameters():
    doc = json.loads(archive.ALLOWLIST.read_text())
    assert doc["reviewed"] and all(e["match"].startswith("api_key=") and e["reason"] and e["reviewed_utc"] for e in doc["reviewed"])


def test_gdelt_records_are_consistent():
    exp = package.expected_gdelt()
    zips = [n for n in exp if n.endswith(".export.CSV.zip")]
    assert len(zips) == 4435 and all(len(m["sha256"]) == 64 and len(m["md5"]) == 32 for m in exp.values())
    idx = json.loads((package.GDELT_DIR / "gdelt_master_index.json").read_text())
    listed = {ln.split()[2].rsplit("/", 1)[-1]: (int(ln.split()[0]), ln.split()[1]) for ln in idx["lines"]}
    assert all(listed[n] == (exp[n]["bytes"], exp[n]["md5"]) for n in zips)      # every cached zip is provider-listed
    tail = (package.GDELT_DIR / "masterfilelist_tail.txt.gz").read_bytes()
    import gzip
    assert hashlib.sha256(gzip.decompress(tail)).hexdigest() == idx["fetched"]["sha256"]
    ra = json.loads((package.GDELT_DIR / "retrieved_at.json").read_text())
    assert ra["gdelt"]["retrieved_at_ms"] == 1791464943118
