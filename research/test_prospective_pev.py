"""PEV-1P: frozen dates/hyperparameters, no outcome information before the checkpoint, prospective window only."""
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import prediction_eval as pe  # noqa: E402
import prospective_pev as pp  # noqa: E402

H = pe.H
ms = lambda s: int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp() * 1000)


def test_frozen_dates_and_hyperparameters_match_the_protocol():
    assert pp.TEST_START_TS == ms("2026-11-01T00:00:00")
    assert pp.CHECKPOINT_TS == ms("2027-05-01T00:00:00")
    proto = (Path(__file__).resolve().parent / "plans" / "2026-11-02-pev1p-protocol.md").read_text()
    assert "2026-11-01T00:00Z" in proto and "2027-05-01T00:00Z" in proto
    assert "λ = 10" in proto and "m = 30" in proto
    report = json.loads((Path(__file__).resolve().parent / "evaluations" / "2026-11-01-pev1-report.json").read_text())
    for coin in report["coins"].values():
        for block in coin.values():
            assert block["selected"] == pp.FROZEN          # frozen to what PEV-1 VAL actually chose


def _closes(start, n):
    return {start + i * H: 100 + 5 * math.sin(i / 9) + i * 0.002 for i in range(n)}


def test_counts_mode_reveals_no_outcome_information():
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, 40 * 24)
    now = pp.TEST_START_TS + 15 * 24 * H
    sent = [(t, 50.0) for t in range(pp.TEST_START_TS - 5 * 24 * H, now, 3 * H)]
    a = pp.counts_for_coin(closes, sent, now)
    flipped = {t: (2 * 100 - p if t > pp.TEST_START_TS else p) for t, p in closes.items()}   # every outcome reversed
    b = pp.counts_for_coin(flipped, sent, now)
    assert a == b                                             # counts do not depend on what happened
    assert set(a["24h"]) == {"units_elapsed", "resolved", "with_price_features", "with_asof_sentiment", "eligible_all_models"}
    assert a["24h"]["units_elapsed"] == 15                   # ts + 24 h <= now: days 0..14 of the window
    assert a["12h"]["units_elapsed"] == 30                   # ts + 12 h <= now: half-days 0..29


def test_only_units_inside_the_prospective_window_are_used():
    rows = pe.build_rows(_closes(pp.TEST_START_TS - 10 * 24 * H, 20 * 24))
    units = pp.test_units(rows, 24, pp.TEST_START_TS + 5 * 24 * H)
    assert units and min(u["ts"] for u in units) >= pp.TEST_START_TS
    assert max(u["ts"] for u in units) + 24 * H <= pp.TEST_START_TS + 5 * 24 * H


def test_test_units_excludes_started_units_before_their_label_time():
    now = pp.TEST_START_TS + 5 * 24 * H + 3 * H
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, (now + 30 * 24 * H - (pp.TEST_START_TS - 20 * 24 * H)) // H + 1)
    rows = pe.build_rows(closes)
    counts = pp.counts_for_coin(closes, [], now)
    for h in pe.HORIZONS_H:
        expected = list(range(pp.TEST_START_TS, now - h * H + 1, h * H))
        units = pp.test_units(rows, h, now)
        assert [u["ts"] for u in units] == expected
        assert counts[f"{h}h"]["units_elapsed"] == len(expected)


def test_final_units_are_frozen_at_checkpoint():
    rows = pe.build_rows(_closes(pp.TEST_START_TS, 220 * 24))
    for h in pe.HORIZONS_H:
        at = pp.final_units(rows, h, pp.CHECKPOINT_TS)
        later = pp.final_units(rows, h, pp.CHECKPOINT_TS + 30 * 24 * H)
        assert at
        assert all(u["ts"] + h * H <= pp.CHECKPOINT_TS for u in at)
        assert [u["ts"] for u in at] == [u["ts"] for u in later]


