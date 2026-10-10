#!/usr/bin/env python3
"""Incremental archive feed: what the Drive archive should contain, append-only.

The feed lists every archive file with its size, SHA-256, MD5, kind, dataset, snapshot and an immutable source URL
(a commit-pinned raw GitHub URL, or a content-addressed blob on the archive-feed branch). The Drive mirror
(apps_script/mirror_core.gs) uploads only the files its state lacks and verifies each against these checksums.

    # once: describe the verified baseline already in Drive
    python3 research/archive/increment.py baseline --pkg PKG --bundle-size 27061498 --out baseline.json
    # each run: extend the previous feed (never rewrites it)
    python3 research/archive/increment.py plan --repo . --previous feed.json --out OUTDIR

No network, no D1, no Drive. Output: OUTDIR/feed.json and OUTDIR/blobs/<sha256> for generated files
(dataset snapshot manifests, incremental code bundles).
"""
import argparse
import datetime as dt
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import archive  # noqa: E402

FEED_FORMAT = "cryptopulse-archive-feed-v1"
REPO_SLUG = "quiquandon-oss/PulseWorkerV2"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO_SLUG}"
FEED_BRANCH = "archive-feed"
CODE_REFS = ["claude/epic-planck-uyapsw-archive", "claude/epic-planck-uyapsw", "claude/sweet-meitner-66ntx8",
             "claude/epic-planck-uyapsw-market-moves", "research-data/market-moves", "research-data/risk-regime-forward", "main"]


class PlanConflict(RuntimeError):
    """The repository would require changing a file the archive already holds (append-only violated)."""


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def md5(b):
    return hashlib.md5(b).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


# ------------------------------------------------------------------ classification

def classify(path):
    """kind of an archive path: preregistration, raw, derived, experiment_output, code_bundle, manifest, report."""
    if path.startswith("00_preregistrations/"):
        return "preregistration"
    if path.startswith("05_code/"):
        return "code_bundle"
    if path.startswith("20_parquet/"):
        return "derived"
    if path.startswith("30_run_records/") or path.startswith("10_raw/frozen/rr_study_outputs/"):
        return "experiment_output"
    if path.startswith("10_raw/"):
        return "raw"
    if path.startswith("_verification/"):
        return "report"
    return "manifest"


def locate(path):
    """(dataset, snapshot, source_path) of a raw/preregistration/run-record archive path, else (None, None, None)."""
    parts = path.split("/")
    for i, p in enumerate(parts):
        if p.startswith("snapshot=") or p == "partitions":
            if parts[0] == "20_parquet":
                return None, None, None
            return parts[i - 1], (p[len("snapshot="):] if p != "partitions" else "partitions"), "/".join(parts[i + 1:])
    return None, None, None


def entry(path, data, origin, url, dataset=None, snapshot=None):
    return {"path": path, "kind": classify(path), "dataset": dataset, "snapshot": snapshot, "origin": origin,
            "size": len(data), "sha256": sha256(data), "md5": md5(data), "url": url}


# ------------------------------------------------------------------ baseline

def build_baseline(pkg, bundle_size, sums_size=None):
    """Feed entries for the verified baseline package already in Drive (built and checked by package.py)."""
    root = Path(pkg) / archive.ARCHIVE_ROOT if (Path(pkg) / archive.ARCHIVE_ROOT).exists() else Path(pkg)
    files = []
    for line in (root / "SHA256SUMS").read_text().splitlines():
        h, p = line.split("  ", 1)
        ds, snap, _ = locate(p)
        if p.startswith("05_code/"):           # rebuilt bundles differ in pack bytes: adopt the uploaded one as is
            files.append({"path": p, "kind": "code_bundle", "dataset": None, "snapshot": None, "origin": "baseline_adopt",
                          "size": bundle_size, "sha256": None, "md5": None})
            continue
        data = (root / p).read_bytes()
        assert sha256(data) == h, p
        e = entry(p, data, "baseline", None, ds, snap)
        del e["url"]
        files.append(e)
    files.append({"path": "SHA256SUMS", "kind": "manifest", "dataset": None, "snapshot": None, "origin": "baseline_adopt",
                  "size": sums_size or (root / "SHA256SUMS").stat().st_size, "sha256": None, "md5": None})
    return sorted(files, key=lambda f: f["path"])


