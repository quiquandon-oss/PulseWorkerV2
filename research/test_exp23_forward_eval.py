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


def test_evaluator_pins_the_stdlib_backend(env):
    status, rec = run(env)
    rep = json.loads((env["results"] / rec["report"] / "report.json").read_text())
    assert rep["fit_backend"] == "stdlib"


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


def _code_lines(p_or_text):
    text = p_or_text if isinstance(p_or_text, str) else p_or_text.read_text()
    text = text[text.index("name: exp23-forward-evaluation"):]                                    # header may differ
    return [line.split("#", 1)[0].rstrip() for line in text.splitlines()]


def assert_installed_copy_matches_template(installed):
    wf, refs = _common_isolation(installed)
    assert set(wf["on"]) == {"schedule"} and wf["on"]["schedule"] == [{"cron": "10 12 * * *"}]
    assert re.fullmatch(r"[0-9a-f]{40}", refs["code"])                                            # immutable pin
    pinned = STUB.read_text().replace("ref: EXP23_EVAL_CODE_SHA", f"ref: {refs['code']}", 1)
    assert _code_lines(installed) == _code_lines(pinned)                                          # nothing else differs


def test_installed_copy_check_accepts_a_pinned_copy_and_rejects_drift(tmp_path):
    sha = "111eea180f897ecf5eff2df2316e45c842ef00d6"
    body = STUB.read_text()
    good = tmp_path / "good.yml"
    good.write_text("# header\n" + body.replace("ref: EXP23_EVAL_CODE_SHA", f"ref: {sha}   # pinned", 1))
    assert_installed_copy_matches_template(good)
    for bad_body in (body,                                                                        # unpinned
                     body.replace("ref: EXP23_EVAL_CODE_SHA", "ref: main", 1),                    # floating ref
                     body.replace("ref: EXP23_EVAL_CODE_SHA", f"ref: {sha}", 1)
                         .replace("cron: '10 12 * * *'", "cron: '10 * * * *'"),                  # other schedule
                     body.replace("ref: EXP23_EVAL_CODE_SHA", f"ref: {sha}", 1)
                         .replace("timeout-minutes: 20", "timeout-minutes: 30")):                 # any other drift
        bad = tmp_path / "bad.yml"
        bad.write_text(bad_body)
        with pytest.raises(AssertionError):
            assert_installed_copy_matches_template(bad)


def test_schedule_stub_is_inert_and_isolated():
    wf, refs = _common_isolation(STUB)
    assert set(wf["on"]) == {"schedule"} and wf["on"]["schedule"] == [{"cron": "10 12 * * *"}]
    assert refs["code"] == "EXP23_EVAL_CODE_SHA"                                                  # template: no SHA
    installed = ROOT / ".github" / "workflows" / "exp23-forward-evaluation.yml"                   # lives on main only
    if installed.exists():
        assert_installed_copy_matches_template(installed)
    fwd = ROOT / ".github" / "workflows" / "research-forward-schedule.yml"
    if fwd.exists():
        assert load(fwd)["on"]["schedule"] == [{"cron": "40 10 * * *"}]                           # runs 90 min later


def test_verify_workflow_runs_only_on_its_branch_and_never_on_a_schedule():
    wf, refs = _common_isolation(VERIFY)
    assert set(wf["on"]) == {"push"} and wf["on"]["push"]["branches"] == ["claude/epic-planck-uyapsw-exp23-eval"]
    assert refs["code"] == "${{ github.sha }}"
    deploy = load(ROOT / ".github" / "workflows" / "deploy.yml")
    assert deploy["on"]["push"]["branches"] == ["main"]                                          # no deploy path


