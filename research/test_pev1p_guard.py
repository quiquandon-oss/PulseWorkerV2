"""PEV-1P no-peeking guard (research/pev1p_window.py): no window outcome and no final analysis before the checkpoint,
whichever path asks for it. Synthetic prices only; nothing here reads the real PEV-1P candle archive.

In-process tests fix the wall clock before the checkpoint with monkeypatch. The subprocess tests use the real wall
clock and nothing else (a script cannot be monkeypatched from here); they are skipped once the checkpoint has passed,
when the guard lifts by design.
"""
import copy
import json
import math
import os
import pickle
import re
import subprocess
import sys
from pathlib import Path

import pytest

RESEARCH = Path(__file__).resolve().parent
REPO = RESEARCH.parent
sys.path.insert(0, str(RESEARCH))
import pev1p_window as pw  # noqa: E402
import prediction_eval as pe  # noqa: E402
import prospective_pev as pp  # noqa: E402

H = pe.H
BEFORE = pw.TEST_START_TS + 13 * 24 * H          # 2026-11-14T00:00Z, inside the window, before the checkpoint
REAL_CLOCK_BEFORE_CHECKPOINT = pw.wall_clock_ms() < pw.CHECKPOINT_TS
Premature = pw.PrematurePEV1PEvaluation


def _closes(start, n):
    return {start + i * H: 100 + 5 * math.sin(i / 9) + i * 0.002 for i in range(n)}


def _window_closes():
    """Hourly synthetic closes from 10 days before the window to 10 days into it: window labels are resolvable."""
    return _closes(pw.TEST_START_TS - 10 * 24 * H, 20 * 24)


def _write_inputs(tmp_path, closes):
    d = tmp_path / "post" / "BTC"
    d.mkdir(parents=True)
    d.joinpath("observations.jsonl").write_text("".join(
        json.dumps({"asset": "BTC", "open_ts": t - H, "close_ts": t - 1, "c": str(p)}) + "\n"
        for t, p in sorted(closes.items())))
    s = tmp_path / "sentiment.json"
    s.write_text(json.dumps({"series": [{"observation_ts": t, "score": 50.0}
                                        for t in range(pw.TEST_START_TS - 5 * 24 * H, BEFORE, 3 * H)]}))
    return tmp_path / "post", s


@pytest.fixture
def clock_before_checkpoint(monkeypatch):
    monkeypatch.setattr(pw, "wall_clock_ms", lambda: BEFORE)


# ---------------------------------------------------------------- the boundary itself
def test_window_and_checkpoint_are_the_protocol_dates_and_shared():
    assert pw.TEST_START_TS == 1793491200000 and pw.CHECKPOINT_TS == 1809129600000
    assert (pp.TEST_START_TS, pp.CHECKPOINT_TS) == (pw.TEST_START_TS, pw.CHECKPOINT_TS)
    assert pe.pev1p_window is pw and pp.pw is pw          # one boundary, used by both modules


def test_build_rows_refuses_any_window_label(clock_before_checkpoint):
    for horizons in (pe.HORIZONS_H, (12,), (24,)):
        with pytest.raises(Premature):
            pe.build_rows(_window_closes(), horizons)


def test_labels_before_the_window_are_still_computed(clock_before_checkpoint):
    # closes end at TEST_START + 11 h: no window decision time can resolve, a straddling pre-window one can
    closes = _closes(pw.TEST_START_TS - 10 * 24 * H, 10 * 24 + 12)
    rows = {r["ts"]: r for r in pe.build_rows(closes)}
    assert rows[pw.TEST_START_TS - 12 * H]["labels"][12] is not None     # label time = window start: PEV-1 TEST unit
    assert all(v is None for t, r in rows.items() if t >= pw.TEST_START_TS for v in r["labels"].values())


# ---------------------------------------------------------------- direct calls
def test_final_for_coin_cannot_be_unlocked_by_now_ts(clock_before_checkpoint):
    for closes in (_window_closes(), _closes(pw.TEST_START_TS - 40 * 24 * H, 24 * 24)):   # with and without window data
        for now in (pw.CHECKPOINT_TS, pw.CHECKPOINT_TS + 30 * 24 * H):
            with pytest.raises(Premature):
                pp.final_for_coin(closes, [], now)


