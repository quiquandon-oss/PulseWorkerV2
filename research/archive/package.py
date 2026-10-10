#!/usr/bin/env python3
"""Build, verify and check the manual Google Drive archive package. Never contacts Google Drive or D1.

    # 1. (network: data.gdeltproject.org only) download the GDELT exports the studies used, byte-verified
    python3 research/archive/package.py fetch-gdelt --out WORK/gdelt
    # 2. (offline) assemble the package from git + the verified GDELT files
    python3 research/archive/package.py assemble --repo . --gdelt WORK/gdelt --out PKG
    # 3. (offline) verify a package, e.g. after downloading it back from Drive
    python3 research/archive/package.py verify --pkg PKG
    python3 research/archive/package.py summary --pkg PKG
    # 4. (offline, sockets disabled) re-run research from the package and compare with committed results
    python3 research/archive/package.py offline-check --pkg PKG --repo .
"""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import archive  # noqa: E402

GDELT_DIR = HERE / "gdelt"
GDELT_BASE = "http://data.gdeltproject.org/gdeltv2/"
LOCK = HERE / "package_lock.json"
CACHE_DATASET = "gdelt_raw_cache_session"
BUNDLE_REFS = ["claude/epic-planck-uyapsw-archive", "claude/epic-planck-uyapsw", "claude/sweet-meitner-66ntx8",
               "claude/epic-planck-uyapsw-market-moves", "research-data/market-moves", "research-data/risk-regime-forward", "main"]


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def expected_gdelt():
    return json.loads((GDELT_DIR / "cache_sha256.json").read_text())["files"]


# ------------------------------------------------------------------ 1. fetch

def fetch_gdelt(out, spacing=0.25, retries=4, opener=None):
    """Download every archived export zip that is not yet in `out`; each must match size, MD5 and SHA-256.

    A file already present with different bytes is a conflict: reported, never overwritten.
    """
    opener = opener or (lambda url: urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "CryptoPulse-research-archive/1.0"}), timeout=120).read())
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    want = {n: m for n, m in expected_gdelt().items() if n.endswith(".export.CSV.zip")}
    res = {"expected": len(want), "present_ok": 0, "downloaded": 0, "conflicts": [], "failed": []}
    for name, meta in sorted(want.items()):
        dest = out / name
        if dest.exists():
            if sha256(dest.read_bytes()) == meta["sha256"]:
                res["present_ok"] += 1
            else:
                res["conflicts"].append(name)
            continue
        data, err = None, None
        for attempt in range(retries):
            try:
                data = opener(GDELT_BASE + name)
                break
            except (urllib.error.URLError, OSError) as e:
                err = str(e)
                time.sleep(2 ** attempt)
        if data is None or len(data) != meta["bytes"] or hashlib.md5(data).hexdigest() != meta["md5"] or sha256(data) != meta["sha256"]:
            res["failed"].append({"name": name, "error": err or "bytes differ from the archived checksums"})
            continue
        tmp = dest.with_suffix(".partial")
        tmp.write_bytes(data)
        os.replace(tmp, dest)
        res["downloaded"] += 1
        time.sleep(spacing)
    res["complete"] = res["present_ok"] + res["downloaded"] == res["expected"] and not res["conflicts"]
    return res


# ------------------------------------------------------------------ 2. assemble

def _locked_catalog(repo):
    cat = json.loads(archive.CATALOG.read_text())
    lock = json.loads(LOCK.read_text())["refs"] if LOCK.exists() else {}
    for d in cat["datasets"]:
        ref = d["source"].get("ref")
        if ref in lock:
            d["source"]["ref"] = lock[ref]
    return cat


def assemble(repo, gdelt, out, bundle=True):
    """Stage the package: archive.build over git + a scratch tree holding the GDELT cache exactly as used."""
    gdelt = Path(gdelt)
    exp = expected_gdelt()
    with tempfile.TemporaryDirectory() as tmp:
        cache = Path(tmp) / "cache" / "gdelt"
        cache.mkdir(parents=True)
        for name, meta in exp.items():
            if not name.endswith(".export.CSV.zip"):
                continue
            src = gdelt / name
            if not src.exists() or sha256(src.read_bytes()) != meta["sha256"]:
                raise SystemExit(f"GDELT file missing or different: {name} (run fetch-gdelt first)")
            os.symlink(src.resolve(), cache / name)
        with tarfile.open(GDELT_DIR / "gdelt_doc_api_and_grid.tar.gz") as t:   # DOC API responses + derived grid
            t.extractall(cache, filter="data")
        ra = json.loads((GDELT_DIR / "retrieved_at.json").read_text())
        (cache / ".retrieved_at.json").write_text(json.dumps({"retrieved_at_ms": ra["gdelt"]["retrieved_at_ms"]}) + "\n")
        for name, meta in exp.items():
            if not name.endswith(".zip") and sha256((cache / name).read_bytes()) != meta["sha256"]:
                raise SystemExit(f"derived GDELT file differs from its record: {name}")
        res = archive.build(repo, tmp, out, _locked_catalog(repo))
    root = Path(out) / archive.ARCHIVE_ROOT
    if bundle:
        b = root / "05_code" / "PulseWorkerV2-research.bundle"
        if not b.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            write_bundle(repo, b)
    (root / "README.txt").write_text(readme(root))
    archive.write_sums(root)
    res["summary"] = summary(out)
    return res