# ------------------------------------------- v0.5.0 source variants (F3_HL / F4_HL / F3_OKX / F4_OKX); TEST data only
def okx_rows(lo, hi, lag=5 * 60_000, step=8 * H, rate=lambda ts: 1e-4 * (1 + (ts // (8 * H)) % 3), observed=None):
    out = []
    for ts in range(lo - lo % step, hi + 1, step):
        out.append({"source": "okx_btc_funding", "instrument": "BTC-USDT-SWAP", "metric": "funding_rate", "timestamp": ts,
                    "available_at": ts, "value": rate(ts), "retrieved_at": ts, "first_observed_at": (observed or (lambda t: t + lag))(ts)})
    return out


def hl_rows(lo, hi, lag=5 * 60_000, observed=None):
    return [dict(r, source="hyperliquid_btc_funding", instrument="BTC") for r in okx_rows(lo, hi, lag, step=H, observed=observed)]


def series_of(rows, key):
    return {r["timestamp"]: (ew.causal_available_at(r), r["value"]) for r in rows
            if (r["source"], r["metric"]) == (ew.SOURCE_FUNDING[key]["source"], "funding_rate")}


def test_causal_availability_rules():
    base = {"timestamp": 100, "available_at": 100}
    assert ew.causal_available_at(dict(base, first_observed_at=500, retrieved_at=200)) == 500       # observation wins
    assert ew.causal_available_at(dict(base, retrieved_at=200)) == 200 + ew.LEGACY_OBSERVATION_MARGIN_MS   # legacy bound
    assert ew.causal_available_at(dict(base, first_observed_at=50)) == 100                           # never before settlement
    assert ew.causal_available_at(dict(base)) is None                                                # no observation -> non-causal


def test_settled_before_t_but_observed_after_t_cannot_affect_features_at_t():
    S = ew.T1 + 40 * 24 * H
    S -= S % (8 * H)                                                 # a settlement time
    T = S + 60_000                                                   # decision 1 min after it
    rows = okx_rows(S - 35 * 24 * H, S - 8 * H)                      # observed 5 min after settlement
    fb = ew.FeatureBuilder({}, None, None)
    ref = fb.source_funding_features("OKX", series_of(rows, "OKX"), T, [])
    assert ref is not None                                           # S-8h is fresh (8h01 <= 8h15) at T
    late = rows + okx_rows(S, S, observed=lambda ts: T + 1)          # settled before T, observed after T
    used = []
    got = fb.source_funding_features("OKX", series_of(late, "OKX"), T, used)
    assert got == ref and max(used) <= T                             # the late row is invisible at T
    assert fb.source_funding_features("OKX", series_of(late, "OKX"), T + 1, [])["fund"] == late[-1]["value"]


def test_future_available_records_cannot_alter_earlier_hours(env):
    s = env["series"]
    oi = ew.load_venue_series([str(env["oi"])])[0]
    lo, hi = ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H
    cut = ew.SEAL_END + 30 * H
    base = {"OKX": series_of(okx_rows(lo, cut), "OKX"), "HL": series_of(hl_rows(lo, cut), "HL")}
    extra = {"OKX": series_of(okx_rows(lo, hi), "OKX"), "HL": series_of(hl_rows(lo, hi), "HL")}
    fb = ew.FeatureBuilder(s, oi, None)
    for t in [u for u in sorted(s) if ew.T1 <= u <= cut]:
        a, b = fb.build_sources(t, base), fb.build_sources(t, extra)
        assert a == b, t


def test_hl_and_okx_are_scored_independently_with_no_substitution(env):
    s = env["series"]
    oi = ew.load_venue_series([str(env["oi"])])[0]
    okx = series_of(okx_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H), "OKX")
    rep = ew.run_source_variants(s, oi, {"OKX": okx, "HL": {}})
    cov = rep["decision_hours"]["coverage"]
    assert cov["F3_OKX"]["evaluable_hours"] > 0 and cov["F4_OKX"]["evaluable_hours"] > 0
    assert cov["F3_HL"]["evaluable_hours"] == 0 and cov["F4_HL"]["evaluable_hours"] == 0       # OKX never stands in for HL
    assert any(b.startswith("U_HL: no causally usable hyperliquid_btc_funding") for b in rep["blockers"])
    assert rep["results_counts"]["comparisons"]["F3_OKX_vs_F1"]["incremental_vs_F1"]["status"] == "NOT_COMPUTED"
    hl = series_of(hl_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H), "HL")
    rep2 = ew.run_source_variants(s, oi, {"OKX": {}, "HL": hl})
    assert rep2["decision_hours"]["coverage"]["F3_HL"]["evaluable_hours"] > 0
    assert rep2["decision_hours"]["coverage"]["F3_OKX"]["evaluable_hours"] == 0
    assert rep["sources"]["HL"]["not_evaluable_hours"] == rep["decision_hours"]["eligible"]


def test_daily_collection_lag_keeps_variants_non_evaluable_with_an_explicit_blocker(env):
    """The real cadence: closed days collected at 10:40 the next day -> observed 11.7-34.7 h after settlement."""
    def daily(ts):
        day = ts - ts % (24 * H)
        return day + 24 * H + 10 * H + 40 * 60_000
    s = env["series"]
    rows = okx_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H, observed=daily) + \
        hl_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H, observed=daily)
    info = {k: ew.load_source_funding([], k)[1] for k in ew.SOURCE_FUNDING}
    rep = ew.run_source_variants(s, None, {"OKX": series_of(rows, "OKX"), "HL": series_of(rows, "HL")}, info)
    assert all(v["evaluable_hours"] == 0 for k, v in rep["decision_hours"]["coverage"].items() if k != "F1")
    assert any(b.startswith("U_OKX: not evaluable at") for b in rep["blockers"])
    assert any(b.startswith("U_HL: not evaluable at") for b in rep["blockers"])