def test_final_units_match_the_exact_checkpoint_population():
    rows = pe.build_rows(_closes(pp.TEST_START_TS, 220 * 24))
    expected_sizes = {24: 181, 12: 362}
    for h in pe.HORIZONS_H:
        expected = list(range(pp.TEST_START_TS, pp.CHECKPOINT_TS - h * H + 1, h * H))
        assert len(expected) == expected_sizes[h]
        assert expected[-1] + h * H == pp.CHECKPOINT_TS
        assert expected[-1] + h * H + h * H > pp.CHECKPOINT_TS
        for now in (pp.CHECKPOINT_TS, pp.CHECKPOINT_TS + 30 * 24 * H):
            units = pp.final_units(rows, h, now)
            assert [u["ts"] for u in units] == expected


def test_final_for_coin_is_frozen_at_checkpoint(monkeypatch):
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, 220 * 24)
    sentiment = [(t, 50.0) for t in range(pp.TEST_START_TS - 5 * 24 * H,
                                          pp.CHECKPOINT_TS + 40 * 24 * H, 3 * H)]
    captured = []

    def predict_all(rows, units, h, models):
        captured.append((h, [u["ts"] for u in units]))
        return {}

    monkeypatch.setattr(pe, "predict_all", predict_all)
    monkeypatch.setattr(pe, "score_block", lambda names, preds, units, h, compare_to=None: ({"n": len(units)}, []))
    monkeypatch.setattr(pe, "decision_label", lambda block, model: "test")
    monkeypatch.setattr(pe, "goal_a", lambda *args: {})
    at = pp.final_for_coin(closes, sentiment, pp.CHECKPOINT_TS)
    at_units = list(captured)
    assert at_units
    assert all(units for _, units in at_units)
    captured.clear()
    later = pp.final_for_coin(closes, sentiment, pp.CHECKPOINT_TS + 30 * 24 * H)
    assert captured == at_units
    assert {key: block["n"] for key, block in at.items()} == {key: block["n"] for key, block in later.items()}


def test_final_for_coin_matches_the_exact_checkpoint_population(monkeypatch):
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, 220 * 24)
    sentiment = [(t, 50.0) for t in range(pp.TEST_START_TS - 5 * 24 * H,
                                          pp.CHECKPOINT_TS + 40 * 24 * H, 3 * H)]
    captured = []

    def predict_all(rows, units, h, models):
        captured.append((h, [u["ts"] for u in units]))
        return {}

    monkeypatch.setattr(pe, "predict_all", predict_all)
    monkeypatch.setattr(pe, "score_block", lambda names, preds, units, h, compare_to=None: ({"n": len(units)}, []))
    monkeypatch.setattr(pe, "decision_label", lambda block, model: "test")
    monkeypatch.setattr(pe, "goal_a", lambda *args: {})
    for now in (pp.CHECKPOINT_TS, pp.CHECKPOINT_TS + 30 * 24 * H):
        pp.final_for_coin(closes, sentiment, now)
        expected = {
            h: list(range(pp.TEST_START_TS, pp.CHECKPOINT_TS - h * H + 1, h * H))
            for h in pe.HORIZONS_H
        }
        assert {h: timestamps for h, timestamps in captured} == expected
        for h, timestamps in expected.items():
            assert len(timestamps) == {24: 181, 12: 362}[h]
            assert timestamps[-1] + h * H == pp.CHECKPOINT_TS
            assert timestamps[-1] + 2 * h * H > pp.CHECKPOINT_TS
        captured.clear()


