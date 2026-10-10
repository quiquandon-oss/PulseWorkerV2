"""Tests for research/exp23_forward_eval.py and its workflow files. Synthetic data only; no network."""
import gzip
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import early_warning_12h as ew  # noqa: E402
import exp23_forward_eval as fe  # noqa: E402
from test_early_warning_12h import flat_prices, funding_series, oi_series  # noqa: E402

H = ew.H
ROOT = Path(__file__).resolve().parent.parent
STUB = ROOT / "research" / "scheduler" / "exp23-forward-evaluation.yml"
VERIFY = ROOT / ".github" / "workflows" / "exp23-forward-evaluation-verify.yml"
DATA_BRANCH = "research-data/exp23-evaluation"


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write_partition(store, source, day, rows, index):
    d = store / source / "2026"
    d.mkdir(parents=True, exist_ok=True)
    data = d / f"{day}.jsonl.gz"
    with gzip.GzipFile(data, "wb", mtime=0) as f:
        f.write("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows).encode())
    meta = {"partition": day, "file_sha256": sha(data), "completeness": "COMPLETE"}
    (d / f"{day}.meta.json").write_text(json.dumps(meta))
    index.setdefault(source, {"files": {}})["files"][day] = meta["file_sha256"]
    (store / "index.json").write_text(json.dumps({"sources": index}))
    return data


def price_rows(series, lo, hi):
    out = []
    for t, v in sorted(series.items()):
        if lo <= t <= hi:
            for metric in ("close", "volume"):
                out.append({"instrument": "BTC", "metric": metric, "timestamp": t - H, "available_at": t,
                            "value": float(v[3]), "raw": {"t": t - H, "T": t - 1, "o": str(v[0]), "h": str(v[1]),
                                                          "l": str(v[2]), "c": str(v[3])}})
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Pinned inputs (synthetic, so the pins are patched to their hashes) and a forward store with one partition."""
    series = flat_prices(ew.SEAL_END - 800 * H, ew.SEAL_END + 60 * H)
    prices = tmp_path / "BTC.jsonl"
    prices.write_text("".join(json.dumps({"open_ts": t - H, "close_ts": t - 1, "available_at": t, "o": str(v[0]),
                                          "h": str(v[1]), "l": str(v[2]), "c": str(v[3])}) + "\n"
                              for t, v in sorted(series.items()) if t <= ew.SEAL_END))
    oi = tmp_path / "oi.jsonl.gz"
    with gzip.GzipFile(oi, "wb", mtime=0) as f:
        lines = []
        for ts, (av, b, u) in sorted(oi_series(ew.SEAL_END - 800 * H, ew.SEAL_END + 60 * H).items()):
            for m, v in (("open_interest", b), ("open_interest_usd", u)):
                lines.append({"source": "binance_oi_archive", "metric": m, "timestamp": ts, "available_at": av, "value": v})
        for ts, (av, v) in sorted(funding_series(ew.SEAL_END - 900 * H, ew.SEAL_END - 30 * H).items()):
            lines.append({"source": "binance_funding_archive", "metric": "funding_rate", "timestamp": ts,
                          "available_at": av, "value": v})
        f.write("".join(json.dumps(r) + "\n" for r in lines).encode())
    monkeypatch.setattr(fe, "PRICES_SHA256", sha(prices))
    monkeypatch.setattr(fe, "FROZEN_OI_SHA256", sha(oi))
    store, index = tmp_path / "store", {}
    store.mkdir()
    write_partition(store, "hyperliquid_hip3", "2026-10-09", price_rows(series, ew.SEAL_END - 5 * H, ew.SEAL_END + 3 * H), index)
    return {"series": series, "prices": prices, "oi": oi, "store": store, "index": index, "results": tmp_path / "res",
            "tmp": tmp_path}


def run(env, run_id="r1", now="2026-10-11T12:10:00Z", results=None):
    return fe.evaluate(str(env["store"]), str(env["prices"]), str(env["oi"]), str(results or env["results"]), run_id, now)


def snapshot(d):
    return {str(p.relative_to(d)): p.read_bytes() for p in sorted(Path(d).rglob("*")) if p.is_file()}


# ------------------------------------------------------------------------------- new data / no new data / duplicates
def test_new_data_then_no_new_data_then_new_partition(env):
    status, rec = run(env)
    assert status == "EVALUATED" and rec["evaluation_status"] == "INSUFFICIENT_SAMPLE"
    assert rec["coverage"]["F1"] == 3 and rec["coverage"]["F2"] == 3 and rec["coverage"]["F3"] == 0
    before = snapshot(env["results"])
    status2, rec2 = run(env, run_id="r2", now="2026-10-12T12:10:00Z")
    assert status2 == "NO_NEW_DATA" and snapshot(env["results"]) == before                    # nothing written
    write_partition(env["store"], "hyperliquid_hip3", "2026-10-10",
                    price_rows(env["series"], ew.SEAL_END + 4 * H, ew.SEAL_END + 27 * H), env["index"])
    status3, rec3 = run(env, run_id="r3", now="2026-10-13T12:10:00Z")
    assert status3 == "EVALUATED" and rec3["dataset_fingerprint"] != rec["dataset_fingerprint"]
    assert rec3["eligible_hours"] == 27
    state = json.loads((env["results"] / "state.json").read_text())
    assert [h["status"] for h in state["history"]] == ["EVALUATED", "EVALUATED"]           # NO_NEW_DATA not recorded


def test_report_is_deterministic_and_timestamp_only_in_run_record(env):
    s1, r1 = run(env, results=env["tmp"] / "a", now="2026-10-11T12:10:00Z")
    s2, r2 = run(env, results=env["tmp"] / "b", now="2026-10-11T18:00:00Z")
    rep = f"reports/{r1['dataset_fingerprint'][:16]}/report.json"
    assert (env["tmp"] / "a" / rep).read_bytes() == (env["tmp"] / "b" / rep).read_bytes()
    assert "2026-10-11T12:10:00Z" not in (env["tmp"] / "a" / rep).read_text()
    assert json.loads(next((env["tmp"] / "a" / "runs").iterdir()).read_text())["evaluated_at"] == "2026-10-11T12:10:00Z"


# ------------------------------------------------------------------------------------ integrity and malformed input
def test_checksum_mismatch_excludes_partition_and_fails_without_price_data(env):
    data = env["store"] / "hyperliquid_hip3" / "2026" / "2026-10-09.jsonl.gz"
    data.write_bytes(data.read_bytes() + b"x")
    status, rec = run(env)
    assert status == "INTEGRITY_FAILURE" and fe.EXIT[status] == 3
    assert rec["problems"][0]["code"] == "CHECKSUM_MISMATCH"


def test_malformed_and_duplicate_rows_reject_their_partition(env):
    good = price_rows(env["series"], ew.SEAL_END + 4 * H, ew.SEAL_END + 6 * H)
    write_partition(env["store"], "binance_oi_archive", "2026-10-10", [{"metric": "open_interest"}], env["index"])
    write_partition(env["store"], "hyperliquid_hip3", "2026-10-10", good + good[:1], env["index"])
    status, rec = run(env)
    codes = {p["path"].split("/")[0]: p["code"] for p in rec["problems"]}
    assert status == "EVALUATED" and codes == {"binance_oi_archive": "MALFORMED_ROW", "hyperliquid_hip3": "DUPLICATE_ROW"}
    assert rec["eligible_hours"] == 3                       # rejected partition contributes nothing (nothing filled)


def test_pin_mismatch_and_missing_inputs(env, monkeypatch):
    monkeypatch.setattr(fe, "PRICES_SHA256", "0" * 64)
    assert run(env)[0] == "INTEGRITY_FAILURE"
    monkeypatch.setattr(fe, "PRICES_SHA256", sha(env["prices"]))
    (env["store"] / "index.json").unlink()
    assert run(env, run_id="r2")[0] == "INPUT_UNAVAILABLE"
    status, rec = fe.evaluate(str(env["store"]), str(env["tmp"] / "missing.jsonl"), str(env["oi"]),
                              str(env["results"]), "r3", "2026-10-11T12:10:00Z")
    assert status == "INPUT_UNAVAILABLE" and fe.EXIT[status] == 2


# ------------------------------------------------------------------------------------------- recovery after failure
def test_failed_runs_are_recorded_and_the_next_run_recovers(env, monkeypatch):
    data = env["store"] / "hyperliquid_hip3" / "2026" / "2026-10-09.jsonl.gz"
    good = data.read_bytes()
    data.write_bytes(good[:-3])
    assert run(env, run_id="bad1")[0] == "INTEGRITY_FAILURE"
    state = json.loads((env["results"] / "state.json").read_text())
    assert state["last_status"] == "INTEGRITY_FAILURE" and state["last_evaluated_fingerprint"] is None
    data.write_bytes(good)
    real = ew.run
    monkeypatch.setattr(fe.ew, "run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    status, rec = run(env, run_id="bad2", now="2026-10-11T13:00:00Z")
    assert status == "EVALUATION_FAILED" and fe.EXIT[status] == 4 and "boom" in rec["detail"]
    assert json.loads((env["results"] / "state.json").read_text())["last_evaluated_fingerprint"] is None
    monkeypatch.setattr(fe.ew, "run", real)
    status, rec = run(env, run_id="ok", now="2026-10-11T14:00:00Z")
    assert status == "EVALUATED"
    hist = [h["status"] for h in json.loads((env["results"] / "state.json").read_text())["history"]]
    assert hist == ["INTEGRITY_FAILURE", "EVALUATION_FAILED", "EVALUATED"]
    assert len(list((env["results"] / "runs").iterdir())) == 3


# --------------------------------------------------------------------- sealed boundary, look-ahead, funding, gating
def test_sealed_rows_in_the_forward_store_never_become_labels(env):
    # A +10% move inside the sealed period, delivered through the forward store, must not create a label.
    rows = price_rows(env["series"], ew.SEAL_END - 30 * H, ew.SEAL_END + 3 * H)
    for r in rows:
        if r["available_at"] <= ew.SEAL_END - 5 * H:
            r["value"] = 110.0
            r["raw"]["c"] = r["raw"]["o"] = r["raw"]["h"] = r["raw"]["l"] = "110"
    write_partition(env["store"], "hyperliquid_hip3", "2026-10-09", rows, env["index"])
    status, rec = run(env)
    rep = json.loads((env["results"] / rec["report"] / "report.json").read_text())
    assert rep["labels"]["episodes"] == 0 and rep["labels"]["burn_in_crossings"] == 0
    assert rep["labels"]["first_labelable"] == ew.SEAL_END + 26 * H
    assert rep["boundary"]["SEAL_END_inclusive"] == ew.SEAL_END and rep["boundary"]["first_decision_T1"] == ew.T1
    # sealed-period prices disagree with the pinned snapshot -> conflict hours dropped, not chosen
    assert rep["data"]["conflicts_dropped"]


def test_missing_funding_keeps_f1_f2_and_blocks_f3_f4_and_counts_only(env):
    status, rec = run(env)
    rep = json.loads((env["results"] / rec["report"] / "report.json").read_text())
    assert any(b.startswith("funding:") for b in rep["blockers"])
    comps = rep["results_counts"]["comparisons"]
    assert comps["F2_vs_F1"]["scored_hours"] == 3 and comps["F3_vs_F1"]["scored_hours"] == 0
    assert rep["status"] == "INSUFFICIENT_SAMPLE" and rep["metrics"] is None
    body = json.dumps(rep)
    assert "p_one_sided" not in body and "wilson95" not in body and "mcnemar_p" not in body
    md = (env["results"] / "latest_progress.md").read_text()
    assert "No performance metric" in md and "INSUFFICIENT_SAMPLE" in md


def test_features_in_report_never_use_future_rows(env):
    # Future rows (available after every decision hour) present in the store cannot change the report's features.
    s1, r1 = run(env, results=env["tmp"] / "a")
    future = price_rows(env["series"], ew.SEAL_END + 40 * H, ew.SEAL_END + 41 * H)
    for r in future:
        r["available_at"] += 10_000 * H
        r["timestamp"] += 10_000 * H
    write_partition(env["store"], "binance_oi_archive", "2026-11-30",
                    [{"source": "binance_oi_archive", "metric": "open_interest", "timestamp": ew.T1,
                      "available_at": ew.T1 + 400 * H, "value": 1.0}], env["index"])
    s2, r2 = run(env, results=env["tmp"] / "b")
    a = json.loads((env["tmp"] / "a" / r1["report"] / "report.json").read_text())
    b = json.loads((env["tmp"] / "b" / r2["report"] / "report.json").read_text())
    assert a["decision_hours"] == b["decision_hours"]


def test_cli_exit_codes_and_summary(env, tmp_path):
    summary = tmp_path / "summary.md"
    args = ["--forward-store", str(env["store"]), "--prices", str(env["prices"]), "--oi-funding", str(env["oi"]),
            "--results", str(env["results"]), "--run-id", "c1", "--now", "2026-10-11T12:10:00Z", "--summary", str(summary)]
    assert fe.main(args) == 0 and "EVALUATED" in summary.read_text()
    assert fe.main(args[:-6] + ["--run-id", "c2", "--now", "2026-10-11T13:00:00Z"]) == 0
    assert fe.main(["--forward-store", str(tmp_path / "nope")] + args[2:]) == 2


# ----------------------------------------------------------------------------------------- workflow isolation
def load(p):
    import yaml
    doc = yaml.safe_load(p.read_text())
    doc["on"] = doc.get("on", doc.get(True))
    return doc


def _common_isolation(p):
    wf = load(p)
    assert wf["permissions"] == {"contents": "write"}
    assert wf["concurrency"] == {"group": "exp23-forward-evaluation", "cancel-in-progress": False}
    body = "\n".join(line.split("#", 1)[0] for line in p.read_text().splitlines())
    assert "secrets." not in body and "wrangler" not in body and "CLOUDFLARE" not in body and "d1" not in body.lower()
    steps = wf["jobs"]["evaluate"]["steps"]
    uses = [s for s in steps if "uses" in s]
    assert all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", s["uses"]) for s in uses)
    assert all(s["with"]["persist-credentials"] is False for s in uses)
    refs = {s["with"]["path"]: s["with"]["ref"] for s in uses}
    assert refs["fwd"] == "research-data/risk-regime-forward" and refs["data"] == DATA_BRANCH
    assert refs["mm"] == "0eb5be03764aede986d6b696fbc2c619e27971f1"
    assert refs["frozen"] == "b5bcff8f924dcbd255b73ca94cb5bb743e461c40"
    assert refs["def"] == "196b87e4d9b95a0136d0c7249251b0934a8eab42"
    tok = [s for s in steps if "github.token" in str(s.get("env", {}))]
    assert len(tok) == 1 and "github.token" not in "".join(str(s) for s in steps if s is not tok[0])
    push = tok[0]["run"]
    assert push.count("git push") == 1 and 'HEAD:refs/heads/${TARGET}' in push and tok[0]["env"]["TARGET"] == DATA_BRANCH
    assert "grep -v '^exp23_evaluation/'" in push and "git add -A exp23_evaluation" in push
    ev = [s for s in steps if s.get("id") == "evaluate"][0]
    assert "exp23_forward_eval.py" in ev["run"] and "env" not in ev
    return wf, refs


def test_schedule_stub_is_inert_and_isolated():
    wf, refs = _common_isolation(STUB)
    assert set(wf["on"]) == {"schedule"} and wf["on"]["schedule"] == [{"cron": "10 12 * * *"}]
    assert not (ROOT / ".github" / "workflows" / "exp23-forward-evaluation.yml").exists()        # not installed
    assert refs["code"] == "EXP23_EVAL_CODE_SHA"                                                  # needs a reviewed SHA
    fwd = ROOT / ".github" / "workflows" / "research-forward-schedule.yml"
    if fwd.exists():
        assert load(fwd)["on"]["schedule"] == [{"cron": "40 10 * * *"}]                           # runs 90 min later


def test_verify_workflow_runs_only_on_its_branch_and_never_on_a_schedule():
    wf, refs = _common_isolation(VERIFY)
    assert set(wf["on"]) == {"push"} and wf["on"]["push"]["branches"] == ["claude/epic-planck-uyapsw-exp23-eval"]
    assert refs["code"] == "${{ github.sha }}"
    deploy = load(ROOT / ".github" / "workflows" / "deploy.yml")
    assert deploy["on"]["push"]["branches"] == ["main"]                                          # no deploy path