def test_final_cli_refuses_even_with_an_injected_checkpoint_now(tmp_path, clock_before_checkpoint):
    candles, sentiment = _write_inputs(tmp_path, _window_closes())
    out = tmp_path / "final.json"
    rc = pp.main(["final", "--candles", str(candles), "--sentiment", str(sentiment), "--out", str(out)],
                 now_ts=pw.CHECKPOINT_TS + 1)
    assert rc == 2 and not out.exists()


def test_hand_built_window_labels_are_refused_where_they_are_used(clock_before_checkpoint):
    rows = pe.build_rows(_window_closes(), horizons=())       # counts-style rows: no labels, allowed
    for r in rows:
        r["labels"] = {24: 0.01}                               # outcomes computed by hand, outside build_rows
    units = [r for r in rows if r["ts"] >= pw.TEST_START_TS and r["ts"] % (24 * H) == 0]
    preds = {"always_up": [1.0] * len(units)}
    with pytest.raises(Premature):
        pe.predict_all(rows, units, 24, {"always_up": lambda up, u, h: 1.0})
    with pytest.raises(Premature):
        pe.score_block(["always_up"], preds, units, 24)
    with pytest.raises(Premature):
        pe.goal_a(["always_up"], preds, units, list(range(len(units))), 24)


# ---------------------------------------------------------------- indirect calls (PEV-1 harness on fresh data)
def test_pev1_evaluate_coin_refuses_data_reaching_into_the_window(clock_before_checkpoint):
    with pytest.raises(Premature):
        pe.evaluate_coin(_window_closes(), [])


def test_pev1_run_cli_refuses_and_writes_no_report(tmp_path, clock_before_checkpoint):
    candles, sentiment = _write_inputs(tmp_path, _window_closes())
    out = tmp_path / "report.json"
    with pytest.raises(Premature):
        pe.main(["run", "--candles", str(candles), "--sentiment", str(sentiment), "--out", str(out)])
    assert not out.exists()


# ---------------------------------------------------------------- what must keep working
def test_counts_mode_runs_before_the_checkpoint_and_reveals_no_outcome(clock_before_checkpoint):
    closes = _window_closes()
    sent = [(t, 50.0) for t in range(pw.TEST_START_TS - 5 * 24 * H, BEFORE, 3 * H)]
    a = pp.counts_for_coin(closes, sent, BEFORE)
    flipped = {t: (2 * 100 - p if t > pw.TEST_START_TS else p) for t, p in closes.items()}
    assert a == pp.counts_for_coin(flipped, sent, BEFORE)
    assert a["24h"]["units_elapsed"] > 0 and a["24h"]["resolved"] > 0


def test_counts_cli_runs_before_the_checkpoint(tmp_path, clock_before_checkpoint):
    candles, sentiment = _write_inputs(tmp_path, _window_closes())
    out = tmp_path / "counts.json"
    assert pp.main(["counts", "--candles", str(candles), "--sentiment", str(sentiment), "--out", str(out)],
                   now_ts=BEFORE) == 0
    report = json.loads(out.read_text())
    assert report["mode"] == "counts" and report["coins"]["BTC"]["24h"]["units_elapsed"] > 0
    assert set(report["coins"]["BTC"]["24h"]) == {"units_elapsed", "resolved", "with_price_features",
                                                  "with_asof_sentiment", "eligible_all_models"}


def test_pinned_pev1_inputs_still_load_before_the_checkpoint(clock_before_checkpoint):
    pinned = RESEARCH / "evaluations" / "pev1-inputs" / "postseal-candles"
    for coin in pe.COINS:
        rows = pe.build_rows(pe.load_candles([pinned], coin))     # no window label is resolvable in them
        assert rows and all(v is None for r in rows if r["ts"] >= pw.TEST_START_TS for v in r["labels"].values())


def test_guard_lifts_at_the_checkpoint(monkeypatch):
    monkeypatch.setattr(pw, "wall_clock_ms", lambda: pw.CHECKPOINT_TS)
    rows = pe.build_rows(_window_closes())
    assert any(r["labels"][24] is not None for r in rows if r["ts"] >= pw.TEST_START_TS)
    pw.check_final()


# ---------------------------------------------------------------- committed code cannot switch the guard off
OVERRIDE = re.compile(r"wall_clock_ms\s*=[^=]|setattr\([^)]*(pev1p_window|\bpw\b)|monkeypatch"
                      r"|object\.__setattr__\(|\b_stamp\b|\.(available_at|decision_ts)\s*=[^=]"
                      r"|(pev1p_window|\bpw)\.(wall_clock_ms|before_checkpoint|check_\w+|in_window|refuse"
                      r"|CHECKPOINT_TS|TEST_START_TS)\s*=[^=]")