def test_counts_stay_rolling_past_the_checkpoint():
    extra_days = 10
    end = pp.CHECKPOINT_TS + (extra_days + 2) * 24 * H
    closes = _closes(pp.TEST_START_TS, (end - pp.TEST_START_TS) // H + 1)
    at = pp.counts_for_coin(closes, [], pp.CHECKPOINT_TS)
    later = pp.counts_for_coin(closes, [], pp.CHECKPOINT_TS + extra_days * 24 * H)
    expected_extra_units = {24: extra_days, 12: 2 * extra_days}
    for h in pe.HORIZONS_H:
        key = f"{h}h"
        increase = later[key]["units_elapsed"] - at[key]["units_elapsed"]
        assert increase == expected_extra_units[h]
        assert later[key]["resolved"] - at[key]["resolved"] == increase


def test_counts_does_not_call_label_computation(monkeypatch):
    original = pe.build_rows

    def build_rows(closes, horizons=pe.HORIZONS_H):
        assert not horizons
        rows = original(closes, horizons=horizons)
        assert all(row["labels"] == {} and row["prev_ret"] == {} for row in rows)
        return rows

    monkeypatch.setattr(pe, "build_rows", build_rows)
    pp.counts_for_coin(_closes(pp.TEST_START_TS - H, 50), [], pp.TEST_START_TS + 24 * H, horizons=(24,))


def test_rows_without_horizons_match_default_features():
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, 50 * 24)
    default = pe.build_rows(closes)
    without_labels = pe.build_rows(closes, horizons=())
    fields = ("ts", "price", "technical_score", "regime_mag")
    assert [{key: row[key] for key in fields} for row in without_labels] == [
        {key: row[key] for key in fields} for row in default
    ]


def test_resolved_count_matches_original_label_rule():
    closes = _closes(pp.TEST_START_TS - 20 * 24 * H, 40 * 24)
    now = pp.TEST_START_TS + 15 * 24 * H
    for t in range(pp.TEST_START_TS + 10 * 24 * H, pp.TEST_START_TS + 16 * 24 * H, H):
        del closes[t]
    sentiment = [(t, 50.0) for t in range(pp.TEST_START_TS - 5 * 24 * H, now, 3 * H)]
    rows = pe.build_rows(closes)
    expected_resolved, expected_eligible = {}, {}
    for h in pe.HORIZONS_H:
        units = pp.test_units(rows, h, now)
        expected_resolved[f"{h}h"] = sum(u["labels"][h] is not None for u in units)
        expected_eligible[f"{h}h"] = sum(
            u["labels"][h] is not None and pe._has_features(u)
            and pe.asof_reading([s[0] for s in sentiment], sentiment, u["ts"]) is not None
            for u in units
        )
    actual = pp.counts_for_coin(closes, sentiment, now)
    assert {key: value["resolved"] for key, value in actual.items()} == expected_resolved
    assert any(value["resolved"] < value["units_elapsed"] for value in actual.values())
    assert {key: value["eligible_all_models"] for key, value in actual.items()} == expected_eligible
    assert any(value > 0 for value in expected_eligible.values())


def test_final_refuses_before_the_checkpoint(tmp_path):
    rc = pp.main(["final", "--candles", str(tmp_path), "--sentiment", str(tmp_path / "s.json"), "--out", str(tmp_path / "o.json")],
                 now_ts=pp.CHECKPOINT_TS - 1)
    assert rc == 2 and not (tmp_path / "o.json").exists()


def test_fetch_paginates_and_keeps_only_closed_post_seal_candles(tmp_path):
    calls = []
    end = pe.SEAL_END_TS + 10 * H

    def post(body):
        lo, hi = body["req"]["startTime"], body["req"]["endTime"]
        calls.append((lo, hi))
        out = []
        t = (lo // H) * H
        while t < hi:
            out.append({"t": t, "T": t + H - 1, "c": "1"})
            t += H
        return out
    pp.fetch(tmp_path, start_ts=pe.SEAL_END_TS - 2 * H, end_ts=end, post=post, page_hours=4)
    assert len(calls) == 9                                    # 12 h in 4 h pages, for each of the 3 coins
    lines = [json.loads(x) for x in (tmp_path / "BTC" / "observations.jsonl").read_text().splitlines()]
    assert all(pe.SEAL_END_TS < x["available_at"] <= end for x in lines)
    assert len(lines) == len({x["open_ts"] for x in lines})   # no duplicates across pages
