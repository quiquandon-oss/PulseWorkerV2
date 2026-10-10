"""EXP-23 forward-data evaluation (RESEARCH ONLY). Re-evaluates the BTC 12-hour early-warning pipeline
(research/early_warning_12h.py, spec v0.4.0) whenever the forward store has new data.

Inputs, all read-only and all from this repository (no Cloudflare, no D1, no secrets, no external service):
  --forward-store   research-data/risk-regime-forward : risk_regime_forward/ (daily collection, 10:40 UTC)
  --prices          research-data/market-moves @0eb5be0 : market_moves/hourly/BTC/observations.jsonl (pinned sha)
  --oi-funding      b5bcff8 : research/results/risk_regime_oi/observations.jsonl.gz (pinned sha)
  --definition      196b87e : research/market_moves/definition.json (optional; canonical sha checked)
Output (the only write target): --results  (research-data/exp23-evaluation : exp23_evaluation/)
  state.json                       last status, last evaluated dataset fingerprint, history
  reports/<fingerprint16>/report.json + progress.md   deterministic (no wall-clock inside)
  runs/<evaluated_at>_<run_id>.json   one record per run that changed something (evaluated or failed)
  latest_progress.md               copy of the latest progress report

Statuses (exit code): EVALUATED (0), NO_NEW_DATA (0; nothing written), INPUT_UNAVAILABLE (2),
INTEGRITY_FAILURE (3), EVALUATION_FAILED (4). A failure never advances the evaluated fingerprint, so the next run
retries the same data (recovery). Partitions failing integrity are excluded (their hours become missing; nothing is
filled) and listed; malformed or duplicated rows reject their whole partition.
"""
import argparse
import glob
import gzip
import hashlib
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import early_warning_12h as ew  # noqa: E402

PRICES_SHA256 = "e0141555ced46de20e130e184f995aa506f122fb2302cc8b52ffe3ef38a6501d"      # plan v1.0.0 pin
FROZEN_OI_SHA256 = "0cadd101115773a828412dee0b142a6614b39b4b92ada6403ef119d637451d01"   # risk_regime_oi @b5bcff8
FORWARD_SOURCES = ("hyperliquid_hip3", "binance_oi_archive", "binance_funding_archive")
EXIT = {"EVALUATED": 0, "NO_NEW_DATA": 0, "INPUT_UNAVAILABLE": 2, "INTEGRITY_FAILURE": 3, "EVALUATION_FAILED": 4}
REQUIRED_ROW_KEYS = ("timestamp", "available_at", "value", "metric")


def _sha(path):
    return ew.sha256_file(path)


def verify_forward_store(store):
    """Checks every partition against its meta and index. Returns (accepted {source: [files]}, problems, versions)."""
    problems, accepted, versions = [], {s: [] for s in FORWARD_SOURCES}, {}
    idx_path = os.path.join(store, "index.json")
    if not os.path.isfile(idx_path):
        return None, [{"code": "MISSING_INDEX", "path": "index.json"}], {}
    try:
        index = json.load(open(idx_path))["sources"]
    except Exception as e:  # noqa: BLE001
        return None, [{"code": "MALFORMED_INDEX", "detail": str(e)}], {}
    for src in FORWARD_SOURCES:
        for meta_path in sorted(glob.glob(os.path.join(store, src, "*", "*.meta.json"))):
            rel = os.path.relpath(meta_path, store)
            data = meta_path[: -len(".meta.json")] + ".jsonl.gz"
            try:
                meta = json.load(open(meta_path))
            except Exception as e:  # noqa: BLE001
                problems.append({"code": "MALFORMED_META", "path": rel, "detail": str(e)})
                continue
            if not os.path.isfile(data):
                problems.append({"code": "MISSING_PARTITION", "path": rel})
                continue
            sha = _sha(data)
            raw = data.replace(os.path.join(store, src), os.path.join(store, "raw", src))
            want_idx = index.get(src, {}).get("files", {}).get(meta.get("partition"))
            if sha != meta.get("file_sha256") or sha != want_idx:
                problems.append({"code": "CHECKSUM_MISMATCH", "path": os.path.relpath(data, store)})
                continue
            if os.path.isfile(raw) and _sha(raw) != meta.get("raw_sha256"):
                problems.append({"code": "RAW_CHECKSUM_MISMATCH", "path": os.path.relpath(raw, store)})
                continue
            bad = _row_problem(data)
            if bad:
                problems.append({"code": bad, "path": os.path.relpath(data, store)})
                continue
            accepted[src].append(data)
            versions[os.path.relpath(data, store)] = sha
    return accepted, problems, versions


