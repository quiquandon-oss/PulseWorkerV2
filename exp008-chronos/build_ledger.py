"""
EXP-008 derived ledger: one JSON file that merges every stored Chronos-2 forecast with its resolution, so a
browser (the CryptoPulseV2 Forecasting Lab) can read the experiment with one request instead of listing and
fetching every file.

DERIVED AND REBUILT ON EVERY RUN. The source of truth stays the immutable files under forecasts/, resolutions/
and runs/; they are only read here, never modified. The ledger copies their fields verbatim (no value is
recomputed, rounded or inferred), so every ledger row is traceable to its forecast file (`forecast_file`) and,
when resolved, its resolution file (`resolution_file`).

Sealed period: forecasts whose origin (input_end_ts) is before 2026-10-09T21:00Z are left out, and so are
their outcomes (research/plans/2026-11-01-prospective-evaluation-plans.md: no outcome before the seal end is
read). The store has none today; the rule keeps it that way.

Usage: python exp008-chronos/build_ledger.py --store <data checkout>/exp008_chronos
Writes <store>/derived/ledger.json. Exit code 0 even with unreadable files: they are listed in `skipped`.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

FORMAT = "exp008-chronos-ledger-v1"
SEAL_END = int(datetime(2026, 10, 9, 21, tzinfo=timezone.utc).timestamp() * 1000)
PROSPECTIVE_START = int(datetime(2026, 11, 1, tzinfo=timezone.utc).timestamp() * 1000)

# Copied verbatim from each forecast file (missing keys stay absent).
FORECAST_FIELDS = ("id", "coin", "horizon_hours", "issued_ts", "input_end_ts", "target_ts", "price_at_prediction",
                   "forecast_median", "forecast_q10", "forecast_q90", "quantile_levels", "predicted_return_pct",
                   "direction", "last_tick_return_pct", "persistence_direction", "model_id", "model_version",
                   "model_revision", "model_revision_resolved", "context_length", "forecast_horizon_steps",
                   "context_sha256", "code_sha", "inference_backend")
# Copied verbatim from each resolution file.
RESOLUTION_FIELDS = ("actual_price", "actual_return_pct", "actual_direction", "correct", "persistence_correct",
                     "absolute_error", "signed_error", "resolution_price_ts", "resolved_ts")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(store, now_ms=None):
    store = Path(store)
    now_ms = int(now_ms if now_ms is not None else datetime.now(timezone.utc).timestamp() * 1000)
    rows, skipped, sealed = [], [], 0
    for fpath in sorted(store.glob("forecasts/*/*/*.json")):
        rel = fpath.relative_to(store).as_posix()
        try:
            fc = _read(fpath)
        except (OSError, ValueError) as e:
            skipped.append({"file": rel, "reason": f"unreadable: {type(e).__name__}"})
            continue
        if not isinstance(fc, dict) or not isinstance(fc.get("input_end_ts"), (int, float)):
            skipped.append({"file": rel, "reason": "no numeric input_end_ts"})
            continue
        if fc["input_end_ts"] < SEAL_END:
            sealed += 1
            continue
        row = {k: fc[k] for k in FORECAST_FIELDS if k in fc}
        anchor = fc.get("anchor") or {}
        row["timesfm_ts"] = anchor.get("timesfm_ts")
        row["forecast_file"] = rel
        row["population"] = "prospective" if fc["input_end_ts"] >= PROSPECTIVE_START else "post_seal_descriptive"
        rpath = store / "resolutions" / fpath.relative_to(store / "forecasts")
        if rpath.exists():
            try:
                res = _read(rpath)
                row.update({k: res[k] for k in RESOLUTION_FIELDS if k in res})
                row["resolution_file"] = rpath.relative_to(store).as_posix()
                row["status"] = "resolved"
            except (OSError, ValueError) as e:
                skipped.append({"file": rpath.relative_to(store).as_posix(), "reason": f"unreadable: {type(e).__name__}"})
                row["status"] = "resolution_unreadable"
        else:
            row["status"] = "pending"
        rows.append(row)
    rows.sort(key=lambda r: (r.get("horizon_hours") or 0, r["input_end_ts"]))
    runs = []
    for rpath in sorted(store.glob("runs/*.json"))[-20:]:
        try:
            r = _read(rpath)
        except (OSError, ValueError):
            continue
        runs.append({"file": rpath.relative_to(store).as_posix(), "run_ts": r.get("run_ts"), "run_id": r.get("run_id"),
                     "forecasts_made": len(r.get("forecasts") or []), "resolved": r.get("resolved"),
                     "pending": r.get("pending"), "errors": len(r.get("errors") or []), "code_sha": r.get("code_sha")})
    return {"format": FORMAT, "generated_ts": now_ms, "derived": True,
            "source": "research-data/exp008-chronos: forecasts/, resolutions/, runs/ (immutable; this file is rebuilt)",
            "seal_end_ts": SEAL_END, "prospective_start_ts": PROSPECTIVE_START, "excluded_sealed": sealed,
            "skipped": skipped, "forecasts": rows, "recent_runs": runs}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", required=True)
    args = ap.parse_args(argv)
    ledger = build(args.store)
    out = Path(args.store) / "derived" / "ledger.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(out)
    print(f"ledger: {len(ledger['forecasts'])} forecasts, {len(ledger['skipped'])} skipped, "
          f"{ledger['excluded_sealed']} sealed -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