def test_no_committed_research_code_overrides_the_guard():
    offenders = []
    for f in REPO.rglob("*.py"):
        if f.name.startswith("test_") or f.name in ("conftest.py", "pev1p_window.py") or "node_modules" in f.parts:
            continue
        for n, line in enumerate(f.read_text(errors="replace").splitlines(), 1):
            if OVERRIDE.search(line):
                offenders.append(f"{f.relative_to(REPO)}:{n}: {line.strip()}")
    assert offenders == []


# ---------------------------------------------------------------- scripts on the real wall clock
def _run(args, tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, *args], cwd=REPO, env=env, capture_output=True, text=True, timeout=120)


PRELUDE = ("import sys; sys.path.insert(0, 'research'); import prediction_eval as pe, prospective_pev as pp; "
           "closes = pe.load_candles([sys.argv[1]], 'BTC'); ")
SCRIPTS = {
    "build_rows": "pe.build_rows(closes)",
    "final_for_coin": "pp.final_for_coin(closes, [], pp.CHECKPOINT_TS)",
    "evaluate_coin": "pe.evaluate_coin(closes, [])",
    "shifted_input": "pe.build_rows({t - 60 * 24 * 3600000: p for t, p in closes.items()})",
    "direct_model_call": "rows = pe.build_rows(closes, horizons=()); q = [r for r in rows if r['ts'] >= pp.TEST_START_TS][-1]; "
                         "pe.model_lr([r for r in rows if r['ts'] <= q['ts']], q, 24, 10.0)",
    "restamped_query": "rows = pe.build_rows(closes, horizons=()); q = [r for r in rows if r['ts'] >= pp.TEST_START_TS][-1]; "
                       "pe.persistence(dict(q, ts=q['ts'] - 60 * 24 * 3600000), 24)",
    "final_cli_injected_now": "sys.exit(pp.main(['final', '--candles', sys.argv[1], '--sentiment', sys.argv[2], "
                              "'--out', sys.argv[3]], now_ts=pp.CHECKPOINT_TS + 1))",
}


@pytest.mark.skipif(not REAL_CLOCK_BEFORE_CHECKPOINT, reason="the guard lifts at the checkpoint by design")
@pytest.mark.parametrize("name", sorted(SCRIPTS))
def test_scripts_cannot_compute_window_outcomes_on_the_real_clock(tmp_path, name):
    candles, sentiment = _write_inputs(tmp_path, _window_closes())
    out = tmp_path / "out.json"
    p = _run(["-c", PRELUDE + SCRIPTS[name], str(candles), str(sentiment), str(out)], tmp_path)
    assert p.returncode != 0 and not out.exists()
    assert "PrematurePEV1PEvaluation" in p.stderr or "refusing" in p.stderr


@pytest.mark.skipif(not REAL_CLOCK_BEFORE_CHECKPOINT, reason="the guard lifts at the checkpoint by design")
def test_clis_on_the_real_clock(tmp_path):
    candles, sentiment = _write_inputs(tmp_path, _window_closes())
    final = _run(["research/prospective_pev.py", "final", "--candles", str(candles), "--sentiment", str(sentiment),
                  "--out", str(tmp_path / "final.json")], tmp_path)
    assert final.returncode == 2 and "refusing" in final.stderr and not (tmp_path / "final.json").exists()
    run = _run(["research/prediction_eval.py", "run", "--candles", str(candles), "--sentiment", str(sentiment),
                "--out", str(tmp_path / "pev1.json")], tmp_path)
    assert run.returncode != 0 and "PrematurePEV1PEvaluation" in run.stderr and not (tmp_path / "pev1.json").exists()
    counts = _run(["research/prospective_pev.py", "counts", "--candles", str(candles), "--sentiment", str(sentiment),
                   "--out", str(tmp_path / "counts.json")], tmp_path)
    assert counts.returncode == 0, counts.stderr
    assert json.loads((tmp_path / "counts.json").read_text())["coins"]["BTC"]["24h"]["units_elapsed"] > 0


# ---------------------------------------------------------------- provenance: copied, re-stamped and shifted data
def _rows_built_after_checkpoint(monkeypatch, closes, horizons=pe.HORIZONS_H):
    """Rows with window labels, built as after the checkpoint (synthetic prices); the clock then returns before it."""
    monkeypatch.setattr(pw, "wall_clock_ms", lambda: pw.CHECKPOINT_TS + 24 * H)
    rows = pe.build_rows(closes, horizons)
    monkeypatch.setattr(pw, "wall_clock_ms", lambda: BEFORE)
    return rows