def write_bundle(repo, dest):
    """git bundle whose refs are branch heads (refs/heads/<name>), so `git clone <bundle>` restores them.
    Built in a temporary bare repository; the source repository is only read."""
    with tempfile.TemporaryDirectory() as bare:
        subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        specs = []
        for name in BUNDLE_REFS:
            for ref in (f"refs/remotes/origin/{name}", f"refs/heads/{name}"):
                if subprocess.run(["git", "-C", str(repo), "rev-parse", "-q", "--verify", ref], capture_output=True).returncode == 0:
                    specs.append(f"{ref}:refs/heads/{name}")
                    break
        subprocess.run(["git", "-C", bare, "fetch", "-q", str(Path(repo).resolve()), *specs], check=True)
        subprocess.run(["git", "-C", bare, "symbolic-ref", "HEAD", "refs/heads/claude/epic-planck-uyapsw-archive"], check=True)
        subprocess.run(["git", "-C", bare, "bundle", "create", str(Path(dest).resolve()), "--all"], check=True, capture_output=True)


# ------------------------------------------------------------------ 3. verify / summary

def _root(pkg):
    p = Path(pkg)
    return p / archive.ARCHIVE_ROOT if (p / archive.ARCHIVE_ROOT).exists() else p


def verify(pkg):
    """archive.verify (checksums, pins, Parquet round trips, credential scan) plus the GDELT cache record."""
    root = _root(pkg)
    res = archive.verify(root)
    exp = expected_gdelt()
    found = {}
    for p in root.glob(f"10_raw/*/{CACHE_DATASET}/snapshot=*/cache/gdelt/**/*"):
        if p.is_file() and p.parent.name == "gdelt" and p.name in exp:
            found[p.name] = sha256(p.read_bytes())
    missing = sorted(set(exp) - set(found))
    differ = sorted(n for n, h in found.items() if h != exp[n]["sha256"])
    if missing:
        res["problems"].append(f"GDELT cache: {len(missing)} files missing, first {missing[0]}")
    if differ:
        res["problems"].append(f"GDELT cache: {len(differ)} files differ from cache_sha256.json, first {differ[0]}")
    res["checked"]["gdelt_files"] = len(found)
    res["ok"] = not res["problems"]
    return res


def summary(pkg):
    root = _root(pkg)
    out = {"files": 0, "bytes": 0, "datasets": {}, "batches": {}}
    for line in (root / "SHA256SUMS").read_text().splitlines() if (root / "SHA256SUMS").exists() else []:
        out["files"] += 1
        out["bytes"] += (root / line.split("  ", 1)[1]).stat().st_size
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).parts
        batch = "/".join(rel[:3]) if rel[0] == "10_raw" and len(rel) > 3 else "/".join(rel[:2]) if len(rel) > 2 else rel[0]
        b = out["batches"].setdefault(batch, {"files": 0, "bytes": 0})
        b["files"] += 1
        b["bytes"] += p.stat().st_size
    for m in sorted((root / "90_manifests").glob("*__*.json")) if (root / "90_manifests").exists() else []:
        man = json.loads(m.read_text())
        out["datasets"][man["dataset_id"]] = {"snapshot": man["snapshot_id"], "status": man["status"],
                                              "files": len(man["files"]), "bytes": sum(f["bytes"] for f in man["files"]),
                                              "rows": sum(q["rows"] for q in man["parquet"]) or None}
    return out


