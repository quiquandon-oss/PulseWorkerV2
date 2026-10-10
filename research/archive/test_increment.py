"""Incremental archive: append-only feed planning, code increments, restore from a known-good manifest, and the
Drive mirror end to end against a local folder (node runs the same mirror_core.gs that Apps Script runs)."""
import gzip
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import increment
import restore

HERE = Path(__file__).resolve().parent
NODE = shutil.which("node")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path):
    """Upstream repo with a data branch, cloned so its branches appear as origin/* (as in a real checkout)."""
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "data")
    git(up, "config", "user.email", "t@example.invalid")
    git(up, "config", "user.name", "t")
    (up / "frozen.json").write_text(json.dumps([{"ts": 1790000000000, "v": 1}]))
    (up / "fwd").mkdir()
    (up / "fwd" / "2026-10-08.jsonl.gz").write_bytes(gzip.compress(b'{"timestamp": "1790000000000", "x": 1}\n', mtime=0))
    (up / "fwd" / "index.json").write_text('{"runs": 1}')
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", "seed")
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(up), str(clone)], check=True)
    catalog = {"catalog_version": "t", "datasets": [
        {"id": "frz", "status": "FROZEN_SNAPSHOT", "format": "json", "table": "d1_extract", "time_field": "ts", "provenance": "t",
         "source": {"kind": "git", "ref": "origin/data", "paths": ["frozen.json"]}},
        {"id": "fwd", "status": "PROSPECTIVE_APPEND_ONLY", "format": "jsonl.gz", "table": "observations", "time_field": "timestamp",
         "provenance": "t", "mutable_paths": ["fwd/index.json"], "source": {"kind": "git", "ref": "origin/data", "paths": ["fwd/*"]}},
    ]}
    return up, clone, catalog


def commit(up, clone, changes, msg):
    for p, data in changes.items():
        (up / p).parent.mkdir(parents=True, exist_ok=True)
        (up / p).write_bytes(data)
    git(up, "add", "-A")
    git(up, "commit", "-q", "-m", msg)
    git(clone, "fetch", "-q", "origin")


def first_feed(clone, catalog):
    feed, blobs, new = increment.plan(clone, {"files": [], "feed_seq": 0, "code_heads": {}}, catalog, code_refs=["data"])
    return feed, blobs


def test_unchanged_repository_adds_nothing_and_keeps_the_feed_identical(repo):
    up, clone, cat = repo
    f1, _ = first_feed(clone, cat)
    f2, blobs, new = increment.plan(clone, f1, cat, code_refs=["data"])
    assert new == [] and blobs == {} and f2["feed_seq"] == f1["feed_seq"]
    assert f2["files"] == f1["files"] and f2["files_sha256"] == f1["files_sha256"]


def test_new_partition_and_index_are_added_without_touching_history(repo):
    up, clone, cat = repo
    f1, _ = first_feed(clone, cat)
    commit(up, clone, {"fwd/2026-10-09.jsonl.gz": gzip.compress(b'{"timestamp": "1790086400000"}\n', mtime=0),
                       "fwd/index.json": b'{"runs": 2}'}, "run 2")
    f2, blobs, new = increment.plan(clone, f1, cat, code_refs=["data"])
    paths = [e["path"] for e in new]
    assert "10_raw/prospective/fwd/partitions/fwd/2026-10-09.jsonl.gz" in paths
    assert any(p.endswith("/fwd/index.json") and "/snapshot=" in p for p in paths)            # mutable file: new snapshot
    assert f2["files"][: len(f1["files"])] == f1["files"]                                    # strictly appended
    head = git(clone, "rev-parse", "origin/data").strip()
    assert all(e["url"].startswith(f"{increment.RAW_BASE}/{head}/") for e in new if e["origin"] == "git")   # commit-pinned
    assert f2["feed_seq"] == f1["feed_seq"] + 1


def test_changed_frozen_dataset_becomes_a_new_snapshot_and_the_old_one_stays(repo):
    up, clone, cat = repo
    f1, _ = first_feed(clone, cat)
    old = [e["path"] for e in f1["files"] if e["dataset"] == "frz"]
    commit(up, clone, {"frozen.json": json.dumps([{"ts": 1790000000000, "v": 2}]).encode()}, "revise")
    f2, blobs, new = increment.plan(clone, f1, cat, code_refs=["data"])
    snaps = {e["snapshot"] for e in f2["files"] if e["dataset"] == "frz"}
    assert len(snaps) == 2 and all(p in [e["path"] for e in f2["files"]] for p in old)
    man = [e for e in new if e["path"].startswith("90_manifests/frz__")]
    assert len(man) == 1 and man[0]["origin"] == "feed_blob"
    doc = json.loads(blobs[man[0]["sha256"]])
    assert doc["files"][0]["fields"] == ["ts", "v"] and doc["files"][0]["data_last_utc"] == "2026-09-21T14:13:20Z"   # schema + cutoff