def make_feed(files, seq, code_heads, note=None):
    return {"format": FEED_FORMAT, "feed_seq": seq, "generated_utc": now_iso(), "repository": REPO_SLUG,
            "code_heads": code_heads, "files": files, "files_sha256": sha256(canonical(files).encode()),
            **({"note": note} if note else {})}


def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ------------------------------------------------------------------ plan

def _signatures(files):
    """dataset -> set of snapshot signatures (frozenset of (source_path, sha256)) already archived."""
    groups = {}
    for f in files:
        ds, snap, src = locate(f["path"])
        if ds and snap != "partitions" and f.get("sha256"):
            groups.setdefault((ds, snap), set()).add((src, f["sha256"]))
    sigs = {}
    for (ds, _snap), members in groups.items():
        sigs.setdefault(ds, set()).add(frozenset(members))
    return sigs


def _records_meta(ds, path, data):
    """Schema (field names) and data cutoff of a JSONL/CSV/JSON-list file, for the snapshot manifest."""
    try:
        rows = archive.records(archive.file_format(ds, path), data)
    except Exception:
        return None
    if not rows:
        return None
    fields = sorted({k for r in rows[:200] for k in r})
    meta = {"rows": len(rows), "fields": fields}
    for key, name in ((ds.get("time_field"), "data_last_utc"), (ds.get("available_field"), "available_last_utc")):
        if key:
            ts = [t for t in (archive.to_ms(r.get(key)) for r in rows) if t is not None]
            if ts:
                meta[name] = archive.iso(max(ts))
    return meta


def plan(repo, previous, catalog=None, code_refs=None, today=None):
    """Return (feed, blobs, new_entries). `previous` is the last feed (or the baseline feed)."""
    catalog = catalog or json.loads(archive.CATALOG.read_text())
    files = list(previous["files"])
    known = {f["path"]: f for f in files}
    sigs = _signatures(files)
    blobs, new = {}, []

    def add(e):
        old = known.get(e["path"])
        if old:
            if old.get("sha256") not in (None, e["sha256"]):
                raise PlanConflict(f"archive already holds different bytes at {e['path']}")
            return
        known[e["path"]] = e
        files.append(e)
        new.append(e)

    for ds in catalog["datasets"]:
        if ds["source"]["kind"] != "git":
            continue
        src = archive.GitSource(repo, ds["source"]["ref"])
        paths = src.match(ds["source"]["paths"])
        if not paths:
            continue
        datas = {p: src.read(p) for p in paths}
        base = archive.STATUS_FOLDER[ds["status"]] + "/" + ds["id"]
        url = lambda p: f"{RAW_BASE}/{src.commit}/{p}"   # noqa: E731  commit-pinned, immutable
        if ds["status"] == "PROSPECTIVE_APPEND_ONLY":
            mutable = [p for p in paths if not archive._is_partition(ds, p)]
            for p in paths:
                if p not in mutable:
                    add(entry(f"{base}/partitions/{p}", datas[p], "git", url(p), ds["id"], "partitions"))
            paths = mutable
            if not paths:
                continue
        sig = frozenset((p, sha256(datas[p])) for p in paths)
        if sig in sigs.get(ds["id"], set()):
            continue
        content = sha256("".join(sha256(datas[p]) for p in paths).encode())
        sid = f"content-{content[:12]}" if ds.get("snapshot_by") == "content" else f"{src.commit_time[:10]}_git-{src.commit[:8]}"
        man = {"dataset_id": ds["id"], "snapshot_id": sid, "status": ds["status"], "provenance": ds.get("provenance"),
               "location": src.describe(), "tool": "research-archive-increment-v1", "files": []}
        for p in paths:
            ap = f"{base}/snapshot={sid}/{p}"
            add(entry(ap, datas[p], "git", url(p), ds["id"], sid))
            fm = {"archive_path": ap, "source_path": p, "bytes": len(datas[p]), "sha256": sha256(datas[p])}
            meta = _records_meta(ds, p, datas[p]) if ds.get("time_field") or ds.get("table") else None
            if meta:
                fm.update(meta)
            man["files"].append(fm)
        mb = (json.dumps(man, indent=1, sort_keys=True) + "\n").encode()
        blobs[sha256(mb)] = mb
        add(entry(f"90_manifests/{ds['id']}__{sid}.json", mb, "feed_blob", f"{RAW_BASE}/{FEED_BRANCH}/blobs/{sha256(mb)}"))
        sigs.setdefault(ds["id"], set()).add(sig)

    heads = previous.get("code_heads") or {}
    cur = _current_heads(repo, code_refs or CODE_REFS)
    if heads and cur != heads:
        b = incremental_bundle(repo, cur, heads)
        if b:
            day = (today or dt.date.today()).isoformat()
            name = f"05_code/increments/PulseWorkerV2-{day}-{sha256(canonical(cur).encode())[:12]}.bundle"
            blobs[sha256(b)] = b
            add(entry(name, b, "feed_blob", f"{RAW_BASE}/{FEED_BRANCH}/blobs/{sha256(b)}"))
    seq = previous.get("feed_seq", 0) + (1 if new or cur != heads else 0)
    return make_feed(files, seq, cur), blobs, new