def test_prices_and_labels_keep_read_only_provenance(tmp_path, monkeypatch):
    candles, _ = _write_inputs(tmp_path, _window_closes())
    closes = pe.load_candles([candles], "BTC")
    assert closes and all(isinstance(p, pw.Price) and p.available_at == t for t, p in closes.items())
    rows = _rows_built_after_checkpoint(monkeypatch, closes)
    labelled = [r for r in rows if r["labels"][24] is not None]
    assert labelled and all(isinstance(r["labels"][24], pw.Label) and r["labels"][24].decision_ts == r["ts"]
                            for r in labelled)
    lab = labelled[0]["labels"][24]
    for copied in (copy.copy(lab), copy.deepcopy(lab), pickle.loads(pickle.dumps(lab))):
        assert copied.decision_ts == lab.decision_ts and float(copied) == float(lab)
    with pytest.raises(AttributeError):
        lab.decision_ts = pw.TEST_START_TS - 30 * 24 * H
    with pytest.raises(AttributeError):
        closes[next(iter(closes))].available_at = 0


def test_copied_and_restamped_window_rows_are_refused(monkeypatch):
    rows = _rows_built_after_checkpoint(monkeypatch, _window_closes())
    window = [r for r in rows if r["ts"] >= pw.TEST_START_TS and r["ts"] % (24 * H) == 0 and r["labels"][24] is not None]
    assert window
    shift = 60 * 24 * H
    for make in (copy.copy, copy.deepcopy, lambda r: pickle.loads(pickle.dumps(r)), dict):
        units = [dict(make(r), ts=r["ts"] - shift) for r in window]       # re-filed before the window
        preds = {"always_up": [1.0] * len(units)}
        with pytest.raises(Premature, match="filed under"):
            pe.score_block(["always_up"], preds, units, 24)
        with pytest.raises(Premature, match="filed under"):
            pe.goal_a(["always_up"], preds, units, list(range(len(units))), 24)
        with pytest.raises(Premature, match="filed under"):
            pe.predict_all(units, units, 24, {"always_up": lambda up, u, h: 1.0})


def test_shifted_input_timestamps_are_refused(tmp_path, clock_before_checkpoint):
    candles, _ = _write_inputs(tmp_path, _window_closes())
    closes = pe.load_candles([candles], "BTC")
    shifted = {t - 60 * 24 * H: p for t, p in closes.items()}               # window prices filed two months earlier
    with pytest.raises(Premature, match="filed under"):
        pe.build_rows(shifted)
    with pytest.raises(Premature, match="filed under"):
        pe.evaluate_coin(shifted, [])
    with pytest.raises(Premature, match="filed under"):
        pe.build_rows(shifted, horizons=())                                 # features only: refused as well
    rows = [dict(r, ts=r["ts"] - 60 * 24 * H) for r in pe.build_rows(closes, horizons=())]  # genuine, then re-stamped
    with pytest.raises(Premature, match="filed under"):
        pe.persistence(rows[-1], 24)
    with pytest.raises(Premature, match="filed under"):
        pe.predict_all(rows, rows[-3:], 24, {"always_up": lambda up, u, h: 1.0})


def test_direct_model_calls_refuse_window_queries(clock_before_checkpoint):
    rows = pe.build_rows(_window_closes(), horizons=())                     # features only: allowed
    q = next(r for r in rows if r["ts"] == pw.TEST_START_TS + 5 * 24 * H)
    upto = [r for r in rows if r["ts"] <= q["ts"]]
    calls = {"model_a": lambda: pe.model_a(upto, q, 24, [], []),
             "model_corrected": lambda: pe.model_corrected(upto, q, 24),
             "model_lr": lambda: pe.model_lr(upto, q, 24, 10.0),
             "model_shrink": lambda: pe.model_shrink(upto, q, 24, 30),
             "climatology": lambda: pe.climatology(upto, q, 24),
             "persistence": lambda: pe.persistence(q, 24)}
    for name, call in calls.items():
        with pytest.raises(Premature, match="prediction"):
            call()


