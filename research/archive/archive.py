#!/usr/bin/env python3
"""Offline research archive: inventory, Parquet build, verification, DuckDB queries and a Drive sync plan.

Everything here reads git objects or local files only. It opens no network connection, needs no D1 or
Google credentials, and never writes outside the --out directory it is given.

    python3 research/archive/archive.py inventory --repo . --scratch "$S" --out inventory.json
    python3 research/archive/archive.py build     --repo . --scratch "$S" --out STAGE
    python3 research/archive/archive.py verify    --stage STAGE
    python3 research/archive/archive.py sync-plan --stage STAGE --out sync_plan.json
"""
import argparse
import ast
import csv
import datetime as dt
import fnmatch
import glob
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CATALOG = HERE / "catalog.json"
ARCHIVE_ROOT = "CryptoPulseV2-Research-Archive"
TOOL_VERSION = "research-archive-v1"

# Drive folder for each dataset status (see RESEARCH_ARCHIVE.md, section D).
STATUS_FOLDER = {
    "PREREGISTRATION": "00_preregistrations",
    "FROZEN_SNAPSHOT": "10_raw/frozen",
    "PROSPECTIVE_APPEND_ONLY": "10_raw/prospective",
    "MUTABLE_EXPORT": "10_raw/exports",
    "RUN_RECORD": "30_run_records",
    "EPHEMERAL_LOCAL": "10_raw/session_extracts",
}


# ---------------------------------------------------------------- hashing

def sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def pinned_hash(rule, data):
    """Recompute a pre-registration or extract hash with the rule its own study used."""
    if rule == "file_bytes":
        return sha256_bytes(data)
    if rule == "json_sort_keys":
        return sha256_bytes(json.dumps(json.loads(data), sort_keys=True).encode())
    if rule == "json_canonical_compact":
        return sha256_bytes(canonical(json.loads(data)).encode())
    if rule.startswith("python_constant_json_sort_keys:"):
        name = rule.split(":", 1)[1]
        value = _python_constant(data.decode(), name)
        return sha256_bytes(json.dumps(value, sort_keys=True).encode())
    raise ValueError(f"unknown hash rule {rule}")


def _python_constant(src, name):
    """Evaluate a module-level literal constant without importing (executing) the module."""
    tree = ast.parse(src)
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return ast.literal_eval(node.value)
    raise KeyError(name)


# ---------------------------------------------------------------- sources

class GitSource:
    """Files of one ref, read from the local object store (no fetch)."""

    def __init__(self, repo, ref):
        self.repo, self.ref = repo, ref
        self.commit = self._git("rev-parse", ref).strip()
        self.commit_time = self._git("show", "-s", "--format=%cI", self.commit).strip()
        self.tree = {}
        for line in self._git("ls-tree", "-r", "-l", self.commit).splitlines():
            meta, path = line.split("\t", 1)
            _mode, _type, blob, size = meta.split()
            self.tree[path] = (blob, int(size))

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True).stdout

    def match(self, patterns):
        return sorted(p for p in self.tree if any(fnmatch.fnmatchcase(p, pat) for pat in patterns))

    def read(self, path):
        return subprocess.run(["git", "-C", str(self.repo), "cat-file", "blob", self.tree[path][0]],
                              check=True, capture_output=True).stdout

    def describe(self):
        return {"kind": "git", "ref": self.ref, "commit": self.commit, "commit_time": self.commit_time}


class LocalSource:
    def __init__(self, root):
        self.root = Path(root)

    def match(self, patterns):
        out = set()
        for pat in patterns:
            for p in glob.glob(str(self.root / pat), recursive=True):
                if os.path.isfile(p):
                    out.add(os.path.relpath(p, self.root))
        return sorted(out)

    def read(self, path):
        return (self.root / path).read_bytes()

    def describe(self):
        return {"kind": "local", "root": str(self.root)}