def readme(root):
    s = summary(root.parent)
    lines = [
        "CryptoPulseV2 research archive (manual Google Drive copy)",
        "",
        "Built by research/archive/package.py from git (quiquandon-oss/PulseWorkerV2) and the GDELT exports the studies used.",
        "Every file is listed in SHA256SUMS. Nothing here is a credential; nothing here is written back to production.",
        "",
        "Layout",
        "  00_preregistrations/  frozen rules (T-A1..T-A3 registration, addendum 1, erratum 1; OI pre-registration; market-move plan)",
        "  05_code/              git bundle of the research branches (restore with: git clone PulseWorkerV2-research.bundle)",
        "  10_raw/frozen|prospective|exports|session_extracts/<dataset>/snapshot=<id>/  byte-identical source files",
        "  10_raw/frozen/gdelt_master_index/  archived GDELT master-list subset, fetched tail, cache checksums, retrieval times",
        "  20_parquet/<table>/dataset=<id>/...  query copies (rebuild their source rows exactly)",
        "  30_run_records/       collection and progress run records",
        "  90_manifests/         one manifest per dataset snapshot + catalog.json",
        "  SHA256SUMS            checksum of every other file (upload last, check first)",
        "",
        "Verify (any machine):   cd CryptoPulseV2-Research-Archive && shasum -a 256 -c SHA256SUMS   (Linux: sha256sum -c)",
        "Full verify + offline research:  see RESTORE in research/archive/RESEARCH_ARCHIVE.md (package.py verify / offline-check)",
        "",
        "Rules: never overwrite or delete a file in the Drive copy; a name or checksum conflict is reported for review.",
        "",
        "Datasets",
    ]
    for k, v in sorted(s["datasets"].items()):
        lines.append(f"  {k:30s} {v['status']:24s} files {v['files']:>5}  bytes {v['bytes']:>12,}  snapshot {v['snapshot']}")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ 4. offline check

def block_network():
    def refuse(*_a, **_k):
        raise RuntimeError("network access attempted during an offline check")
    socket.socket.connect = refuse
    socket.create_connection = refuse
    socket.getaddrinfo = refuse


def _index(root):
    return next(root.glob("10_raw/frozen/gdelt_master_index/snapshot=*/research/archive/gdelt/gdelt_master_index.json"))


def offline_check(pkg, repo):
    """With sockets disabled: verify, run the DuckDB analyses, re-run the GDELT study from the package and
    compare it with the committed artifact in the package."""
    block_network()
    root = _root(pkg)
    out = {"verify": verify(root)}
    import offline_queries
    q = offline_queries.run(root.parent, repo)
    out["event_replay"] = {k: len(v) if isinstance(v, list) else v for k, v in q["event_replay"].items()}
    import gdelt_research_run as gr
    cache = next(root.glob(f"10_raw/*/{CACHE_DATASET}/snapshot=*/cache/gdelt"))
    v1 = next(root.glob("10_raw/*/d1_extracts_2026_10_08/snapshot=*/research/archive/session_extracts/inputs/v1_full.json"))
    committed = next(root.glob("10_raw/*/rr_study_outputs/snapshot=*/research/results/gdelt_risk_regime_shock.json"))
    with tempfile.TemporaryDirectory() as tmp:
        art = Path(tmp) / "gdelt_risk_regime_shock.json"
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):          # the study prints its status line
            rc = gr.main(["--v1", str(v1), "--cache", str(cache), "--scope", "history", "--max-files", "5000",
                          "--offline-index", str(_index(root)), "--out", str(art)])
        a, b = json.loads(art.read_text()), json.loads(committed.read_text())
        gr.use_offline_index(None)
    skip = {"previous_runs", "offline_index"}
    diff = sorted(k for k in set(a) | set(b) if k not in skip and a.get(k) != b.get(k))
    out["gdelt_study"] = {"exit": rc, "status": a["status"], "fields_compared": len(set(b) - skip), "fields_differing": diff}
    out["ok"] = out["verify"]["ok"] and a["status"] == "OK" and not diff and not out["event_replay"]["not_reproduced"]
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("fetch-gdelt")
    p.add_argument("--out", required=True)
    p = sub.add_parser("assemble")
    p.add_argument("--repo", default=".")
    p.add_argument("--gdelt", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--no-bundle", action="store_true")
    for name in ("verify", "summary"):
        sub.add_parser(name).add_argument("--pkg", required=True)
    p = sub.add_parser("offline-check")
    p.add_argument("--pkg", required=True)
    p.add_argument("--repo", default=".")
    a = ap.parse_args(argv)
    if a.cmd == "fetch-gdelt":
        res = fetch_gdelt(a.out)
    elif a.cmd == "assemble":
        res = assemble(a.repo, a.gdelt, a.out, bundle=not a.no_bundle)
    elif a.cmd == "verify":
        res = verify(a.pkg)
    elif a.cmd == "summary":
        res = summary(a.pkg)
    else:
        res = offline_check(a.pkg, a.repo)
    print(json.dumps(res, indent=1, default=str))
    return 0 if res.get("ok", res.get("complete", True)) else 1


if __name__ == "__main__":
    sys.exit(main())