def _row_problem(path):
    """MALFORMED_ROW / DUPLICATE_ROW for a partition, or None. Duplicates cannot occur in a valid partition."""
    seen = set()
    try:
        with gzip.open(path, "rt") as f:
            for line in f:
                r = json.loads(line)
                if any(k not in r for k in REQUIRED_ROW_KEYS) or not isinstance(r["timestamp"], int) \
                        or not isinstance(r["available_at"], int) or r["available_at"] < r["timestamp"]:
                    return "MALFORMED_ROW"
                key = (r.get("instrument"), r["metric"], r["timestamp"])
                if key in seen:
                    return "DUPLICATE_ROW"
                seen.add(key)
    except Exception:  # noqa: BLE001
        return "MALFORMED_ROW"
    return None


def fingerprint(versions, code_sha, spec_sha):
    return hashlib.sha256(json.dumps({"inputs": versions, "code": code_sha, "spec": spec_sha},
                                     sort_keys=True).encode()).hexdigest()


def _load_state(results):
    p = os.path.join(results, "state.json")
    return json.load(open(p)) if os.path.isfile(p) else {"history": [], "last_evaluated_fingerprint": None}


def _dump(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, indent=1, sort_keys=True)
        f.write("\n")


def progress_markdown(rep, fp, problems):
    cov = rep["decision_hours"]["coverage"]
    lab, cp = rep["labels"], rep["checkpoint"]
    lines = [
        "# EXP-23 BTC 12-hour early warning: forward progress",
        "",
        f"- Dataset fingerprint: `{fp}`",
        f"- Spec: v0.4.0 `{ew.SPEC_SHA256[:16]}…`; definition v1.0.0 `{ew.DEFINITION_SHA256[:16]}…`",
        f"- Status: **{rep['status']}** (checkpoint {cp['evaluable_events']}/{ew.CHECKPOINT_TOTAL} events, "
        f"UP {cp['up']}/{ew.CHECKPOINT_PER_DIR}, DOWN {cp['down']}/{ew.CHECKPOINT_PER_DIR})",
        f"- Post-seal price hours: {rep['data']['price_hours_post_seal']}; eligible decision hours: "
        f"{rep['decision_hours']['eligible']}",
        f"- Labelled hours: {lab['labelled_hours']} (VALID {lab['valid_hours']}); burn-in complete: "
        f"{lab['burn_in_complete']}; episodes: {lab['episodes']} (UP {lab['episodes_up']}, DOWN {lab['episodes_down']}); "
        f"burn-in crossings: {lab['burn_in_crossings']}",
        "",
        "| Feature set | Evaluable hours | First evaluable (ms) |",
        "|---|---|---|",
    ] + [f"| {k} | {v['evaluable_hours']} | {v['first_evaluable']} |" for k, v in cov.items()] + [
        "",
        "No performance metric is reported below the checkpoint (counts only).",
    ]
    if rep["blockers"]:
        lines += ["", "Blockers:"] + [f"- {b}" for b in rep["blockers"]]
    if problems:
        lines += ["", "Excluded inputs:"] + [f"- {p['code']}: {p.get('path', p.get('detail', ''))}" for p in problems]
    return "\n".join(lines) + "\n"