def test_missing_or_conflicting_funding_rows_are_never_filled(tmp_path):
    rows = okx_rows(ew.T1, ew.T1 + 10 * 8 * H)
    rows[3] = dict(rows[3])
    dup = dict(rows[4], value=rows[4]["value"] + 1)                 # same settlement, another value
    noobs = {k: v for k, v in rows[5].items() if k not in ("retrieved_at", "first_observed_at")}
    p = tmp_path / "okx.jsonl.gz"
    with gzip.GzipFile(p, "wb", mtime=0) as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows[:5] + [dup, noobs] + rows[6:]).encode())
    series, info = ew.load_source_funding([str(p)], "OKX")
    assert rows[4]["timestamp"] not in series and rows[5]["timestamp"] not in series
    assert info["conflicts_dropped"] == 1 and info["non_causal_rows"] == 1 and info["settlements"] == len(rows) - 2


def write_funding_partition(env, source, day, rows):
    return write_partition(env["store"], source, day, rows, env["index"])


def test_evaluator_reports_source_variants_separately_and_keeps_the_v040_report(env, tmp_path):
    status, rec = run(env)                                           # no HL / OKX partitions at all
    rep0 = json.loads((env["results"] / rec["report"] / "report.json").read_text())
    sv0 = json.loads((env["results"] / rec["report"] / "source_variants.json").read_text())
    assert status == "EVALUATED" and rec["coverage"]["F1"] == 3                # price-only evaluation unaffected
    assert sv0["protocol"] == "v0.5.0" and sv0["spec_sha256"] == ew.SPEC_V050_SHA256
    assert len([b for b in sv0["blockers"] if "no causally usable" in b]) == 2
    rows = okx_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 3 * H)
    write_funding_partition(env, "okx_btc_funding", "2026-10-09", rows)
    status2, rec2 = run(env, run_id="r2", now="2026-10-12T12:10:00Z", results=tmp_path / "res2")
    assert status2 == "EVALUATED" and rec2["dataset_fingerprint"] != rec["dataset_fingerprint"]   # input hash included
    assert any(k.startswith("okx_btc_funding/") for k in rec2["inputs"])
    rep2 = json.loads((tmp_path / "res2" / rec2["report"] / "report.json").read_text())
    drop = ("inputs", "dataset_fingerprint", "excluded_inputs")
    assert {k: v for k, v in rep0.items() if k not in drop} == {k: v for k, v in rep2.items() if k not in drop}   # v0.4.0 identical
    sv2 = json.loads((tmp_path / "res2" / rec2["report"] / "source_variants.json").read_text())
    assert sv2["sources"]["OKX"]["settlements"] == len(rows) and sv2["sources"]["OKX"]["accepted_partitions"] == 1
    assert sv2["decision_hours"]["coverage"]["F3_HL"]["evaluable_hours"] == 0
    md = (tmp_path / "res2" / "latest_progress.md").read_text()
    assert "v0.5.0 source variants" in md and "| OKX |" in md


def test_tampered_okx_partition_is_excluded_not_used(env):
    data = write_funding_partition(env, "okx_btc_funding", "2026-10-09", okx_rows(ew.SEAL_END - 100 * H, ew.SEAL_END))
    with gzip.GzipFile(data, "wb", mtime=0) as f:
        f.write(b'{"metric": "funding_rate"}\n')
    status, rec = run(env)
    assert status == "EVALUATED" and any(p["code"] == "CHECKSUM_MISMATCH" and "okx_btc_funding" in p["path"] for p in rec["problems"])
    sv = json.loads((env["results"] / rec["report"] / "source_variants.json").read_text())
    assert sv["sources"]["OKX"]["settlements"] == 0


