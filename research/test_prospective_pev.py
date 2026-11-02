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