def open_source(ds, repo, scratch):
    src = ds["source"]
    if src["kind"] == "git":
        return GitSource(repo, src["ref"])
    if not scratch:
        return None
    return LocalSource(scratch)


# ---------------------------------------------------------------- records

def records(fmt, data):
    """Rows of a dataset file in their original form (dicts, values untouched)."""
    if fmt.startswith("jsonl"):
        text = gzip.decompress(data).decode() if fmt.endswith(".gz") else data.decode()
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if fmt == "csv":
        return list(csv.DictReader(io.StringIO(data.decode())))
    if fmt == "json":
        obj = json.loads(data)
        return obj if isinstance(obj, list) and all(isinstance(r, dict) for r in obj) else None
    return None


def file_format(ds, path):
    if path.endswith(".jsonl.gz"):
        return "jsonl.gz"
    if path.endswith(".jsonl"):
        return "jsonl"
    if path.endswith(".csv"):
        return "csv"
    if path.endswith(".json"):
        return "json"
    return "binary"


def to_ms(v):
    """Epoch ms from an int/str epoch or an ISO-8601 string; None when absent."""
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v)
    if s.lstrip("-").isdigit():
        return int(s)
    try:
        return int(dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def iso(ms):
    return None if ms is None else dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def series_stats(rows, ds):
    """Row count, time range and, for regular series, the gaps between consecutive observations."""
    tf, af = ds.get("time_field"), ds.get("available_field")
    keys = ds.get("series_key")
    out = {"rows": len(rows)}
    times = [t for t in (to_ms(r.get(tf)) for r in rows) if t is not None] if tf else []
    if times:
        out["first"], out["last"] = iso(min(times)), iso(max(times))
    if af:
        avail = [t for t in (to_ms(r.get(af)) for r in rows) if t is not None]
        if avail:
            out["available_first"], out["available_last"] = iso(min(avail)), iso(max(avail))
    if not (keys and tf):
        return out
    groups = {}
    for r in rows:
        t = to_ms(r.get(tf))
        if t is not None:
            groups.setdefault(tuple(str(r.get(k)) for k in keys), []).append((t, r))
    series = []
    for key, items in sorted(groups.items()):
        items.sort(key=lambda x: x[0])
        ts = [t for t, _ in items]
        step = ds.get("expected_step_ms") or (ds.get("step_by_interval_ms") or {}).get(items[0][1].get("interval"))
        s = {"key": "|".join(key), "rows": len(ts), "first": iso(ts[0]), "last": iso(ts[-1]),
             "duplicate_timestamps": len(ts) - len(set(ts))}
        if step:
            # half a step of tolerance: settlement times carry millisecond jitter (e.g. 08:00:00.042)
            gaps = [(a, b) for a, b in zip(ts, ts[1:]) if b - a > step + step // 2]
            s["step_ms"] = step
            s["missing_steps"] = sum(round((b - a) / step) - 1 for a, b in gaps)
            s["gaps"] = len(gaps)
            s["largest_gap_h"] = round(max((b - a for a, b in gaps), default=0) / 3600000, 2)
        series.append(s)
    out["series"] = series
    return out


# ---------------------------------------------------------------- inventory

def inventory(repo, scratch, catalog=None):
    catalog = catalog or json.loads(CATALOG.read_text())
    out = {"tool": TOOL_VERSION, "generated_at": iso(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)),
           "catalog_version": catalog["catalog_version"], "datasets": []}
    sources = {}
    for ds in catalog["datasets"]:
        key = (ds["source"]["kind"], ds["source"].get("ref"))
        if key not in sources:
            sources[key] = open_source(ds, repo, scratch)
        src = sources[key]
        entry = {k: ds[k] for k in ("id", "title", "status", "format", "provenance") if k in ds}
        entry["known_gaps"] = ds.get("known_gaps")
        if src is None:
            entry["location"] = {"kind": "local", "available": False}
            out["datasets"].append(entry)
            continue
        entry["location"] = src.describe()
        files = src.match(ds["source"]["paths"])
        entry["files"], all_rows = [], []
        for path in files:
            data = src.read(path)
            f = {"path": path, "bytes": len(data), "sha256": sha256_bytes(data), "format": file_format(ds, path)}
            if f["format"].endswith(".gz"):
                f["uncompressed_bytes"] = len(gzip.decompress(data))
            pin = (ds.get("pinned_sha256") or {}).get(path)
            if pin:
                got = pinned_hash(pin["rule"], data)
                f["pinned"] = {"rule": pin["rule"], "expected": pin["sha256"], "actual": got, "ok": got == pin["sha256"]}
            rows = records(f["format"], data) if ds.get("table") or ds.get("time_field") else None
            if rows is not None:
                f["rows"] = len(rows)
                in_pq = not ds.get("parquet_paths") or any(fnmatch.fnmatchcase(path, p) for p in ds["parquet_paths"])
                if in_pq:
                    all_rows.extend(rows)
            entry["files"].append(f)
        entry["file_count"] = len(files)
        entry["bytes"] = sum(f["bytes"] for f in entry["files"])
        entry["uncompressed_bytes"] = sum(f.get("uncompressed_bytes", f["bytes"]) for f in entry["files"])
        if all_rows:
            entry["stats"] = series_stats(all_rows, ds)
        out["datasets"].append(entry)
    return out


# ---------------------------------------------------------------- parquet

def _arrow():
    import pyarrow as pa
    import pyarrow.parquet as pq
    return pa, pq


def row_sha(rec):
    return sha256_bytes(canonical(rec).encode())


def rows_digest(rows):
    """One digest over the ordered per-row hashes (kept in file metadata; a per-row hash column would be
    incompressible and outweigh the data)."""
    return sha256_bytes("\n".join(row_sha(r) for r in rows).encode())


def to_table(rows, ds, source_path, source_sha):
    """Original fields kept exactly (same names and values); typed helper columns and lineage added.

    Helper columns start with an underscore so the original record is the set of columns without one.
    Nested values and columns whose types cannot be expressed uniformly are stored as canonical JSON
    text and listed in the file metadata, so verification can rebuild the original rows byte for byte.
    """
    pa, _ = _arrow()
    names = []
    for r in rows:
        for k in r:
            if k not in names:
                names.append(k)
    cols, json_cols = {}, []
    for n in names:
        vals = [r.get(n) for r in rows]
        present = [v for v in vals if v is not None]
        kinds = {type(v) for v in present}
        if kinds <= {str}:
            cols[n] = pa.array(vals, pa.string())
        elif kinds <= {bool}:
            cols[n] = pa.array(vals, pa.bool_())
        elif kinds <= {int}:
            cols[n] = pa.array(vals, pa.int64())
        elif kinds <= {float}:
            cols[n] = pa.array(vals, pa.float64())
        else:   # mixed int/float, dicts, lists, mixed types: exact JSON text
            json_cols.append(n)
            cols[n] = pa.array([None if v is None else canonical(v) for v in vals], pa.string())
    missing = {n: [n not in r for r in rows] for n in names}
    has_missing = [n for n in names if any(missing[n])]
    for n in has_missing:
        cols[f"_absent_{n}"] = pa.array(missing[n], pa.bool_())
    tf, af = ds.get("time_field"), ds.get("available_field")
    if tf:
        cols["_ts_ms"] = pa.array([to_ms(r.get(tf)) for r in rows], pa.int64())
    if af:
        cols["_available_at_ms"] = pa.array([to_ms(r.get(af)) for r in rows], pa.int64())
    if "retrieved_at" in names:
        cols["_retrieved_at_ms"] = pa.array([to_ms(r.get("retrieved_at")) for r in rows], pa.int64())
    for num in ("value", "c", "o", "h", "l", "v"):
        if num in names and cols[num].type == pa.string():
            cols[f"_{num}_f64"] = pa.array([_float(r.get(num)) for r in rows], pa.float64())
    cols["_line"] = pa.array(range(1, len(rows) + 1), pa.int64())
    meta = {"rows_digest": rows_digest(rows), "dataset_id": ds["id"], "source_path": source_path, "source_sha256": source_sha, "rows": str(len(rows)),
            "original_columns": canonical(names), "json_columns": canonical(json_cols), "tool": TOOL_VERSION}
    table = pa.table(cols)
    return table.replace_schema_metadata({k.encode(): v.encode() for k, v in meta.items()})


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def rebuild_rows(table):
    """Inverse of to_table: the original records, from the Parquet columns alone."""
    meta = {k.decode(): v.decode() for k, v in (table.schema.metadata or {}).items()}
    names, json_cols = json.loads(meta["original_columns"]), set(json.loads(meta["json_columns"]))
    data = table.to_pydict()
    absent = {n: data.get(f"_absent_{n}") for n in names}
    rows = []
    for i in range(table.num_rows):
        r = {}
        for n in names:
            if absent[n] is not None and absent[n][i]:
                continue
            v = data[n][i]
            r[n] = json.loads(v) if n in json_cols and v is not None else v
        rows.append(r)
    return rows, meta


# ---------------------------------------------------------------- staging build

def snapshot_id(src_desc, files_sha):
    if src_desc["kind"] == "git":
        return f"{src_desc['commit_time'][:10]}_git-{src_desc['commit'][:8]}"
    return f"local-{files_sha[:12]}"


def build(repo, scratch, stage, catalog=None):
    """Stage the archive locally in the Drive layout: byte-identical raw files, Parquet, manifests."""
    _, pq = _arrow()
    catalog = catalog or json.loads(CATALOG.read_text())
    root = Path(stage) / ARCHIVE_ROOT
    root.mkdir(parents=True, exist_ok=True)
    inv = inventory(repo, scratch, catalog)
    by_id = {d["id"]: d for d in catalog["datasets"]}
    manifests = []
    for entry in inv["datasets"]:
        if not entry.get("files"):
            continue
        ds = by_id[entry["id"]]
        src = GitSource(repo, ds["source"]["ref"]) if ds["source"]["kind"] == "git" else LocalSource(scratch)
        sid = snapshot_id(entry["location"], sha256_bytes("".join(f["sha256"] for f in entry["files"]).encode()))
        base = root / STATUS_FOLDER[ds["status"]] / ds["id"]
        raw_dir = base / f"snapshot={sid}"
        man = {"dataset_id": ds["id"], "snapshot_id": sid, "status": ds["status"], "location": entry["location"],
               "provenance": ds.get("provenance"), "tool": TOOL_VERSION, "files": [], "parquet": []}
        for f in entry["files"]:
            data = src.read(f["path"])
            dest = (base / "partitions" if _is_partition(ds, f["path"]) else raw_dir) / f["path"]
            _write_once(dest, data)
            man["files"].append({"archive_path": str(dest.relative_to(root)), "source_path": f["path"],
                                 "bytes": f["bytes"], "sha256": f["sha256"], "rows": f.get("rows"),
                                 "pinned": f.get("pinned")})
            in_pq = ds.get("table") and (not ds.get("parquet_paths") or any(fnmatch.fnmatchcase(f["path"], p) for p in ds["parquet_paths"]))
            rows = records(f["format"], data) if in_pq else None
            if rows:
                table = to_table(rows, ds, f["path"], f["sha256"])
                part = "partitions" if _is_partition(ds, f["path"]) else f"snapshot={sid}"
                pq_path = root / "20_parquet" / ds["table"] / f"dataset={ds['id']}" / part / (f["path"].replace("/", "__") + ".parquet")
                buf = io.BytesIO()
                pq.write_table(table, buf, compression="zstd")
                _write_once(pq_path, buf.getvalue())
                man["parquet"].append({"archive_path": str(pq_path.relative_to(root)), "source_path": f["path"],
                                       "rows": table.num_rows, "bytes": len(buf.getvalue()),
                                       "sha256": sha256_bytes(buf.getvalue())})
        man_path = root / "90_manifests" / f"{ds['id']}__{sid}.json"
        _write_once(man_path, (json.dumps(man, indent=1, sort_keys=True) + "\n").encode())
        manifests.append(man)
    shutil.copyfile(CATALOG, root / "90_manifests" / "catalog.json")
    write_sums(root)
    return {"root": str(root), "manifests": len(manifests)}


def _is_partition(ds, path):
    """Append-only stores keep each immutable partition once (not one copy per sync); their mutable files
    (an index rewritten by every run) go to the per-sync snapshot folder instead."""
    return ds["status"] == "PROSPECTIVE_APPEND_ONLY" and not any(
        fnmatch.fnmatchcase(path, p) for p in ds.get("mutable_paths", []))


def _write_once(path, data):
    """Immutable snapshots: a path is written once; rewriting it with different bytes is refused."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise RuntimeError(f"refusing to overwrite immutable archive file with different bytes: {path}")
        return
    tmp = path.with_suffix(path.suffix + ".partial")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def write_sums(root):
    lines = []
    for p in sorted(Path(root).rglob("*")):
        if p.is_file() and p.name != "SHA256SUMS":
            lines.append(f"{sha256_bytes(p.read_bytes())}  {p.relative_to(root)}")
    (Path(root) / "SHA256SUMS").write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------- secret scan

SECRET_PATTERNS = [
    re.compile(rb"Bearer\s+[A-Za-z0-9._~+/=-]{20,}"),           # HTTP bearer tokens
    re.compile(rb"ya29\.[A-Za-z0-9_-]{20,}"),                     # Google OAuth access tokens
    re.compile(rb"1//[A-Za-z0-9_-]{30,}"),                         # Google OAuth refresh tokens
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),                    # GitHub tokens
    re.compile(rb"AKIA[0-9A-Z]{16}"),                              # AWS access keys
    re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(rb"(?i)(cloudflare_api_token|api[_-]?key|client_secret)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9_-]{16,}"),
]


def secret_findings(root):
    """Credential-looking strings in staged text files (gzip members included). Archives must hold none."""
    found = []
    for p in sorted(Path(root).rglob("*")):
        if not p.is_file() or p.suffix in (".zip", ".parquet"):
            continue
        data = p.read_bytes()
        if p.suffix == ".gz":
            try:
                data = gzip.decompress(data)
            except OSError:
                continue
        for pat in SECRET_PATTERNS:
            if pat.search(data):
                found.append(f"{p.relative_to(root)}: matches {pat.pattern[:40]!r}")
    return found


# ---------------------------------------------------------------- verify

def verify(stage):
    """Check a staged (or downloaded) archive: checksums, pinned hashes and exact Parquet round trips."""
    _, pq = _arrow()
    root = Path(stage) / ARCHIVE_ROOT if (Path(stage) / ARCHIVE_ROOT).exists() else Path(stage)
    problems, checked = [], {"files": 0, "parquet": 0, "rows": 0, "pinned": 0}
    listed = {}
    for line in (root / "SHA256SUMS").read_text().splitlines():
        h, p = line.split("  ", 1)
        listed[p] = h
    present = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
    for p in sorted(set(listed) - present):
        problems.append(f"missing: {p}")
    for p in sorted(present - set(listed)):
        problems.append(f"not in SHA256SUMS: {p}")
    for p in sorted(set(listed) & present):
        if sha256_bytes((root / p).read_bytes()) != listed[p]:
            problems.append(f"checksum mismatch: {p}")
        checked["files"] += 1
    for man_path in sorted((root / "90_manifests").glob("*__*.json")):
        man = json.loads(man_path.read_text())
        raw = {f["source_path"]: f for f in man["files"]}
        for f in man["files"]:
            if f.get("pinned"):
                data = (root / f["archive_path"]).read_bytes()
                if pinned_hash(f["pinned"]["rule"], data) != f["pinned"]["expected"]:
                    problems.append(f"pinned hash changed: {f['archive_path']}")
                checked["pinned"] += 1
        for q in man["parquet"]:
            src = raw[q["source_path"]]
            try:
                table = pq.read_table(root / q["archive_path"])
                rebuilt, meta = rebuild_rows(table)
                original = records(file_format(None, src["source_path"]), (root / src["archive_path"]).read_bytes())
            except Exception as e:   # a corrupt or truncated file is a finding, not a crash
                problems.append(f"unreadable: {q['archive_path']} or {src['archive_path']}: {type(e).__name__}")
                continue
            if meta["source_sha256"] != src["sha256"]:
                problems.append(f"lineage mismatch: {q['archive_path']}")
            if rows_digest(rebuilt) != rows_digest(original):
                problems.append(f"parquet does not reproduce its source rows: {q['archive_path']}")
            if meta["rows_digest"] != rows_digest(original):
                problems.append(f"row digest differs: {q['archive_path']}")
            checked["parquet"] += 1
            checked["rows"] += len(original)
    for f in secret_findings(root):
        problems.append(f"credential-like content: {f}")
    return {"ok": not problems, "checked": checked, "problems": problems}


# ---------------------------------------------------------------- sync plan (no network)

def sync_plan(stage, remote_listing=None):
    """What a first upload would do. remote_listing = {archive_path: sha256} of what Drive already holds.

    Rules: upload only files that are absent remotely; a remote file with different bytes is a CONFLICT and
    is never overwritten; nothing is ever deleted remotely.
    """
    root = Path(stage) / ARCHIVE_ROOT if (Path(stage) / ARCHIVE_ROOT).exists() else Path(stage)
    remote = remote_listing or {}
    plan = {"root": ARCHIVE_ROOT, "upload": [], "already_present": [], "conflicts": [], "delete": []}
    for line in (root / "SHA256SUMS").read_text().splitlines():
        h, p = line.split("  ", 1)
        size = (root / p).stat().st_size
        if p not in remote:
            plan["upload"].append({"path": p, "bytes": size, "sha256": h})
        elif remote[p] == h:
            plan["already_present"].append(p)
        elif p.startswith("90_manifests/catalog.json"):
            plan["upload"].append({"path": p, "bytes": size, "sha256": h, "note": "catalog replaced; old version kept by Drive revision history"})
        else:
            plan["conflicts"].append({"path": p, "local": h, "remote": remote[p]})
    plan["upload"].append({"path": "SHA256SUMS", "bytes": (root / "SHA256SUMS").stat().st_size, "note": "uploaded last"})
    plan["upload_bytes"] = sum(u["bytes"] for u in plan["upload"])
    return plan


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("inventory", "build"):
        p = sub.add_parser(name)
        p.add_argument("--repo", default=".")
        p.add_argument("--scratch", default=os.environ.get("ARCHIVE_SCRATCH"))
        p.add_argument("--out", required=True)
    p = sub.add_parser("verify")
    p.add_argument("--stage", required=True)
    p = sub.add_parser("sync-plan")
    p.add_argument("--stage", required=True)
    p.add_argument("--remote-listing")
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "inventory":
        Path(a.out).write_text(json.dumps(inventory(a.repo, a.scratch), indent=1) + "\n")
    elif a.cmd == "build":
        print(json.dumps(build(a.repo, a.scratch, a.out)))
    elif a.cmd == "verify":
        res = verify(a.stage)
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    elif a.cmd == "sync-plan":
        remote = json.loads(Path(a.remote_listing).read_text()) if a.remote_listing else None
        Path(a.out).write_text(json.dumps(sync_plan(a.stage, remote), indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
