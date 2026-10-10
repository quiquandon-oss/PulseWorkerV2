#!/usr/bin/env python3
"""Restore the archive as of a known-good state manifest, from a downloaded copy of the Drive folder.

    python3 research/archive/restore.py --archive DOWNLOADED/CryptoPulseV2_Research_Archive --seq 3 --out RESTORED

Replays 90_manifests/archive_state/manifest-000000.json .. manifest-<seq>.json, checks the hash chain and the
state digest, then copies every file of that state into OUT after checking its SHA-256. Files added after <seq>
are ignored. Nothing in the archive copy is modified. Offline; no credentials.
"""
import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

STATE_DIR = "90_manifests/archive_state"


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def state_digest(state):
    rows = [[p, state[p]["sha256"]] for p in sorted(state)]
    return hashlib.sha256(canonical(rows).encode()).hexdigest()


def replay(archive, upto=None):
    """{path: entry} as of manifest `upto` (default: the latest), with the chain verified."""
    archive = Path(archive)
    names = sorted((archive / STATE_DIR).glob("manifest-*.json"))
    if not names:
        raise SystemExit("no state manifests found")
    state, prev, seq = {}, None, -1
    for p in names:
        doc_bytes = p.read_bytes()
        doc = json.loads(doc_bytes)
        if doc["seq"] != seq + 1:
            raise SystemExit(f"manifest chain gap at {p.name}")
        parent_ok = doc["parent"] is None if prev is None else doc["parent"]["sha256"] == prev
        if not parent_ok:
            raise SystemExit(f"manifest chain broken at {p.name}")
        for e in doc["added"]:
            if e["path"] in state:
                raise SystemExit(f"{p.name} adds {e['path']} twice")
            state[e["path"]] = e
        if state_digest(state) != doc["state_sha256"]:
            raise SystemExit(f"state digest mismatch at {p.name}")
        seq, prev = doc["seq"], hashlib.sha256(doc_bytes).hexdigest()
        if upto is not None and seq == upto:
            return state, seq
    if upto is not None and upto > seq:
        raise SystemExit(f"manifest {upto} not found (latest is {seq})")
    return state, seq


def restore(archive, out, upto=None):
    archive, out = Path(archive), Path(out)
    state, seq = replay(archive, upto)
    res = {"seq": seq, "files": 0, "bytes": 0, "missing": [], "mismatched": []}
    for path, e in sorted(state.items()):
        src = archive / path
        if not src.is_file():
            res["missing"].append(path)
            continue
        if hashlib.sha256(src.read_bytes()).hexdigest() != e["sha256"]:
            res["mismatched"].append(path)
            continue
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            raise SystemExit(f"refusing to overwrite {dest}")
        shutil.copyfile(src, dest)
        res["files"] += 1
        res["bytes"] += e["size"]
    res["ok"] = not (res["missing"] or res["mismatched"])
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seq", type=int, help="known-good manifest number (default: latest)")
    a = ap.parse_args(argv)
    res = restore(a.archive, a.out, a.seq)
    print(json.dumps(res, indent=1))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
