#!/usr/bin/env python3
"""Archive the GDELT 2.0 file index needed to run the GDELT studies offline, and check a cache against it.

    # once, with network: fetch the tail of masterfilelist.txt and keep it verbatim
    python3 research/archive/gdelt_index.py fetch --cache CACHE --out-dir DIR
    # offline: rebuild the index subset from that kept tail, or just check a cache
    python3 research/archive/gdelt_index.py build --master DIR/masterfilelist_tail.txt.gz --fetched DIR/masterfilelist_tail.json --cache CACHE --out DIR/gdelt_master_index.json
    python3 research/archive/gdelt_index.py check --index DIR/gdelt_master_index.json --cache CACHE

The subset holds the provider's own lines (size, MD5, URL) for every 15-minute export from the first to the
last cached batch, so `gdelt_research_run.use_offline_index` can verify every cached zip without a network.
"""
import argparse
import gzip
import hashlib
import json
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import gdelt_geo_shock as g          # noqa: E402
import gdelt_research_run as gr      # noqa: E402

TAIL_BYTES = (12_000_000, 24_000_000, 48_000_000)
LOOKBACK = timedelta(hours=g.BASELINE_HOURS + 2)


def cached_batches(cache):
    return sorted(g.batch_ts_from_name(p.name) for p in Path(cache).glob("*.export.CSV.zip"))


def fetch_tail(earliest):
    """One HTTP Range request for the tail of the master list, grown only if it does not reach `earliest`."""
    for n in TAIL_BYTES:
        req = urllib.request.Request(g.GDELT_SOURCE["master_file_list"], headers={"User-Agent": gr.USER_AGENT, "Range": f"bytes=-{n}"})
        with urllib.request.urlopen(req, timeout=120) as r:
            status, body = r.status, r.read(n + 1)
        retrieved = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        text = body.decode("utf-8", "replace")
        first = text.split("\n", 1)[-1] if status == 206 else text
        stamps = [g.batch_ts_from_name(ln.split()[2]) for ln in first.splitlines() if len(ln.split()) == 3]
        if (stamps and min(stamps) <= earliest) or status == 200:
            return body, {"retrieved_at": retrieved, "http_status": status, "range": f"bytes=-{n}",
                          "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    raise RuntimeError(f"master list tail of {TAIL_BYTES[-1]} bytes does not reach {earliest}")


def build_index(tail_bytes, fetched, cache):
    batches = cached_batches(cache)
    text = tail_bytes.decode("utf-8", "replace")
    if fetched.get("http_status") == 206:
        text = text.split("\n", 1)[-1]            # the first line of a range response may be cut
    # reach back one baseline window (+2 h, as the studies request) before the first cached batch: the studies
    # read file *availability* over that window from the index even where they need no file
    return gr.archive_master_index(text, batches[0] - LOOKBACK, fetched)


def check_cache(index_path, cache):
    """Every cached zip against the archived index, plus every listed batch the cache lacks."""
    idx, doc = gr.load_offline_index(Path(index_path))
    by_name = {u.rsplit("/", 1)[-1]: (u, meta) for u, meta in idx.items()}
    res = {"index_batches": len(idx), "cached": 0, "ok": 0, "size_mismatch": [], "md5_mismatch": [], "not_listed": [],
           "listed_not_cached": []}
    present = set()
    for p in sorted(Path(cache).glob("*.export.CSV.zip")):
        res["cached"] += 1
        present.add(p.name)
        if p.name not in by_name:
            res["not_listed"].append(p.name)
            continue
        size, md5 = by_name[p.name][1]
        data = p.read_bytes()
        if len(data) != size:
            res["size_mismatch"].append(p.name)
        elif g.md5_hex(data) != md5:
            res["md5_mismatch"].append(p.name)
        else:
            res["ok"] += 1
    res["listed_not_cached"] = sorted(n for n in by_name if n not in present)
    res["complete"] = not (res["size_mismatch"] or res["md5_mismatch"] or res["not_listed"])
    return res


def dump(obj):
    return json.dumps(obj, indent=1, sort_keys=True) + "\n"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch")
    f.add_argument("--cache", required=True)
    f.add_argument("--out-dir", required=True)
    b = sub.add_parser("build")
    b.add_argument("--master", required=True)
    b.add_argument("--fetched", required=True)
    b.add_argument("--cache", required=True)
    b.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--index", required=True)
    c.add_argument("--cache", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "fetch":
        out = Path(a.out_dir)
        out.mkdir(parents=True, exist_ok=True)
        body, fetched = fetch_tail(cached_batches(a.cache)[0] - LOOKBACK)
        (out / "masterfilelist_tail.txt.gz").write_bytes(gzip.compress(body, mtime=0))
        (out / "masterfilelist_tail.json").write_text(dump(fetched))
        (out / "gdelt_master_index.json").write_text(dump(build_index(body, fetched, a.cache)))
        print(dump(check_cache(out / "gdelt_master_index.json", a.cache)))
    elif a.cmd == "build":
        body = gzip.decompress(Path(a.master).read_bytes()) if a.master.endswith(".gz") else Path(a.master).read_bytes()
        fetched = json.loads(Path(a.fetched).read_text())
        if hashlib.sha256(body).hexdigest() != fetched["sha256"]:
            raise SystemExit("master list bytes do not match their recorded sha256")
        Path(a.out).write_text(dump(build_index(body, fetched, a.cache)))
    else:
        res = check_cache(a.index, a.cache)
        print(dump(res))
        return 0 if res["complete"] else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