def test_models_still_run_outside_the_window(clock_before_checkpoint):
    rows = pe.build_rows(_closes(pw.TEST_START_TS - 60 * 24 * H, 40 * 24))   # ends 20 days before the window
    q = [r for r in rows if r["ts"] % (24 * H) == 0][-1]
    upto = [r for r in rows if r["ts"] <= q["ts"]]
    for call in (lambda: pe.model_corrected(upto, q, 24), lambda: pe.model_lr(upto, q, 24, 10.0),
                 lambda: pe.model_shrink(upto, q, 24, 30), lambda: pe.climatology(upto, q, 24),
                 lambda: pe.persistence(q, 24), lambda: pe.model_a(upto, q, 24, [], [])):
        call()                                                              # no refusal outside the window


def test_predict_all_computes_nothing_for_window_units(clock_before_checkpoint):
    closes = _closes(pw.TEST_START_TS - 20 * 24 * H, 20 * 24 + 3)          # ends 02:00 into the window, like PEV-1
    rows = pe.build_rows(closes)
    units = pe.unit_times(rows, 24, pe.SEAL_END_TS + 1, rows[-1]["ts"] + 1)
    assert any(u["ts"] >= pw.TEST_START_TS for u in units)
    seen = []
    preds = pe.predict_all(rows, units, 24, {"m": lambda up, u, h: seen.append(u["ts"]) or 0.5})
    assert seen and all(t < pw.TEST_START_TS for t in seen)
    assert [p is None for p in preds["m"]] == [u["ts"] >= pw.TEST_START_TS for u in units]


def test_every_prospective_model_callable_refuses_window_queries(clock_before_checkpoint):
    rows = pe.build_rows(_window_closes(), horizons=())
    q = next(r for r in rows if r["ts"] == pw.TEST_START_TS + 5 * 24 * H)
    upto = [r for r in rows if r["ts"] <= q["ts"]]
    ms = pp.models([], [])
    assert set(ms) == {"always_up", "always_down", "climatology", "persistence", "A", "B", "C", "P4-LR", "P4-KNN-SHRINK"}
    for name, fn in ms.items():
        with pytest.raises(Premature, match="prediction"):
            fn(upto, q, 24)
    pre = [r for r in pe.build_rows(_closes(pw.TEST_START_TS - 60 * 24 * H, 40 * 24)) if r["ts"] % (24 * H) == 0]
    assert pp.models([], [])["always_up"](pre[:-1], pre[-1], 24) == 1.0     # outside the window: unchanged


def test_models_refuse_refiled_training_rows_with_window_labels(monkeypatch):
    rows = _rows_built_after_checkpoint(monkeypatch, _closes(pw.TEST_START_TS - 30 * 24 * H, 45 * 24))
    shift = 90 * 24 * H
    refiled = [dict(r, ts=r["ts"] - shift) for r in rows]                 # window-stamped labels re-filed earlier
    q = dict(next(r for r in pe.build_rows(_closes(pw.TEST_START_TS - 200 * 24 * H, 24 * 24)) if r["technical_score"]),
             ts=max(r["ts"] for r in refiled) + 48 * H)                    # a genuine pre-window query
    for call in (lambda: pe.model_corrected(refiled, q, 24), lambda: pe.model_lr(refiled, q, 24, 10.0),
                 lambda: pe.model_shrink(refiled, q, 24, 30), lambda: pe.climatology(refiled, q, 24),
                 lambda: pe.model_a(refiled, q, 24, [], [])):
        with pytest.raises(Premature, match="filed under"):
            call()


def test_one_refiled_window_price_is_refused_for_features_and_prev_ret(tmp_path, clock_before_checkpoint):
    candles, _ = _write_inputs(tmp_path, _closes(pw.TEST_START_TS - 40 * 24 * H, 50 * 24))
    closes = pe.load_candles([candles], "BTC")
    pre_t = pw.TEST_START_TS - 20 * 24 * H                                  # a pre-window decision time
    window_price = closes[pw.TEST_START_TS + 5 * 24 * H]
    tampered = {t: p for t, p in closes.items() if t < pw.TEST_START_TS - 10 * 24 * H}  # nothing else from the window
    tampered[pre_t - 24 * H] = window_price                                 # re-filed as the past close of pre_t
    for horizons in ((), (24,), pe.HORIZONS_H):                             # features only, prev_ret, labels
        with pytest.raises(Premature, match="filed under"):
            pe.build_rows(tampered, horizons)
    untampered = {t: p for t, p in closes.items() if t < pw.TEST_START_TS - 10 * 24 * H}
    assert pe.build_rows(untampered)                                        # genuine pre-window data still builds