def evaluate(forward_store, prices, oi_funding, results, run_id, now_iso, definition=None):
    """One evaluation pass. Returns (status, record). Writes only under `results`."""
    state = _load_state(results)
    record = {"run_id": run_id, "evaluated_at": now_iso, "problems": []}

    def fail(status, detail):
        record.update(status=status, detail=detail)
        state.update(last_status=status, last_run=run_id, last_run_at=now_iso)
        state["history"].append({"run_id": run_id, "at": now_iso, "status": status})
        _dump(os.path.join(results, "runs", f"{now_iso.replace(':', '')}_{run_id}.json"), record)
        _dump(os.path.join(results, "state.json"), state)
        return status, record

    for label, path in (("prices", prices), ("oi_funding", oi_funding), ("forward_store", forward_store)):
        if not path or not os.path.exists(path):
            return fail("INPUT_UNAVAILABLE", f"{label} not found: {path}")
    if _sha(prices) != PRICES_SHA256:
        return fail("INTEGRITY_FAILURE", "price snapshot sha256 differs from the plan pin")
    if _sha(oi_funding) != FROZEN_OI_SHA256:
        return fail("INTEGRITY_FAILURE", "frozen OI/funding file sha256 differs from the pin")
    if definition:
        if ew.canonical_sha256(json.load(open(definition))) != ew.DEFINITION_SHA256:
            return fail("INTEGRITY_FAILURE", "definition.json canonical sha256 differs from v1.0.0")
    accepted, problems, versions = verify_forward_store(forward_store)
    record["problems"] = problems
    if accepted is None:
        return fail("INPUT_UNAVAILABLE", "forward store index missing or malformed")
    if not accepted["hyperliquid_hip3"]:
        return fail("INTEGRITY_FAILURE", "no forward price partition passed integrity checks")
    versions = dict(versions, **{"pinned/prices": PRICES_SHA256, "pinned/oi_funding": FROZEN_OI_SHA256})
    code_sha = {os.path.basename(p): _sha(p) for p in (ew.__file__, os.path.abspath(__file__))}
    fp = fingerprint(versions, code_sha, ew.SPEC_SHA256)
    record.update(dataset_fingerprint=fp, inputs=versions, code_sha256=code_sha)
    if state.get("last_evaluated_fingerprint") == fp:
        record["status"] = "NO_NEW_DATA"
        return "NO_NEW_DATA", record                      # nothing written: no duplicate processing
    try:
        merged, pconf = ew.merge_prices(ew.load_market_moves(prices), ew.load_forward_price_files(accepted["hyperliquid_hip3"]))
        venue = [oi_funding] + accepted["binance_oi_archive"] + accepted["binance_funding_archive"]
        oi, funding, vconf = ew.load_venue_series(venue)
        rep = ew.run(merged, oi, funding, conflicts=[["price", t] for t in pconf] + [list(c) for c in vconf])
        rep["inputs"] = versions
        rep["code_sha256"] = code_sha
        rep["excluded_inputs"] = problems
        rep["dataset_fingerprint"] = fp
        if rep["boundary"]["SEAL_END_inclusive"] != ew.SEAL_END or rep["labels"]["first_labelable"] <= ew.SEAL_END:
            raise AssertionError("sealed boundary violated")
    except Exception as e:  # noqa: BLE001
        return fail("EVALUATION_FAILED", f"{type(e).__name__}: {e}\n" + traceback.format_exc(limit=3))
    out_dir = os.path.join(results, "reports", fp[:16])
    _dump(os.path.join(out_dir, "report.json"), rep)
    md = progress_markdown(rep, fp, problems)
    for p in (os.path.join(out_dir, "progress.md"), os.path.join(results, "latest_progress.md")):
        with open(p, "w") as f:
            f.write(md)
    record.update(status="EVALUATED", evaluation_status=rep["status"], report=os.path.relpath(out_dir, results),
                  eligible_hours=rep["decision_hours"]["eligible"], labelled_hours=rep["labels"]["labelled_hours"],
                  episodes=rep["labels"]["episodes"], burn_in_complete=rep["labels"]["burn_in_complete"],
                  coverage={k: v["evaluable_hours"] for k, v in rep["decision_hours"]["coverage"].items()},
                  checkpoint=rep["checkpoint"], blockers=rep["blockers"])
    state.update(last_status="EVALUATED", last_evaluated_fingerprint=fp, last_report=record["report"],
                 last_run=run_id, last_run_at=now_iso)
    state["history"].append({"run_id": run_id, "at": now_iso, "status": "EVALUATED", "fingerprint": fp,
                             "evaluation_status": rep["status"]})
    _dump(os.path.join(results, "runs", f"{now_iso.replace(':', '')}_{run_id}.json"), record)
    _dump(os.path.join(results, "state.json"), state)
    return "EVALUATED", record


def main(argv=None):
    ap = argparse.ArgumentParser(description="EXP-23 forward-data evaluation (research only)")
    ap.add_argument("--forward-store", required=True)
    ap.add_argument("--prices", required=True)
    ap.add_argument("--oi-funding", required=True)
    ap.add_argument("--definition")
    ap.add_argument("--results", required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--now", required=True, help="evaluation timestamp (UTC ISO-8601), recorded only in run records")
    ap.add_argument("--summary", help="optional file to append a markdown status line to (e.g. $GITHUB_STEP_SUMMARY)")
    a = ap.parse_args(argv)
    status, rec = evaluate(a.forward_store, a.prices, a.oi_funding, a.results, a.run_id, a.now, a.definition)
    line = (f"EXP-23 forward evaluation: **{status}**"
            + (f" ({rec.get('evaluation_status')}, checkpoint {rec['checkpoint']['evaluable_events']}/"
               f"{ew.CHECKPOINT_TOTAL})" if status == "EVALUATED" else "")
            + (f" — {rec.get('detail', '').splitlines()[0]}" if rec.get("detail") else ""))
    print(line)
    print(json.dumps({k: rec.get(k) for k in ("status", "evaluation_status", "dataset_fingerprint", "eligible_hours",
                                              "labelled_hours", "episodes", "burn_in_complete", "coverage",
                                              "checkpoint", "blockers", "problems")}, indent=1, sort_keys=True))
    if a.summary:
        with open(a.summary, "a") as f:
            f.write(line + "\n")
            if os.path.isfile(os.path.join(a.results, "latest_progress.md")) and status == "EVALUATED":
                f.write("\n" + open(os.path.join(a.results, "latest_progress.md")).read())
    return EXIT[status]


if __name__ == "__main__":
    sys.exit(main())
