"""Seal-safe structural check of the stored EXP-009 / EXP-010 batches (owner decision H1, 2026-10-31).

H1 allows structural checks only for EXP-009 and EXP-010: schema, duplicates, labels and temporal integrity, with no
predictive conclusion. A check that cannot be isolated from sealed outcomes stays blocked. So this module:

- reads only identity, timestamp and label fields, and never reads a return, residual or other numeric outcome field;
- emits only pass/fail per invariant and per batch, plus counts of batches. It never emits a value, count or
  distribution of any label or outcome-derived field;
- does NOT check that every label is reachable on stored batches, because a label's presence in a window that
  overlaps the seal (2026-07-22 to 2026-10-09T21:00Z) is outcome-derived. Reachability is established by the
  modules' own synthetic tests, and is checked on stored data only for fully post-seal windows.

Milestone (both registry rows): 4 independent weekly batches with well-formed output. "Independent" here means
analysis times at least 6 days apart, the weekly cadence; the 90-day windows still overlap each other, so this is
structural independence (separate runs), not statistical independence.

  python3 research/structural_check_exp009_010.py --from-d1     # GitHub Actions only (needs the D1 token)
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SEAL_START_TS = 1784678400000          # 2026-07-22T00:00:00Z
SEAL_END_TS = 1791579600000            # 2026-10-09T21:00:00Z
MAX_WINDOW_MS = 92 * 24 * 3600000      # 90-day windows plus slack
MIN_GAP_MS = 6 * 24 * 3600000
MILESTONE_BATCHES = 4

SUBJECTS = {"EXP-009": "EXP-009:event_source_evidence", "EXP-010": "EXP-010:source_dialogue_validation"}

EXP009_INTERPRETATIONS = (
    "EVENT_SOURCE_ALIGNED", "EVENT_SOURCE_NOT_ALIGNED", "EVENT_SOURCE_REDUNDANT_POSSIBLE",
    "EVENT_SOURCE_INFORMATIONAL_ONLY", "EVENT_SOURCE_MISLEADING_POSSIBLE", "INSUFFICIENT_EVIDENCE",
)
EXP009_KEYS = ("window", "events", "results", "source_keys", "evidence_coverage", "by_source_interpretation",
               "btc_outcome_coverage")
EXP010_KEYS = ("window", "source_keys", "relationship_summary", "redundancy_summary")
EXP010_RELATIONSHIP_LABELS = ("INSUFFICIENT_EVIDENCE", "DIFFERENT_TIMING", "SUPPORTING", "CONTRADICTING")
EXP010_REDUNDANCY_LABELS = ("REDUNDANCY_UNRESOLVED", "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED", "INSUFFICIENT_EVIDENCE")


def _common(row, report, failures):
    a, s, e = int(row["analysis_ts"]), int(row["window_start_ts"]), int(row["window_end_ts"])
    if not (s < e):
        failures.append("window_start_before_end")
    if e != a:
        failures.append("window_end_equals_analysis_ts")
    if e - s > MAX_WINDOW_MS:
        failures.append("window_at_most_90_days")
    w = report.get("window") or {}
    if w.get("start_ts") is None or w.get("end_ts") is None or not (int(w["start_ts"]) >= s and int(w["end_ts"]) <= e):
        failures.append("report_window_inside_row_window")
    if not row.get("validation_status"):
        failures.append("validation_status_present")


def check_exp009_batch(row):
    failures = []
    try:
        report = json.loads(row["metric_json"])
    except (TypeError, ValueError):
        return ["metric_json_parses"]
    for k in EXP009_KEYS:
        if k not in report:
            failures.append(f"key_present:{k}")
    if failures:
        return failures
    _common(row, report, failures)
    results, events = report["results"], report["events"]
    if int(row["sample_size"]) != len(results):
        failures.append("sample_size_equals_len_results")
    event_ids = {ev.get("event_id") for ev in events}
    sources = set(report["source_keys"])
    lo, hi = int(report["window"]["start_ts"]), int(report["window"]["end_ts"])
    seen, dup = set(), False
    bad = {"identity_fields_present": False, "event_in_window": False, "event_listed": False, "source_listed": False,
           "label_in_declared_set": False, "no_internal_model_rows": False}
    for r in results:                      # identity, time and label fields only
        eid, ets, sk, lab = r.get("event_id"), r.get("event_ts"), r.get("source_key"), r.get("event_source_interpretation")
        if eid is None or ets is None or sk is None:
            bad["identity_fields_present"] = True
            continue
        if (eid, sk) in seen:
            dup = True
        seen.add((eid, sk))
        if not (lo <= int(ets) <= hi):
            bad["event_in_window"] = True
        if eid not in event_ids:
            bad["event_listed"] = True
        if sk not in sources:
            bad["source_listed"] = True
        if lab not in EXP009_INTERPRETATIONS:
            bad["label_in_declared_set"] = True
        if r.get("is_internal_model_event") is not False:
            bad["no_internal_model_rows"] = True
    if dup:
        failures.append("no_duplicate_event_source_rows")
    failures += [k for k, v in bad.items() if v]
    return failures


def check_exp010_batch(row):
    failures = []
    try:
        report = json.loads(row["metric_json"])
    except (TypeError, ValueError):
        return ["metric_json_parses"]
    for k in EXP010_KEYS:
        if k not in report:
            failures.append(f"key_present:{k}")
    if failures:
        return failures
    _common(row, report, failures)
    rel, red = report["relationship_summary"], report["redundancy_summary"]
    by_rel, by_red = rel.get("by_relationship") or {}, red.get("by_redundancy") or {}
    if int(row["sample_size"]) != rel.get("n_source_pair_observations"):
        failures.append("sample_size_equals_n_source_pair_observations")
    if set(by_rel) != set(EXP010_RELATIONSHIP_LABELS):
        failures.append("relationship_labels_equal_declared_set")
    elif sum(by_rel.values()) != rel.get("n_source_pair_observations"):
        failures.append("relationship_counts_sum_to_n")
    if set(by_red) != set(EXP010_REDUNDANCY_LABELS):
        failures.append("redundancy_labels_equal_declared_set")
    elif sum(by_red.values()) != red.get("n_source_pairs"):
        failures.append("redundancy_counts_sum_to_n")
    return failures


CHECKERS = {"EXP-009": check_exp009_batch, "EXP-010": check_exp010_batch}


def check_subject(exp_id, rows):
    """Pass/fail only: no label, value or count of any outcome-derived field leaves this function."""
    batches = []
    for row in sorted(rows, key=lambda r: int(r["analysis_ts"])):
        f = CHECKERS[exp_id](row)
        batches.append({"analysis_ts": int(row["analysis_ts"]), "valid": not f, "failed_invariants": sorted(set(f)),
                        "fully_post_seal": int(row["window_start_ts"]) >= SEAL_END_TS})
    independent, last = [], None
    for b in batches:
        if b["valid"] and (last is None or b["analysis_ts"] - last >= MIN_GAP_MS):
            independent.append(b["analysis_ts"])
            last = b["analysis_ts"]
    met = len(independent) >= MILESTONE_BATCHES
    return {
        "experiment_id": exp_id, "check": "structural-v1 (owner decision H1)", "n_batches": len(batches),
        "n_valid": sum(b["valid"] for b in batches), "n_independent_valid": len(independent),
        "n_fully_post_seal": sum(b["fully_post_seal"] for b in batches), "batches": batches,
        "structural_milestone": "MET" if met else "NOT_MET",
        "label_reachability": "NOT_CHECKED_ON_SEAL_OVERLAPPING_BATCHES (covered by the modules' synthetic tests)",
        "predictive_conclusion": None,
    }


def _fetch(subject):
    sql = ("SELECT analysis_ts, window_start_ts, window_end_ts, sample_size, validation_status, metric_json "
           f"FROM research_analyses WHERE subject = '{subject}' ORDER BY analysis_ts")
    out = subprocess.run(["wrangler", "d1", "execute", "sentiment-history", "--remote", "--json", "--command", sql],
                         capture_output=True, text=True, check=True)
    parsed = json.loads(out.stdout)
    return parsed[0]["results"] if parsed and parsed[0].get("results") is not None else []


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-d1", action="store_true", required=True)
    ap.parse_args(argv)
    report = {k: check_subject(k, _fetch(v)) for k, v in SUBJECTS.items()}
    print(json.dumps(report, indent=1))
    return 0 if all(r["n_valid"] == r["n_batches"] for r in report.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
