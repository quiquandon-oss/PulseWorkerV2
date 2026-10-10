#!/usr/bin/env python3
"""Copy a verified package into a mounted Drive folder without ever overwriting or deleting, then verify it.

Used by the Colab notebook (drive_backup_colab.ipynb) on a Google Drive mount, but it works on any directory:

    python3 research/archive/drive_copy.py copy   --src PKG/CryptoPulseV2-Research-Archive --dest /content/drive/MyDrive/<folder>
    python3 research/archive/drive_copy.py verify --dest /content/drive/MyDrive/<folder>

Rules:
- every file listed in the source SHA256SUMS is copied in batches; SHA256SUMS itself goes last;
- a destination file with the same bytes is skipped; one with different bytes is a CONFLICT: the copy stops and
  nothing is overwritten;
- a destination file that is not in SHA256SUMS (e.g. a Drive duplicate "name (1).json") is reported;
- nothing is ever deleted; files are written to a temporary name and renamed only when complete.
"""
import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

REPORT_DIR = "_verification"          # reports live here and are excluded from the package comparison
BATCHES = [
    ("1 small records", ("00_preregistrations/", "30_run_records/", "90_manifests/", "README.txt")),
    ("2 code", ("05_code/",)),
    ("3 raw frozen/exports/prospective", ("10_raw/frozen/", "10_raw/exports/", "10_raw/prospective/")),
    ("4 GDELT cache", ("10_raw/session_extracts/",)),
    ("5 parquet", ("20_parquet/",)),
]


def sha256_file(p, chunk=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                return h.hexdigest()
            h.update(b)


def read_sums(path):
    out = {}
    for line in Path(path).read_text().splitlines():
        h, p = line.split("  ", 1)
        out[p] = h
    return out


def batch_of(path):
    for name, prefixes in BATCHES:
        if any(path == p or path.startswith(p) for p in prefixes):
            return name
    return "6 other"


def copy_package(src, dest, log=print):
    src, dest = Path(src), Path(dest)
    sums = read_sums(src / "SHA256SUMS")
    res = {"copied": 0, "skipped_identical": 0, "conflicts": [], "batches": {}}
    order = sorted(sums, key=lambda p: (batch_of(p), p))
    current = None
    for rel in order:
        b = batch_of(rel)
        if b != current:
            current = b
            log(f"batch {b} ...")
        s, d = src / rel, dest / rel
        stats = res["batches"].setdefault(b, {"files": 0, "bytes": 0})
        if d.exists():
            if sha256_file(d) == sums[rel]:
                res["skipped_identical"] += 1
                continue
            res["conflicts"].append(rel)
            log(f"CONFLICT (not overwritten, copy stopped): {rel}")
            return res
        d.parent.mkdir(parents=True, exist_ok=True)
        tmp = d.with_name(d.name + ".partial")
        if tmp.exists():
            res["conflicts"].append(str(tmp.relative_to(dest)))
            log(f"CONFLICT: leftover partial file {tmp}; not touched, copy stopped")
            return res
        shutil.copyfile(s, tmp)
        os.replace(tmp, d)
        res["copied"] += 1
        stats["files"] += 1
        stats["bytes"] += s.stat().st_size
    sums_dest = dest / "SHA256SUMS"                       # last, so a partial upload is recognisable
    if sums_dest.exists():
        if sha256_file(sums_dest) != sha256_file(src / "SHA256SUMS"):
            res["conflicts"].append("SHA256SUMS")
            return res
        res["skipped_identical"] += 1
    else:
        shutil.copyfile(src / "SHA256SUMS", dest / "SHA256SUMS.partial")
        os.replace(dest / "SHA256SUMS.partial", sums_dest)
        res["copied"] += 1
    return res


def verify_tree(dest, expected_sums=None):
    """Re-hash every file in `dest` against its SHA256SUMS (and, if given, the package's own SHA256SUMS)."""
    dest = Path(dest)
    res = {"ok": False, "files_checked": 0, "bytes": 0, "missing": [], "mismatched": [], "unexpected": []}
    if not (dest / "SHA256SUMS").exists():
        res["missing"].append("SHA256SUMS")
        return res
    sums = read_sums(dest / "SHA256SUMS")
    if expected_sums is not None and read_sums(expected_sums) != sums:
        res["mismatched"].append("SHA256SUMS differs from the package's")
    present = set()
    for p in dest.rglob("*"):
        if p.is_file():
            rel = p.relative_to(dest).as_posix()
            if rel == "SHA256SUMS" or rel.startswith(REPORT_DIR + "/"):
                continue
            present.add(rel)
    for rel in sorted(sums):
        if rel not in present:
            res["missing"].append(rel)
            continue
        if sha256_file(dest / rel) != sums[rel]:
            res["mismatched"].append(rel)
        res["files_checked"] += 1
        res["bytes"] += (dest / rel).stat().st_size
    res["unexpected"] = sorted(present - set(sums))
    res["ok"] = not (res["missing"] or res["mismatched"] or res["unexpected"])
    return res


def compare_remote_listing(src, listing):
    """The package against Google Drive's own server-side record of the uploaded files.

    listing = {relative path: [(md5Checksum, size), ...]} for every file under the Drive folder, as returned by
    the Drive API (more than one entry for a path means Drive holds duplicates with the same name).
    """
    src = Path(src)
    sums = read_sums(src / "SHA256SUMS")
    expected = {rel: (hashlib.md5((src / rel).read_bytes()).hexdigest(), (src / rel).stat().st_size) for rel in [*sums, "SHA256SUMS"]}
    res = {"ok": False, "files_expected": len(expected), "files_matching": 0, "bytes_matching": 0,
           "missing": [], "mismatched": [], "duplicates": [], "unexpected": []}
    for rel, (md5, size) in sorted(expected.items()):
        entries = listing.get(rel, [])
        if not entries:
            res["missing"].append(rel)
        elif len(entries) > 1:
            res["duplicates"].append(rel)
        elif (entries[0][0], int(entries[0][1])) != (md5, size):
            res["mismatched"].append(rel)
        else:
            res["files_matching"] += 1
            res["bytes_matching"] += size
    res["unexpected"] = sorted(p for p in listing if p not in expected and not p.startswith(REPORT_DIR + "/"))
    res["ok"] = res["files_matching"] == len(expected) and not (res["duplicates"] or res["unexpected"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("copy")
    c.add_argument("--src", required=True)
    c.add_argument("--dest", required=True)
    v = sub.add_parser("verify")
    v.add_argument("--dest", required=True)
    v.add_argument("--expected-sums")
    a = ap.parse_args(argv)
    res = copy_package(a.src, a.dest) if a.cmd == "copy" else verify_tree(a.dest, a.expected_sums)
    print(json.dumps(res, indent=1))
    return 0 if (res.get("ok") if a.cmd == "verify" else not res["conflicts"]) else 1


if __name__ == "__main__":
    sys.exit(main())
