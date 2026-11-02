"""Post-seal candle archive: append-only, revisions kept, nothing sealed or still open, manifest per run."""
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import candle_archive as ca  # noqa: E402
import prediction_eval as pe  # noqa: E402

H = pe.H


def fake_post(prices):
    """Serves hourly candles from a dict open_ts -> close price, like candleSnapshot (includes the open candle)."""
    calls = []

    def post(body):
        lo, hi = body["req"]["startTime"], body["req"]["endTime"]
        calls.append(body["req"]["coin"])
        return [{"t": t, "T": t + H - 1, "o": "1", "h": "2", "l": "0.5", "c": str(p), "v": "10", "n": 5}
                for t, p in sorted(prices.items()) if lo <= t < hi]
    post.calls = calls
    return post


def test_archive_is_append_only_keeps_revisions_and_never_stores_sealed_or_open_candles(tmp_path):
    start = pe.SEAL_END_TS - 5 * H                          # includes sealed candles in the upstream response
    prices = {start + i * H: 100 + i for i in range(30)}
    now1 = pe.SEAL_END_TS + 10 * H + 30 * 60000             # the candle opened at SEAL_END+10h is still open
    run1 = ca.update(tmp_path, post=fake_post(prices), now_ts=now1, page_hours=7)
    obs = tmp_path / "BTC" / "observations.jsonl"
    first = obs.read_bytes()
    rows = [json.loads(x) for x in first.decode().splitlines()]
    assert all(pe.SEAL_END_TS < r["available_at"] <= now1 for r in rows)
    assert len(rows) == 10 and run1["assets"]["BTC"]["gaps"] == 0
    # later run: one already-archived candle was revised upstream, and new candles closed
    prices[pe.SEAL_END_TS + 8 * H] = 999
    now2 = now1 + 6 * H
    run2 = ca.update(tmp_path, post=fake_post(prices), now_ts=now2)
    after = obs.read_bytes()
    assert after.startswith(first)                          # nothing rewritten
    rows2 = [json.loads(x) for x in after.decode().splitlines()]
    assert len(rows2) == len({r["open_ts"] for r in rows2}) == 16
    assert next(r for r in rows2 if r["open_ts"] == pe.SEAL_END_TS + 8 * H)["c"] != "999"   # first-seen value kept
    rev = [json.loads(x) for x in (tmp_path / "BTC" / "revisions.jsonl").read_text().splitlines()]
    assert len(rev) == 1 and rev[0]["revised"]["c"] == "999"
    assert run2["assets"]["BTC"]["appended"] == 6 and run2["assets"]["BTC"]["revisions"] == 1


def test_manifest_records_file_hashes(tmp_path):
    prices = {pe.SEAL_END_TS + i * H: 50 for i in range(5)}
    run = ca.update(tmp_path, post=fake_post(prices), now_ts=pe.SEAL_END_TS + 6 * H)
    m = json.loads((tmp_path / "runs" / f"{run['run_id']}.json").read_text())
    for rel, h in m["sha256"].items():
        assert hashlib.sha256((tmp_path / rel).read_bytes()).hexdigest() == h
    assert set(m["assets"]) == {"BTC", "ETH", "LINK"}


def test_requests_never_start_before_the_seal_end(tmp_path):
    seen = []

    def post(body):
        seen.append(body["req"]["startTime"])
        return []
    ca.update(tmp_path, post=post, now_ts=pe.SEAL_END_TS + 3 * H)
    assert min(seen) >= pe.SEAL_END_TS