def test_missing_binance_funding_does_not_block_an_eligible_okx_variant(env):
    s = env["series"]
    oi = ew.load_venue_series([str(env["oi"])])[0]                 # OI only; Binance funding is not an input at all
    rep = ew.run_source_variants(s, oi, {"OKX": series_of(okx_rows(ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H), "OKX")})
    assert rep["decision_hours"]["coverage"]["F4_OKX"]["evaluable_hours"] > 0


def test_v050_spec_hash_and_v040_untouched():
    p = ROOT / "research" / "exp23" / "BTC_12H_EARLY_WARNING_SPEC_v0.5.0.md"
    assert sha(p) == ew.SPEC_V050_SHA256
    assert (ROOT / "research" / "exp23" / "BTC_12H_EARLY_WARNING_SPEC_v0.5.0.sha256").read_text().split()[0] == ew.SPEC_V050_SHA256
    assert sha(ROOT / "research" / "exp23" / "BTC_12H_EARLY_WARNING_SPEC_v0.4.0.md") == ew.SPEC_SHA256
    assert ew.FEATURE_SETS == {"F1": ("P",), "F2": ("P", "O"), "F3": ("P", "U"), "F4": ("P", "O", "U")}


# ------------------------------- freshness boundary under intraday observation (unchanged limits); TEST data only
def test_hl_freshness_boundary_needs_observation_within_one_hour_of_settlement():
    S = ew.T1 + 40 * 24 * H                                          # a settlement (top of an hour)
    t = S + H                                                        # the next decision hour: S is the latest usable
    hist = hl_rows(S - 31 * 24 * H, S - H)                           # 30-day history observed 5 min after settlement
    fb = ew.FeatureBuilder({}, None, None)
    ok = hist + hl_rows(S, S, observed=lambda ts: ts + H)            # observed exactly at t
    late = hist + hl_rows(S, S, observed=lambda ts: ts + H + 1)      # observed 1 ms after t
    assert fb.source_funding_features("HL", series_of(ok, "HL"), t, [])["fund"] == ok[-1]["value"]
    assert fb.source_funding_features("HL", series_of(late, "HL"), t, []) is None   # S-1h is 2 h old > 1 h 15 min
    short = hl_rows(S - 26 * 24 * H, S, lag=60_000)                  # fresh but < 640 settlements in 30 days
    assert fb.source_funding_features("HL", series_of(short, "HL"), t, []) is None


def test_okx_freshness_boundary_one_hour_after_each_settlement():
    S = ew.T1 + 40 * 24 * H
    S -= S % (8 * H)
    hist = okx_rows(S - 31 * 24 * H, S - 8 * H)
    fb = ew.FeatureBuilder({}, None, None)
    late = hist + okx_rows(S, S, observed=lambda ts: ts + 2 * H)     # observed 2 h after settlement
    sl = series_of(late, "OKX")
    assert fb.source_funding_features("OKX", sl, S + H, []) is None              # S-8h is 9 h old, S not yet seen
    assert fb.source_funding_features("OKX", sl, S + 2 * H, [])["fund"] == late[-1]["value"]
    assert fb.source_funding_features("OKX", sl, S, [])["fund"] == late[-2]["value"]   # S-8h: 8 h <= 8 h 15 min


def test_hourly_observation_makes_hl_evaluable_and_missed_runs_stay_not_evaluable(env):
    s = env["series"]
    lo, hi = ew.SEAL_END - 900 * H, ew.SEAL_END + 60 * H
    missed = {ts for ts in range(lo - lo % H, hi + 1, H) if (ts // H) % 7 == 0}        # every 7th run lost
    obs = lambda ts: ts + (3 * H if ts in missed else 20 * 60_000)                      # else seen 20 min later
    hl = series_of(hl_rows(lo, hi, observed=obs), "HL")
    fb = ew.FeatureBuilder(s, None, None)
    hours = [u for u in sorted(s) if ew.T1 <= u <= hi - H]
    got = {t: fb.build_sources(t, {"HL": hl})["U_HL"] is not None for t in hours}
    assert any(got.values())
    for t in hours:
        assert got[t] == ((t - H) not in missed), t                  # never filled from an older settlement
