"""Risk Regime research data layer: historical reconstruction around real V1 / prediction failures. RESEARCH ONLY.

Builds, from the raw observations of `risk_regime_sources.py`:
- a synchronized raw timeline around Event #15 (and around every indexed failure episode),
- the 575-observation V1 alignment ("what did the external world look like when V1 made this call?"),
- a failure/event index reusing existing CryptoPulse definitions (research_events pr3-v1, the EXP-005 V1
  baseline call, outcome_engine's resolution rule),
- data-quality and coverage reports per source.

It computes no score, no weights, no thresholds, and makes no claim about which source matters.

Usage (all inputs are read-only extracts; caches come from earlier research runs):
  python3 research/risk_regime_reconstruction.py --v1 <v1_full.json> --predictions <predictions.json> \
      --events <research_events.json> --btc <btc_data.json> --hl-cache <dir> --hl-funding-cache <dir> \
      --gdelt-cache <dir> --out-dir research/results [--live]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import risk_regime_data as d  # noqa: E402
import risk_regime_sources as s  # noqa: E402

UTC = timezone.utc
HOUR, DAY = d.HOUR, d.DAY
EVENT15 = {"event_id": 15, "prediction_boundary_ms": 1790510487481, "event_ms": 1790564484213,
           "label": "BTC 84,429 -> 83,480 (about -1.1%) in the 24 h to 2026-09-28 03:01 UTC; prediction 1125 (p_up 0.73) issued 2026-09-27 12:01 UTC"}
# Timeline offsets relative to the prediction boundary (negative = before) and to the event (after).
TIMELINE = (("-24h", "boundary", -24 * HOUR), ("-12h", "boundary", -12 * HOUR), ("-6h", "boundary", -6 * HOUR),
            ("-3h", "boundary", -3 * HOUR), ("-1h", "boundary", -HOUR), ("-30m", "boundary", -30 * 60_000),
            ("prediction boundary", "boundary", 0), ("event", "event", 0), ("+1h", "event", HOUR), ("+3h", "event", 3 * HOUR),
            ("+6h", "event", 6 * HOUR), ("+12h", "event", 12 * HOUR), ("+24h", "event", 24 * HOUR))
MAX_AGE_MS = {"hyperliquid_hip3": 2 * HOUR, "hyperliquid_btc_funding": 2 * HOUR, "gdelt_events": 45 * 60_000,
              "bybit_oi": 2 * HOUR, "binance_oi": 2 * HOUR, "deribit_dvol": 2 * HOUR, "bybit_funding": 9 * HOUR,
              "binance_funding": 9 * HOUR, "defillama_stablecoins": 2 * DAY, "defillama_dex": 2 * DAY,
              "defillama_tvl": 2 * DAY, "gdelt_doc": HOUR}
RAW_EVENTS_PER_EPISODE = 200     # storage cap for GDELT event rows around non-Event-#15 episodes (by mentions)
V1_SOURCE_KEYS = {"g": "geopolitics", "m": "macrogeo", "o": "oil", "y": "yield10y", "n": "nasdaq", "s": "sp500",
                  "u": "usd", "fd": "funding", "ls": "longshort", "hf": "hypefunding"}


# ---------------- outcomes (outcome_engine, unchanged) ----------------
def v1_outcomes(v1: Sequence[Dict], btc: Sequence[Dict], horizon_hours: int = 24) -> Dict[int, Dict]:
    """Runs outcome_engine.compute_forward_returns_from_history UNCHANGED over an in-memory copy of the read-only
    `history` (ts, score) and `btc_data` (ts, btc_price) extracts."""
    import outcome_engine as oe
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE history (ts INTEGER, score REAL)")
    conn.execute("CREATE TABLE btc_data (ts INTEGER, btc_price REAL)")
    conn.executemany("INSERT INTO history VALUES (?, ?)", [(r["ts"], r.get("score")) for r in v1])
    conn.executemany("INSERT INTO btc_data VALUES (?, ?)", [(r["ts"], r["btc_price"]) for r in btc])
    out = {}
    lo, hi = v1[0]["ts"], v1[-1]["ts"]
    step = oe.MAX_WINDOW_MS - 1
    a = lo
    while a <= hi:
        for row in oe.compute_forward_returns_from_history(conn, a, min(a + step, hi + 1), horizon_hours):
            out[row["anchor_ts"]] = row
        a += step + 1
    return out


def v1_direction(score) -> Optional[str]:
    """The EXP-005 V1 baseline call (experiment5_agent._v1_baseline_direction): score >= 50 -> UP."""
    from experiment5_agent import _v1_baseline_direction
    return _v1_baseline_direction(score)


# ---------------- failure / event index ----------------
def failure_index(v1: Sequence[Dict], outcomes: Dict[int, Dict], preds: Sequence[Dict], events: Sequence[Dict]) -> List[Dict]:
    """Stable ids: RE-<event_id> (research_events, pr3-v1, unchanged), PRED-<id> (predictions table, model
    knn-core-v1, the model research_events calls 'V2'), PREDRUN-<horizon>-<first id> (consecutive incorrect
    predictions), V1OBS-<ts> (V1 baseline call vs realized 24h direction)."""
    out = []
    for e in events:
        out.append({"id": f"RE-{e['event_id']}", "type": e["category"], "subtype": e.get("direction"), "anchor_ts": e["event_ts"],
                    "detection_ts": e["detection_ts"], "definition": f"research_events {e['trigger_version']}: {e['trigger_metric']} (threshold {e.get('trigger_threshold')})",
                    "intensity": e.get("intensity")})
    resolved = [p for p in preds if p.get("realized_up") is not None and p["p_up"] != 0.5]
    for p in resolved:
        up = p["p_up"] > 0.5
        if up == bool(p["realized_up"]):
            continue
        out.append({"id": f"PRED-{p['id']}", "type": "PRED_UP_BTC_DOWN" if up else "PRED_DOWN_BTC_UP", "subtype": f"BTC_{p['horizon_h']}h",
                    "anchor_ts": p["ts"], "definition": "predictions: p_up > 0.5 vs realized_up (resolver's own field)",
                    "p_up": p["p_up"], "realized_return_pct": p.get("realized_return")})
    for h in sorted({p["horizon_h"] for p in resolved}):
        run: List[Dict] = []
        for p in [x for x in resolved if x["horizon_h"] == h] + [None]:
            wrong = p is not None and (p["p_up"] > 0.5) != bool(p["realized_up"])
            if wrong:
                run.append(p)
                continue
            if len(run) >= 2:
                out.append({"id": f"PREDRUN-{h}h-{run[0]['id']}", "type": "PRED_CONSECUTIVE_FAILURES", "subtype": f"BTC_{h}h",
                            "anchor_ts": run[0]["ts"], "run_length": len(run), "prediction_ids": [x["id"] for x in run],
                            "definition": "consecutive incorrect predictions of one horizon (same metric as research_events V2_FAILURE_CLUSTER, which uses >= 5)"})
            run = []
    for r in v1:
        o = outcomes.get(r["ts"])
        call = v1_direction(r.get("score"))
        if not o or o["outcome_status"] != "RESOLVED" or call is None or o["realized_direction"] in (None, "FLAT"):
            continue
        if call != o["realized_direction"]:
            out.append({"id": f"V1OBS-{r['ts']}", "type": f"V1_{call}_BTC_{o['realized_direction']}", "subtype": "BTC_24h",
                        "anchor_ts": r["ts"], "definition": "EXP-005 V1 baseline call (score >= 50 -> UP) vs outcome_engine 24h realized direction",
                        "v1_score": r.get("score"), "forward_return_pct": round(o["forward_return_pct"], 4)})
    return sorted(out, key=lambda x: (x["anchor_ts"], x["id"]))


# ---------------- timelines ----------------
def snapshot(store: d.ObservationStore, t_ms: int, keys: Sequence[Tuple[str, str, str]]) -> Dict[str, Dict]:
    """Latest value of each series available at t: value, its own timestamp, age, change vs the previous
    available observation (no interpolation, no fill)."""
    out = {}
    for k in keys:
        o, prev = store.latest_available_with_previous(k, t_ms)
        name = "|".join(k)
        if o is None:
            out[name] = {"status": "NOT_AVAILABLE_AT_T"}
            continue
        age = t_ms - o["timestamp"]
        lim = MAX_AGE_MS.get(k[0])
        delta = None
        if prev and isinstance(o["value"], (int, float)) and isinstance(prev["value"], (int, float)):
            delta = o["value"] - prev["value"]
        out[name] = {"status": "STALE" if lim and age > lim else "OK", "value": o["value"], "timestamp": d.iso(o["timestamp"]),
                     "age_min": round(age / 60_000, 1), "change_vs_previous": delta, "available_at": d.iso(o["available_at"]),
                     "historical_or_live": o["historical_or_live"]}
    return out


def timeline(store: d.ObservationStore, boundary_ms: int, event_ms: int, keys: Sequence[Tuple[str, str, str]]) -> List[Dict]:
    rows = []
    for label, anchor, off in TIMELINE:
        t = (boundary_ms if anchor == "boundary" else event_ms) + off
        rows.append({"label": label, "t": d.iso(t), "t_ms": t, "before_prediction": t <= boundary_ms, "values": snapshot(store, t, keys)})
    return rows


def compact_timeline(store, anchor_ms: int, keys) -> Dict:
    """Same offsets around a single anchor (episodes whose prediction time is also the reference time)."""
    offs = [("-24h", -24), ("-12h", -12), ("-6h", -6), ("-3h", -3), ("-1h", -1), ("anchor", 0), ("+1h", 1), ("+3h", 3), ("+6h", 6), ("+12h", 12), ("+24h", 24)]
    cols = ["|".join(k) for k in keys]
    rows = []
    for lab, h in offs:
        t = anchor_ms + h * HOUR
        snap = snapshot(store, t, keys)
        rows.append({"label": lab, "t": d.iso(t), "values": [[snap[c].get("value"), snap[c].get("age_min"), snap[c]["status"]] for c in cols]})
    return {"columns": cols, "rows": rows}


# ---------------- GDELT raw events ----------------
def gdelt_raw_events(windows: Dict[str, Tuple[int, int]], cache: Path, idx, retrieved_at: int, cap: Optional[Dict[str, int]] = None) -> Dict[str, List[Dict]]:
    """Relevant (conflict-category) GDELT event rows per window, parsed from the MD5-verified cached exports."""
    import gdelt_geo_shock as g
    import gdelt_research_run as gr
    out = {}
    for name, (lo, hi) in windows.items():
        rows, seen = [], set()
        b = datetime.fromtimestamp((lo // (15 * 60_000)) * 15 * 60, UTC)
        end = datetime.fromtimestamp(hi / 1000, UTC)
        while b <= end:
            status, data, _ = gr.fetch_batch(b, idx, cache)
            if gr.OFFLINE["index"] is not None and status in gr.OFFLINE_PROBLEMS:
                raise gr.OfflineError(f"archived GDELT batch {g.stamp(b)} unusable: {status}")
            if data is not None:
                for e in g.parse_export_zip(data, g.export_url(b)):
                    if e.event_id in seen:
                        continue
                    seen.add(e.event_id)
                    c = g.classify(e)
                    if c.relevant:
                        rows.append(s.gdelt_event_record(e, b, c, retrieved_at))
            b = b + timedelta(minutes=15)
        rows.sort(key=lambda r: (-r["num_mentions"], r["first_seen"], r["global_event_id"]))
        lim = (cap or {}).get(name)
        out[name] = {"window": [d.iso(lo), d.iso(hi)], "relevant_events": len(rows), "stored": min(len(rows), lim) if lim else len(rows),
                     "storage_cap": lim, "events": rows[:lim] if lim else rows}
    return out


# ---------------- runner ----------------
def embed_page(template: str, data: Dict) -> str:
    """Self-contained variant of the Event Research page (data inlined, `</` escaped so it cannot close the script)."""
    return template.replace("/*__EMBEDDED_DATA__*/null", json.dumps(data, default=str).replace("</", "<\\/"))


def _gz_text(path: Path):
    """gzip text writer with a zero header timestamp, so identical content gives identical bytes."""
    import io
    return io.TextIOWrapper(gzip.GzipFile(filename=str(path), mode="wb", mtime=0), encoding="utf-8")


RETRIEVED_AT_FILE = ".retrieved_at.json"     # archived caches record their original retrieval time here


def _mtime(path: Path) -> int:
    """Retrieval time of a cache: the recorded one when archived (copies and downloads change file mtimes),
    otherwise the oldest file mtime."""
    rec = path / RETRIEVED_AT_FILE
    if rec.exists():
        return int(json.loads(rec.read_text())["retrieved_at_ms"])
    files = [p for p in path.iterdir() if p.is_file()] if path.exists() else []
    return int(min(p.stat().st_mtime for p in files) * 1000) if files else int(time.time() * 1000)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    for a in ("--v1", "--predictions", "--events", "--btc", "--hl-cache", "--hl-funding-cache", "--gdelt-cache", "--out-dir"):
        ap.add_argument(a, required=True)
    ap.add_argument("--live", action="store_true", help="also call the live third-party APIs (Bybit, Binance, Deribit, DeFiLlama, GDELT DOC)")
    ap.add_argument("--gdelt-index", help="archived GDELT master-list subset: offline run, no GDELT network access")
    args = ap.parse_args(argv)
    import gdelt_research_run as gr
    if args.gdelt_index and args.live:
        ap.error("--gdelt-index (offline) cannot be combined with --live")
    gr.use_offline_index(Path(args.gdelt_index) if args.gdelt_index else None)
    v1 = sorted(json.load(open(args.v1)), key=lambda r: r["ts"])
    preds = sorted(json.load(open(args.predictions)), key=lambda r: r["ts"])
    events = sorted(json.load(open(args.events)), key=lambda r: r["event_id"])
    btc = sorted(json.load(open(args.btc)), key=lambda r: (r["ts"], r["btc_price"]))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    now = int(time.time() * 1000)
    if gr.OFFLINE["index"] is not None:
        # offline: the run is "as of" its newest archived input, so repeated runs produce identical artifacts
        now = max(_mtime(Path(args.hl_cache)), _mtime(Path(args.hl_funding_cache)), _mtime(Path(args.gdelt_cache)))
    win_lo, win_hi = v1[0]["ts"] - 9 * DAY, max(v1[-1]["ts"], EVENT15["event_ms"] + 24 * HOUR) + HOUR
    store = d.ObservationStore()
    runs: Dict[str, Dict] = {}

    # --- Hyperliquid (reused cache) ---
    import hyperliquid_asset_universe_run as ur
    from hyperliquid_research_run import fetch_funding
    log = Counter()
    hl_ret = _mtime(Path(args.hl_cache))
    candles = {sym: ur.candles(sym, "1h", v1[0]["ts"] - 9 * DAY, win_hi, Path(args.hl_cache), log) for sym in s.HL_SYMBOLS}
    obs = s.hyperliquid_obs(candles, hl_ret)
    runs["hyperliquid_hip3"] = {"status": "OK", "observations": len(obs), "requests": dict(log), "retrieved_at": d.iso(hl_ret)}
    store.add(obs)
    flog = Counter()
    f_ret = _mtime(Path(args.hl_funding_cache))
    fund = fetch_funding(v1[0]["ts"] - 9 * DAY, win_hi, Path(args.hl_funding_cache), flog)
    obs = s.hyperliquid_funding_obs(fund, f_ret)
    runs["hyperliquid_btc_funding"] = {"status": "OK", "observations": len(obs), "requests": dict(flog), "retrieved_at": d.iso(f_ret)}
    store.add(obs)

    # --- GDELT events (reused cache + frozen component) ---
    import gdelt_geo_shock as g
    import gdelt_research_run as gr
    gcache = Path(args.gdelt_cache)
    g_ret = _mtime(gcache)
    lo_dt = datetime.fromtimestamp(v1[0]["ts"] / 1000, UTC)
    times = gr._grid(lo_dt, datetime.fromtimestamp(v1[-1]["ts"] / 1000, UTC))
    batches = g.required_batches(times)
    idx = gr.master_index(min(batches[0], lo_dt - timedelta(hours=g.BASELINE_HOURS + 2)))
    series, _, glog = gr.build_series(batches, idx, gcache, datetime.fromtimestamp(EVENT15["event_ms"] / 1000, UTC))
    if gr.OFFLINE["index"] is not None and gr.offline_problems(glog):
        bad = gr.offline_problems(glog)
        raise gr.OfflineError(f"{len(bad)} archived GDELT batches unusable, first {bad[0]}")
    obs = s.gdelt_batch_obs(series, g_ret)
    grid_file = next(iter(sorted(gcache.glob("_geo_grid_*.json"))), None)
    if grid_file:
        obs += s.geo_shock_obs(json.loads(grid_file.read_text())["rows"], g_ret)
    runs["gdelt_events"] = {"status": "OK", "observations": len(obs), "batches": len(series), "fetch": glog["fetch_status_counts"],
                            "geo_shock_grid": grid_file.name if grid_file else None, "retrieved_at": d.iso(g_ret)}
    store.add(obs)

    # --- live third-party sources (each reports its own status; nothing substituted) ---
    http = s.Http(log=Counter())
    # DOC rate-limits per (shared) egress IP: patient capped retries, and successful responses are cached next to the
    # GDELT export cache (with their real retrieval time) so an interrupted run resumes instead of starting over.
    doc_http = s.Http(min_interval_s=5.5, retries=40, max_backoff_s=10, cache_dir=gcache / "_doc_api", log=Counter())
    re_windows = [(e["event_ts"] - 24 * HOUR, e["event_ts"] + 24 * HOUR) for e in events]
    ev_window = (EVENT15["prediction_boundary_ms"] - 24 * HOUR, EVENT15["event_ms"] + 24 * HOUR)
    live_jobs = {
        "bybit_oi": (s.collect_bybit_oi, (http, win_lo, win_hi, now)),
        "bybit_funding": (s.collect_bybit_funding, (http, win_lo, win_hi, now)),
        "binance_oi": (s.collect_binance_oi, (http, win_lo, win_hi, now)),
        "binance_funding": (s.collect_binance_funding, (http, win_lo, win_hi, now)),
        "deribit_dvol": (s.collect_deribit_dvol, (http, win_lo, win_hi, now)),
        "deribit_options_snapshot": (s.collect_deribit_options_snapshot, (http, now)),
        "defillama_stablecoins": (s.collect_defillama_stablecoins, (http, win_lo, win_hi, now)),
        "defillama_dex": (s.collect_defillama_dex, (http, win_lo, win_hi, now)),
        "defillama_tvl": (s.collect_defillama_tvl, (http, win_lo, win_hi, now)),
        "gdelt_doc": (s.collect_gdelt_doc, (doc_http, [ev_window] + re_windows, now)),
    }
    doc_articles = []
    for name, (fn, a) in live_jobs.items():
        if not args.live:
            runs[name] = {"status": "NOT_RUN", "note": "run with --live"}
            continue
        r = s.run_collector(fn, *a)
        store.add(r["observations"])
        doc_articles += r.get("articles", [])
        runs[name] = {k: v for k, v in r.items() if k not in ("observations", "articles")}
        runs[name]["observations"] = len(r["observations"])
    runs["_http_log"] = {"http": dict(http.log), "gdelt_doc": dict(doc_http.log)}

    # --- quality / inventory ---
    keys = store.keys()
    q = {"|".join(k): d.quality(store, k, (win_lo, win_hi)) for k in keys}
    b_ms, e_ms = EVENT15["prediction_boundary_ms"], EVENT15["event_ms"]
    inventory = {"artifact": "risk-regime-source-inventory", "generated_at": d.iso(now), "window": [d.iso(win_lo), d.iso(win_hi)],
                 "v1_impact": "NONE: research data layer only. No score, no weights, no thresholds, no V1 change.",
                 "contract_fields": list(d.FIELDS) + ["raw"], "sources": {}, "series_quality": q}
    for sid, meta in s.SOURCES.items():
        sk = [k for k in keys if k[0] == sid]
        before = [k for k in sk if store.latest_available(k, b_ms)]
        at_event = [k for k in sk if store.latest_available(k, e_ms)]
        run = runs.get(sid, {})
        inventory["sources"][sid] = {**meta, "status": meta.get("status") or run.get("status", "NOT_RUN"), "run": run,
                                     "series": len(sk), "observations": sum(len(store.series(k)) for k in sk),
                                     "first": min((f for k in sk if (f := q["|".join(k)].get("first"))), default=None),
                                     "last": max((f for k in sk if (f := q["|".join(k)].get("last"))), default=None),
                                     # a live-only snapshot is timestamped at retrieval, outside the research window
                                     "first_any": min((d.iso(store.series(k)[0]["timestamp"]) for k in sk), default=None),
                                     "last_any": max((d.iso(store.series(k)[-1]["timestamp"]) for k in sk), default=None),
                                     "event15_series_available_before_boundary": len(before),
                                     "event15_series_available_at_event": len(at_event)}
    inventory["http"] = runs["_http_log"]

    # --- Event #15 ---
    tl_keys = [k for k in keys if not (k[0] == "hyperliquid_hip3" and k[2] == "volume")] + [k for k in keys if k[0] == "hyperliquid_hip3" and k[2] == "volume"]
    raw_windows = {"event15": ev_window}
    raw_caps = {}
    for e in events:
        if e["event_id"] != 15:
            raw_windows[f"RE-{e['event_id']}"] = (e["event_ts"] - 24 * HOUR, e["event_ts"] + 6 * HOUR)
            raw_caps[f"RE-{e['event_id']}"] = RAW_EVENTS_PER_EPISODE
    raw_ev = gdelt_raw_events(raw_windows, gcache, idx, g_ret, raw_caps)
    e15 = raw_ev.pop("event15")
    boundary_events = [r for r in e15["events"] if r["available_at"] <= b_ms]
    event15 = {"artifact": "risk-regime-event15-raw-timeline", "event": EVENT15,
               "note": "Raw values only. No score, no ranking of sources.",
               "timeline": timeline(store, b_ms, e_ms, tl_keys),
               "gdelt_events_window": e15["window"], "gdelt_relevant_events_in_window": e15["relevant_events"],
               "gdelt_relevant_events_available_before_boundary": len(boundary_events),
               "gdelt_events_before_boundary_top": boundary_events[:100],
               "gdelt_events_boundary_to_event_top": [r for r in e15["events"] if b_ms < r["available_at"] <= e_ms][:100],
               "gdelt_doc_articles": [a for a in doc_articles if ev_window[0] <= a["first_seen"] <= ev_window[1]],
               "source_status": {sid: inventory["sources"][sid]["status"] for sid in s.SOURCES}}

    # --- V1 alignment + failure index ---
    outs = v1_outcomes(v1, btc)
    idx_rows = failure_index(v1, outs, preds, events)
    cols = ["|".join(k) for k in keys]
    rows = []
    for r in v1:
        o = outs.get(r["ts"], {})
        snap = {}
        for k, c in zip(keys, cols):
            ob = store.latest_available(k, r["ts"])
            snap[c] = None if ob is None else [ob["value"], round((r["ts"] - ob["timestamp"]) / 60_000, 1)]
        rows.append({"ts": r["ts"], "t": d.iso(r["ts"]), "v1_score": r.get("score"), "v1_direction": v1_direction(r.get("score")),
                     "v1_sources": {name: r.get(k) for k, name in V1_SOURCE_KEYS.items()},
                     "btc_outcome_24h": {k: o.get(k) for k in ("btc_price_at_anchor", "realized_btc_price", "realized_future_ts", "forward_return_pct", "realized_direction", "outcome_status")},
                     "research": snap})
    history = {"artifact": "risk-regime-history", "v1_observations": len(rows),
               "inputs_sha256": {n: hashlib.sha256(json.dumps(x, sort_keys=True).encode()).hexdigest()
                                 for n, x in (("v1", v1), ("predictions", preds), ("research_events", events), ("btc_data", btc))},
               "research_columns": cols, "research_cell": "[value, age_minutes] of the latest observation available at the V1 timestamp; null = nothing available",
               "outcome_rule": "outcome_engine.compute_forward_returns_from_history (unchanged), 24h, over the read-only btc_data extract",
               "direction_rule": "experiment5_agent._v1_baseline_direction: score >= 50 -> UP",
               "failure_index": idx_rows, "failure_index_counts": dict(Counter(x["type"] for x in idx_rows)), "rows": rows}

    episodes = [x for x in idx_rows if x["id"].startswith(("RE-", "PREDRUN-", "PRED-"))]
    ep_tl = {x["id"]: compact_timeline(store, x["anchor_ts"], tl_keys) for x in episodes}
    ep_out = {"artifact": "risk-regime-episodes", "episodes": episodes, "timelines": ep_tl,
              "gdelt_top_events": {k: v for k, v in raw_ev.items()}, "event15": event15}

    def write(name, obj):
        p = out_dir / name
        p.write_text(json.dumps(obj, sort_keys=True, indent=1, default=str) + "\n")
        return p
    write("risk_regime_source_inventory.json", inventory)
    write("risk_regime_event15.json", event15)
    write("risk_regime_history.json", history)
    write("risk_regime_episodes.json", ep_out)
    raw_dir = out_dir / "risk_regime_raw"
    raw_dir.mkdir(exist_ok=True)
    with _gz_text(raw_dir / "observations.jsonl.gz") as f:
        for k in keys:
            for o in store.series(k):
                f.write(json.dumps(o, sort_keys=True) + "\n")
    if doc_articles:
        with _gz_text(raw_dir / "gdelt_doc_articles.jsonl.gz") as f:
            for a in doc_articles:
                f.write(json.dumps(a, sort_keys=True) + "\n")
    page = Path(__file__).resolve().parent / "event_research.html"
    if page.exists():
        # The page loads risk_regime_episodes.json from its own directory. embed_page() can produce a single
        # offline file instead; it is not written by default to avoid committing the data twice.
        (out_dir / "risk_regime_event_research.html").write_text(page.read_text())
    print("OK", {k: v.get("status") for k, v in inventory["sources"].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
