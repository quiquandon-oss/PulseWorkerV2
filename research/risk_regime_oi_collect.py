"""Venue open interest + funding collection from a verified execution location. RESEARCH ONLY.

Runs the EXISTING collectors (`risk_regime_sources.collect_bybit_oi`, `collect_bybit_funding`, `collect_binance_oi`,
`collect_binance_funding`) and, additionally, Binance's own public data archive (data.binance.vision, published by
Binance: https://github.com/binance/binance-public-data), which keeps the same USD-M futures open-interest
statistics as daily 5-minute files with official SHA-256 checksums and is not limited to the REST API's 30 days.

Before collecting it records WHERE it runs (Cloudflare trace: country / colo) and how each provider answers.
Nothing is bypassed: a provider that refuses this location is reported with its own answer and contributes no data.
No proxy, no credentials, no paid source. Nothing is filled, interpolated or substituted.

Outputs (in --out-dir):
  manifest.json            location evidence, provider probes, per-source status / coverage / errors, checksums
  observations.jsonl.gz    normalised observations (common contract), deterministically de-duplicated
  raw_responses.jsonl.gz   every raw response body (REST JSON text; archive CSV text) with URL, status,
                           retrieval time and sha256 (archive zips: sha256 checked against Binance's .CHECKSUM)

Usage: python3 research/risk_regime_oi_collect.py --out-dir research/results/risk_regime_oi [--start 2026-01-01]
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk_regime_data as d  # noqa: E402
import risk_regime_sources as s  # noqa: E402

UTC = timezone.utc
FIVE_MIN = 5 * 60_000
ARCHIVE = "https://data.binance.vision/data/futures/um"
TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
PROBES = {"bybit": "https://api.bybit.com/v5/market/time", "binance_fapi": "https://fapi.binance.com/fapi/v1/time",
          "binance_archive": f"{ARCHIVE}/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-09-27.zip.CHECKSUM"}

ARCHIVE_SOURCES = {
    "binance_oi_archive": {
        "dimension": s.NEW, "group": "derivatives", "provider": "Binance USD-M Futures (public data archive)",
        "instrument": "BTCUSDT perpetual",
        "endpoint": f"GET {ARCHIVE}/daily/metrics/BTCUSDT/BTCUSDT-metrics-YYYY-MM-DD.zip (+ .CHECKSUM)",
        "docs": "https://github.com/binance/binance-public-data", "auth": "none", "cost_eur": 0, "resolution": "5m",
        "history": "daily files since 2021-12; each day's file is published after the day ends",
        "availability_rule": "create_time + 5 min: the same statistic was served live by /futures/data/openInterestHist "
                             "(period 5m); the archive file itself appears after the day ends (recorded in raw)",
        "unit": "BTC (sum_open_interest) and USD (sum_open_interest_value)", "v1_overlap": "none: V1 has no open-interest source",
    },
    "binance_funding_archive": {
        "dimension": s.HIGHER_RES, "group": "derivatives", "provider": "Binance USD-M Futures (public data archive)",
        "instrument": "BTCUSDT perpetual",
        "endpoint": f"GET {ARCHIVE}/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-YYYY-MM.zip (+ .CHECKSUM)",
        "docs": "https://github.com/binance/binance-public-data", "auth": "none", "cost_eur": 0, "resolution": "8h settlement",
        "history": "monthly files; the current month is not yet published", "availability_rule": "calc_time (settlement)",
        "unit": "rate per funding interval", "v1_overlap": "separate venue of V1's funding dimension (V1 reads Hyperliquid funding)",
    },
}


# ---------------- raw recording ----------------
class Recorder:
    """Wraps the existing Http opener: every response body is kept verbatim with its sha256."""

    def __init__(self, base: Callable = s.Http._urlopen):
        self.base = base
        self.rows: List[Dict] = []

    def __call__(self, url: str, timeout: int = 30) -> Tuple[int, bytes]:
        status, body = self.base(url, timeout)
        self.rows.append({"url": url, "status": status, "retrieved_at": int(time.time() * 1000),
                          "sha256": hashlib.sha256(body).hexdigest(), "body": body.decode("utf-8", "replace")})
        return status, body


def get_bytes(url: str, timeout: int = 60) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": s.USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b""
    except Exception as e:  # network-level failure is recorded, not hidden
        return -1, str(e).encode()


# ---------------- location evidence ----------------
def location(fetch: Callable = get_bytes) -> Dict:
    status, body = fetch(TRACE)
    kv = dict(line.split("=", 1) for line in body.decode("utf-8", "replace").splitlines() if "=" in line) if status == 200 else {}
    ip = kv.pop("ip", None)
    return {"source": TRACE, "status": status, "loc": kv.get("loc"), "colo": kv.get("colo"),
            "ip_sha256_prefix": hashlib.sha256(ip.encode()).hexdigest()[:12] if ip else None, "checked_at": d.iso(int(time.time() * 1000))}


def probe(fetch: Callable = get_bytes) -> Dict:
    out = {}
    for k, u in PROBES.items():
        st, body = fetch(u)
        out[k] = {"url": u, "status": st, "body_excerpt": body[:200].decode("utf-8", "replace")}
    return out


# ---------------- Binance public data archive ----------------
def _verify(zip_bytes: bytes, checksum_text: str) -> Optional[bool]:
    want = checksum_text.strip().split()[0].lower() if checksum_text.strip() else None
    return None if not want else hashlib.sha256(zip_bytes).hexdigest() == want


def _csv_rows(zip_bytes: bytes) -> Tuple[str, List[Dict]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = z.namelist()[0]
        text = z.read(name).decode("utf-8")
    return text, list(csv.DictReader(io.StringIO(text)))


def _ts(v: str) -> int:
    v = v.strip()
    if v.isdigit():
        return int(v)
    return int(datetime.strptime(v, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC).timestamp() * 1000)


def parse_metrics(text_rows: List[Dict], url: str, retrieved_at: int, file_sha: str) -> List[Dict]:
    obs = []
    for r in text_rows:
        ts = _ts(r["create_time"])
        raw = dict(r, archive_url=url, archive_sha256=file_sha)
        for metric, field, unit in (("open_interest", "sum_open_interest", "BTC"), ("open_interest_usd", "sum_open_interest_value", "USD")):
            if r.get(field) in (None, ""):
                continue
            obs.append(d.make_obs(source="binance_oi_archive", provider=ARCHIVE_SOURCES["binance_oi_archive"]["provider"],
                                  instrument="BTCUSDT", metric=metric, timestamp=ts, value=s._num(r[field]), unit=unit,
                                  interval="5m", retrieved_at=retrieved_at, historical_or_live=d.HISTORICAL,
                                  source_url=url, available_at=ts + FIVE_MIN, raw=raw))
    return obs


def parse_funding_archive(text_rows: List[Dict], url: str, retrieved_at: int, file_sha: str) -> List[Dict]:
    obs = []
    for r in text_rows:
        ts = _ts(r["calc_time"])
        obs.append(d.make_obs(source="binance_funding_archive", provider=ARCHIVE_SOURCES["binance_funding_archive"]["provider"],
                              instrument="BTCUSDT", metric="funding_rate", timestamp=ts, value=s._num(r["last_funding_rate"]),
                              unit=f"rate/{r.get('funding_interval_hours') or 8}h", interval="8h", retrieved_at=retrieved_at,
                              historical_or_live=d.HISTORICAL, source_url=url, available_at=ts,
                              raw=dict(r, archive_url=url, archive_sha256=file_sha)))
    return obs


def collect_archive(kind: str, labels: List[str], fetch: Callable = get_bytes, pause_s: float = 0.2) -> Dict:
    """kind = 'metrics' (daily labels YYYY-MM-DD) or 'fundingRate' (monthly labels YYYY-MM)."""
    sid = "binance_oi_archive" if kind == "metrics" else "binance_funding_archive"
    period = "daily" if kind == "metrics" else "monthly"
    obs, raws, files = [], [], Counter()
    for lab in labels:
        url = f"{ARCHIVE}/{period}/{kind}/BTCUSDT/BTCUSDT-{kind}-{lab}.zip"
        st, z = fetch(url)
        ret = int(time.time() * 1000)
        if st != 200:
            files[f"http_{st}"] += 1
            raws.append({"url": url, "status": st, "retrieved_at": ret, "body": z[:200].decode("utf-8", "replace")})
            continue
        cst, ctext = fetch(url + ".CHECKSUM")
        ok = _verify(z, ctext.decode("utf-8", "replace")) if cst == 200 else None
        sha = hashlib.sha256(z).hexdigest()
        files["checksum_ok" if ok else "checksum_failed" if ok is False else "checksum_missing"] += 1
        if ok is False:
            raws.append({"url": url, "status": st, "retrieved_at": ret, "sha256": sha, "checksum": ctext.decode("utf-8", "replace"),
                         "body": None, "note": "checksum mismatch: file not used"})
            continue
        text, rows = _csv_rows(z)
        parse = parse_metrics if kind == "metrics" else parse_funding_archive
        obs += parse(rows, url, ret, sha)
        raws.append({"url": url, "status": st, "retrieved_at": ret, "sha256": sha, "checksum": ctext.decode("utf-8", "replace").strip(),
                     "checksum_verified": ok, "body": text})
        time.sleep(pause_s)
    status = "OK" if obs and not files.get("checksum_failed") else ("EMPTY" if not obs else "PARTIAL")
    if not obs and files and all(k.startswith("http_") for k in files):
        status = "BLOCKED_OR_UNAVAILABLE"
    return {"source": sid, "status": status, "observations": obs, "raw": raws, "files": dict(files)}


def day_labels(start: datetime, end: datetime) -> List[str]:
    out, t = [], start
    while t <= end:
        out.append(t.strftime("%Y-%m-%d"))
        t += timedelta(days=1)
    return out


def month_labels(start: datetime, end: datetime) -> List[str]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ---------------- run ----------------
def coverage(obs: List[Dict]) -> Dict:
    by = Counter((o["metric"]) for o in obs)
    ts = [o["timestamp"] for o in obs]
    return {"observations": len(obs), "by_metric": dict(by), "first": d.iso(min(ts)) if ts else None, "last": d.iso(max(ts)) if ts else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--start", default="2026-01-01")
    args = ap.parse_args(argv)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    start_dt = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=UTC)
    start_ms = int(start_dt.timestamp() * 1000)
    manifest = {"artifact": "risk-regime-oi-collection", "collected_at": d.iso(now), "window_start": d.iso(start_ms),
                "location": location(), "probes": probe(), "sources": {}, "files": {},
                "rules": "no proxy, no credentials, no paid source, no substitution; refused requests are recorded, not retried elsewhere"}
    rec = Recorder()
    http = s.Http(opener=rec, log=Counter())
    store = d.ObservationStore()
    for fn in (s.collect_bybit_oi, s.collect_bybit_funding, s.collect_binance_oi, s.collect_binance_funding):
        r = s.run_collector(fn, http, start_ms, now, now)
        store.add(r["observations"])
        manifest["sources"][r["source"]] = {"status": r["status"], "error": r.get("error"), "requests": r.get("requests"),
                                            "note": r.get("note"), **coverage(r["observations"]), **s.SOURCES[r["source"]]}
    yesterday = datetime.fromtimestamp(now / 1000, UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    archive_raw = []
    for kind, labels in (("metrics", day_labels(start_dt, yesterday)), ("fundingRate", month_labels(start_dt, yesterday))):
        r = collect_archive(kind, labels)
        store.add(r["observations"])
        archive_raw += r["raw"]
        manifest["sources"][r["source"]] = {"status": r["status"], "files": r["files"], "requested_files": len(labels),
                                            **coverage(r["observations"]), **ARCHIVE_SOURCES[r["source"]]}
    manifest["http_log"] = dict(http.log)
    manifest["dedup"] = {"|".join(k): {"duplicates_dropped": store.duplicates.get(k, 0), "conflicts": len(store.conflicts.get(k, []))}
                         for k in store.keys()}
    with gzip.open(out / "observations.jsonl.gz", "wt") as f:
        for k in store.keys():
            for o in store.series(k):
                f.write(json.dumps(o, sort_keys=True) + "\n")
    with gzip.open(out / "raw_responses.jsonl.gz", "wt") as f:
        for r in rec.rows + archive_raw:
            f.write(json.dumps(r, sort_keys=True) + "\n")
    for name in ("observations.jsonl.gz", "raw_responses.jsonl.gz"):
        manifest["files"][name] = hashlib.sha256((out / name).read_bytes()).hexdigest()
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True, default=str) + "\n")
    print(json.dumps({"location": manifest["location"], "probes": {k: v["status"] for k, v in manifest["probes"].items()},
                      "sources": {k: (v["status"], v["observations"]) for k, v in manifest["sources"].items()}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