def test_a_rewritten_partition_is_a_conflict_not_an_overwrite(repo):
    up, clone, cat = repo
    f1, _ = first_feed(clone, cat)
    commit(up, clone, {"fwd/2026-10-08.jsonl.gz": gzip.compress(b'{"timestamp": "1"}\n', mtime=0)}, "rewrite history")
    with pytest.raises(increment.PlanConflict, match="different bytes"):
        increment.plan(clone, f1, cat, code_refs=["data"])


def test_code_increment_bundle_applies_on_top_of_the_previous_heads(repo, tmp_path):
    up, clone, cat = repo
    f1, _ = first_feed(clone, cat)
    old_head = git(clone, "rev-parse", "origin/data").strip()
    f1["code_heads"] = {"data": old_head}
    commit(up, clone, {"notes.txt": b"code change"}, "code")
    f2, blobs, new = increment.plan(clone, f1, cat, code_refs=["data"])
    (b,) = [e for e in new if e["kind"] == "code_bundle"]
    bundle = tmp_path / "inc.bundle"
    bundle.write_bytes(blobs[b["sha256"]])
    other = tmp_path / "restore"
    subprocess.run(["git", "clone", "-q", "--no-checkout", str(up), str(other)], check=True)
    git(other, "update-ref", "refs/heads/old", old_head)
    git(other, "fetch", "-q", str(bundle), "refs/heads/data:refs/heads/from-bundle")
    assert git(other, "rev-parse", "from-bundle").strip() == f2["code_heads"]["data"]
    f3, blobs3, new3 = increment.plan(clone, f2, cat, code_refs=["data"])
    assert new3 == []                                                                          # no new commits: no bundle


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_end_to_end_local_drive_adopt_increment_interrupt_restore(repo, tmp_path):
    """Planner (Python) -> mirror (the Apps Script code, run by node) -> restore (Python), all on local folders."""
    up, clone, cat = repo
    feed0, blobs0 = first_feed(clone, cat)
    drive = tmp_path / "drive"
    drive.mkdir()
    # the 'verified baseline': feed0's files already in Drive with a SHA256SUMS, as after the manual upload
    for e in feed0["files"]:
        src = blobs0.get(e["sha256"]) if e["origin"] == "feed_blob" else git_show(clone, e["url"])
        (drive / e["path"]).parent.mkdir(parents=True, exist_ok=True)
        (drive / e["path"]).write_bytes(src)
    (drive / "SHA256SUMS").write_text("".join(f"{e['sha256']}  {e['path']}\n" for e in feed0["files"]))
    base = [dict(e, origin="baseline") for e in feed0["files"]] + [
        {"path": "SHA256SUMS", "kind": "manifest", "dataset": None, "snapshot": None, "origin": "baseline_adopt",
         "size": (drive / "SHA256SUMS").stat().st_size, "sha256": None, "md5": None}]
    for e in base:
        e.pop("url", None)
    bfeed = increment.make_feed(sorted(base, key=lambda e: e["path"]), 0, {"data": git(clone, "rev-parse", "origin/data").strip()})
    out0 = tmp_path / "f0"
    increment.write_out(out0, bfeed, {})
    assert mirror(drive, out0, clone)["status"] == "ADOPTED"
    assert mirror(drive, out0, clone)["status"] == "NOOP"

    commit(up, clone, {"fwd/2026-10-09.jsonl.gz": gzip.compress(b'{"timestamp": "2"}\n', mtime=0),
                       "fwd/2026-10-10.jsonl.gz": gzip.compress(b'{"timestamp": "3"}\n', mtime=0), "fwd/index.json": b'{"runs": 3}'}, "runs")
    feed1, blobs1, new1 = increment.plan(clone, bfeed, cat, code_refs=["data"])
    out1 = tmp_path / "f1"
    increment.write_out(out1, feed1, blobs1)
    r = mirror(drive, out1, clone, extra=["--budget-ms", "25", "--tick-ms", "20"])               # stops part-way
    assert r["status"] == "PARTIAL" and not (drive / "90_manifests/archive_state/manifest-000001.json").exists()
    r = mirror(drive, out1, clone)
    assert r["status"] == "OK" and r["manifest"]["seq"] == 1 and len(r["uploaded"]) + len(r["adopted_existing"]) == len(new1)
    assert mirror(drive, out1, clone)["status"] == "NOOP"

    r0 = restore.restore(drive, tmp_path / "r0", upto=0)                                        # known-good older state
    r1 = restore.restore(drive, tmp_path / "r1")
    assert r0["ok"] and r1["ok"] and r1["files"] == r0["files"] + len(new1)
    assert not (tmp_path / "r0/10_raw/prospective/fwd/partitions/fwd/2026-10-09.jsonl.gz").exists()

    victim = drive / new1[0]["path"]
    victim.write_bytes(b"corrupted")
    assert not restore.restore(drive, tmp_path / "r2")["ok"]
    r = mirror(drive, out1, clone, check=False)
    assert r["status"] == "FAIL" and r["problems"][0]["code"] == "MISMATCH"