def _current_heads(repo, refs):
    out = {}
    for name in refs:
        r = subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", f"refs/remotes/origin/{name}^{{commit}}"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            out[name] = r.stdout.strip()
    return out


def incremental_bundle(repo, heads, previous_heads):
    """A git bundle of the commits added since `previous_heads` (those are its prerequisites), or None."""
    with tempfile.TemporaryDirectory() as bare, tempfile.TemporaryDirectory() as out:
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        specs = [f"refs/remotes/origin/{n}:refs/tmp/{n}" for n in heads]
        subprocess.run(["git", "-C", bare, "fetch", "-q", str(Path(repo).resolve()), *specs], check=True)
        for n, c in heads.items():
            subprocess.run(["git", "-C", bare, "update-ref", f"refs/heads/{n}", c], check=True)
        for n in heads:
            subprocess.run(["git", "-C", bare, "update-ref", "-d", f"refs/tmp/{n}"], check=True)
        olds = [c for c in set(previous_heads.values())
                if subprocess.run(["git", "-C", bare, "cat-file", "-e", f"{c}^{{commit}}"], capture_output=True).returncode == 0]
        new = subprocess.run(["git", "-C", bare, "rev-list", "--branches", "--not", *olds], capture_output=True, text=True).stdout.split()
        if not new:
            return None
        path = Path(out) / "inc.bundle"
        subprocess.run(["git", "-C", bare, "bundle", "create", str(path), "--branches", "--not", *olds],
                       check=True, capture_output=True)
        return path.read_bytes()


def write_out(out, feed, blobs):
    out = Path(out)
    (out / "blobs").mkdir(parents=True, exist_ok=True)
    for h, b in blobs.items():
        p = out / "blobs" / h
        if p.exists() and p.read_bytes() != b:
            raise PlanConflict(f"blob {h} exists with different bytes")
        p.write_bytes(b)
    (out / "feed.json").write_text(json.dumps(feed, indent=1, sort_keys=True) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("baseline")
    b.add_argument("--pkg", required=True)
    b.add_argument("--bundle-size", type=int, required=True, help="size of the git bundle actually uploaded")
    b.add_argument("--code-heads", required=True, help="package_expected_sha256.json (its bundle_heads)")
    b.add_argument("--out", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--repo", default=".")
    p.add_argument("--previous", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "baseline":
        heads = json.loads(Path(a.code_heads).read_text())["bundle_heads"]
        feed = make_feed(build_baseline(a.pkg, a.bundle_size), 0, heads,
                         note="verified baseline uploaded 2026-10-10 (report _verification/upload_verification_2026-10-10T114950Z.json)")
        Path(a.out).write_text(json.dumps(feed, indent=1, sort_keys=True) + "\n")
        print(json.dumps({"files": len(feed["files"]), "feed_seq": 0}))
    else:
        feed, blobs, new = plan(a.repo, json.loads(Path(a.previous).read_text()))
        write_out(a.out, feed, blobs)
        print(json.dumps({"feed_seq": feed["feed_seq"], "files": len(feed["files"]), "new": len(new),
                          "new_bytes": sum(e["size"] for e in new)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