def git_show(clone, url):
    commit_, path = url[len(increment.RAW_BASE) + 1:].split("/", 1)
    return subprocess.run(["git", "-C", str(clone), "show", f"{commit_}:{path}"], check=True, capture_output=True).stdout


def mirror(drive, feed_dir, clone, extra=(), check=True):
    p = subprocess.run([NODE, str(HERE / "apps_script" / "local_run.mjs"), "--drive", str(drive), "--feed", str(feed_dir / "feed.json"),
                        "--repo", str(clone), "--blobs", str(feed_dir / "blobs"), *extra], capture_output=True, text=True)
    return json.loads(p.stdout)


def test_restore_refuses_a_broken_manifest_chain(tmp_path):
    d = tmp_path / "a" / restore.STATE_DIR
    d.mkdir(parents=True)
    st = {"x": {"path": "x", "sha256": hashlib.sha256(b"x").hexdigest(), "size": 1}}
    m0 = {"format": "cryptopulse-archive-state-v1", "seq": 0, "parent": None, "added": list(st.values()), "state_sha256": restore.state_digest(st)}
    (d / "manifest-000000.json").write_text(json.dumps(m0))
    m1 = dict(m0, seq=1, parent={"name": "manifest-000000.json", "sha256": "0" * 64}, added=[])
    (d / "manifest-000001.json").write_text(json.dumps(m1))
    with pytest.raises(SystemExit, match="chain broken"):
        restore.replay(tmp_path / "a")
    assert restore.replay(tmp_path / "a", upto=0)[1] == 0                                       # the older state is still usable


def test_committed_baseline_feed_matches_the_verified_upload():
    f = json.loads((HERE / "baseline" / "feed-000000.json").read_text())
    assert len(f["files"]) == 4676 and sum(e["size"] for e in f["files"]) == 396_985_967          # the Drive report's totals
    assert f["files_sha256"] == hashlib.sha256(increment.canonical(f["files"]).encode()).hexdigest()
    assert {e["origin"] for e in f["files"]} == {"baseline", "baseline_adopt"}
    adopt = sorted(e["path"] for e in f["files"] if e["origin"] == "baseline_adopt")
    assert adopt == ["05_code/PulseWorkerV2-research.bundle", "SHA256SUMS"]


def test_single_paste_file_is_exactly_the_tested_sources():
    import importlib.util
    spec = importlib.util.spec_from_file_location("bsf", HERE / "apps_script" / "build_single_file.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert (HERE / "apps_script" / "CryptoPulseV2_archive_sync.gs").read_text() == m.build()
    assert all(ord(c) < 128 for c in m.build())                                    # plain ASCII: safe to copy on a phone


def test_staged_increment_feed_extends_the_baseline_by_exactly_its_new_files():
    base = json.loads((HERE / "baseline" / "feed-000000.json").read_text())
    stage = json.loads((HERE / "feed_stage" / "feed.json").read_text())
    assert stage["files"][: len(base["files"])] == base["files"]
    assert stage["files_sha256"] == hashlib.sha256(increment.canonical(stage["files"]).encode()).hexdigest()
    for e in stage["files"][len(base["files"]):]:
        blob = (HERE / "feed_stage" / "blobs" / e["sha256"]).read_bytes()
        assert hashlib.sha256(blob).hexdigest() == e["sha256"] and len(blob) == e["size"] and e["url"].endswith(e["sha256"])
