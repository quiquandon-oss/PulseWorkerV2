import gzip
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import risk_regime_forward as fw
import risk_regime_oi_collect as oc

H, D = fw.HOUR, fw.DAY
T0 = int(datetime(2026, 10, 8, tzinfo=timezone.utc).timestamp() * 1000)       # day after the frozen OI data
HEADER = ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
          "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n")
CUT = {"binance_oi_archive|BTCUSDT|open_interest": T0 - 5 * 60_000, "binance_oi_archive|BTCUSDT|open_interest_usd": T0 - 5 * 60_000}


def metrics_csv(day_ms, skip=(), bump=None):
    rows = []
    for k in range(288):
        if k in skip:
            continue
        t = datetime.fromtimestamp((day_ms + k * 300_000) / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        v = 90000.0 + k + (0.5 if bump == k else 0.0)
        rows.append(f"{t},BTCUSDT,{v},{v * 85000},1,1,1,1\n")
    return HEADER + "".join(rows)


def archive(day_label, text):
    url = f"{oc.ARCHIVE}/daily/metrics/BTCUSDT/BTCUSDT-metrics-{day_label}.zip"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(f"BTCUSDT-metrics-{day_label}.csv", text)
    zb = b.getvalue()
    return {url: (200, zb), url + ".CHECKSUM": (200, f"{hashlib.sha256(zb).hexdigest()}  x.zip".encode())}


def fetcher(files):
    def f(url, timeout=60):
        if url == oc.TRACE:
            return 200, b"ip=192.0.2.1\nloc=XX\ncolo=TST\n"
        return files.get(url, (404, b"not found"))
    return f


def run(tmp, files, now, cut=CUT):
    return fw.run_collection(tmp, ["binance_oi_archive"], now=now, cutoffs=cut, fetch=fetcher(files))


def statuses(r):
    return {(a["partition"], a["status"]) for a in r["attempts"]}


def test_idempotent_rerun_is_a_noop_and_keeps_first_collection(tmp_path):
    files = archive("2026-10-08", metrics_csv(T0))
    now = T0 + 2 * D + H
    r1 = run(tmp_path, files, now)
    assert ("2026-10-08", "WRITTEN") in statuses(r1)
    p, m, raw = fw.ForwardStore(tmp_path, CUT).paths("binance_oi_archive", "2026-10-08")
    b1, meta1 = p.read_bytes(), json.loads(m.read_text())
    r2 = run(tmp_path, files, now + H)                     # complete partitions are not even re-attempted
    assert all(a["partition"] != "2026-10-08" for a in r2["attempts"])
    assert p.read_bytes() == b1 and json.loads(m.read_text())["first_collected_at"] == meta1["first_collected_at"]
    rec = fw.ForwardStore(tmp_path, CUT).write("binance_oi_archive", "2026-10-08",
                                               oc.collect_archive("metrics", ["2026-10-08"], fetch=fetcher(files), pause_s=0)["observations"],
                                               [], ["binance_oi_archive|BTCUSDT|open_interest", "binance_oi_archive|BTCUSDT|open_interest_usd"],
                                               {"started_at": "later", "run_id": "x"})
    assert rec["status"] == "UNCHANGED" and p.read_bytes() == b1      # forced re-write of identical content: no change
    assert meta1["completeness"] == "COMPLETE" and meta1["observations"] == 576 and raw.exists()


def test_gaps_detected_never_filled_and_superseded_when_complete(tmp_path):
    now = T0 + 2 * D + H
    r1 = run(tmp_path, archive("2026-10-08", metrics_csv(T0, skip=(10, 11))), now)
    store = fw.ForwardStore(tmp_path, CUT)
    m = store.meta("binance_oi_archive", "2026-10-08")
    assert m["completeness"] == "GAPS" and m["missing_slots"] == 4 and m["observations"] == 572     # 2 slots x 2 metrics, nothing filled
    assert m["missing_ranges"]["binance_oi_archive|BTCUSDT|open_interest"] == [["2026-10-08T00:50:00+00:00", "2026-10-08T00:55:00+00:00"]]
    rows = fw.read_partition(store.paths("binance_oi_archive", "2026-10-08")[0])
    assert all(o["value"] != 0 for o in rows) and len(rows) == 572
    r2 = run(tmp_path, archive("2026-10-08", metrics_csv(T0)), now + H)    # GAPS partitions are retried
    assert ("2026-10-08", "SUPERSEDED") in statuses(r2)
    m2 = store.meta("binance_oi_archive", "2026-10-08")
    assert m2["completeness"] == "COMPLETE" and m2["superseded"] == [m["fingerprint"]] and m2["first_collected_at"] == m["first_collected_at"]


def test_conflicting_value_is_never_overwritten(tmp_path):
    now = T0 + 2 * D + H
    run(tmp_path, archive("2026-10-08", metrics_csv(T0, skip=(5,))), now)
    store = fw.ForwardStore(tmp_path, CUT)
    before = store.paths("binance_oi_archive", "2026-10-08")[0].read_bytes()
    r = run(tmp_path, archive("2026-10-08", metrics_csv(T0, bump=7)), now + H)
    conflict = [a for a in r["attempts"] if a["status"] == "CONFLICT"]
    assert conflict and conflict[0]["differing"] and store.paths("binance_oi_archive", "2026-10-08")[0].read_bytes() == before


def test_frozen_data_not_duplicated_and_utc_partition_boundaries(tmp_path):
    cut = {k: T0 + 12 * H - 5 * 60_000 for k in CUT}              # frozen data reaches 11:55 of the day
    run(tmp_path, archive("2026-10-08", metrics_csv(T0)), T0 + 2 * D, cut=cut)
    store = fw.ForwardStore(tmp_path, cut)
    rows = fw.read_partition(store.paths("binance_oi_archive", "2026-10-08")[0])
    assert min(o["timestamp"] for o in rows) == T0 + 12 * H and max(o["timestamp"] for o in rows) == T0 + D - 5 * 60_000
    assert store.meta("binance_oi_archive", "2026-10-08")["completeness"] == "COMPLETE"     # expected slots start after the cutoff
    assert fw.partition_of("day", T0 + D - 1) == "2026-10-08" and fw.partition_of("day", T0 + D) == "2026-10-09"
    assert fw.slot_of(T0 + 8 * H + 2, 8 * H) == T0 + 8 * H and fw.slot_of(T0 + 8 * H - 2, 8 * H) == T0 + 8 * H   # funding ms jitter


def test_not_yet_published_then_missing_when_late(tmp_path):
    r = run(tmp_path, {}, T0 + D + 2 * H)
    assert ("2026-10-08", "NOT_YET_PUBLISHED") in statuses(r)
    r = run(tmp_path, {}, T0 + 5 * D)
    assert ("2026-10-08", "MISSING") in statuses(r)
    idx = json.loads((tmp_path / "index.json").read_text())
    assert idx["sources"]["binance_oi_archive"]["partitions"] == 0
    assert not list((tmp_path / "binance_oi_archive").glob("**/*.jsonl.gz"))     # nothing written for missing data


def candle_fn(fail=False):
    def f(sym, lo, hi, cache, log):
        if fail:
            raise OSError("network down")
        return [{"t": t, "T": t + H - 1, "o": 1.0, "h": 1.0, "l": 1.0, "c": 2.0, "v": 3.0, "n": 1} for t in range(lo, hi, H)]
    return f


def funding_fn(lo, hi, cache, log):
    return [{"t": t + 31, "funding": 1.25e-5, "premium": -2e-4} for t in range(lo, hi, H)]


def test_hyperliquid_failure_is_recorded_and_recovered(tmp_path):
    cut = {f"hyperliquid_hip3|{s}|{m}": T0 - H for s in fw.HL_SYMBOLS for m in ("close", "volume")}
    cut.update({f"hyperliquid_btc_funding|BTC|{m}": T0 - H + 31 for m in ("funding_rate", "premium")})
    now = T0 + D + 2 * H
    r1 = fw.run_collection(tmp_path, ["hyperliquid_hip3", "hyperliquid_btc_funding"], now=now, cutoffs=cut,
                           fetch=fetcher({}), candles_fn=candle_fn(fail=True), funding_fn=funding_fn)
    assert any(a["status"] == "FETCH_FAILED" and a["source"] == "hyperliquid_hip3" for a in r1["attempts"])
    assert list((tmp_path / "runs").glob("*.json"))                                     # the failed run is on record
    assert not (tmp_path / "hyperliquid_hip3").exists()
    r2 = fw.run_collection(tmp_path, ["hyperliquid_hip3", "hyperliquid_btc_funding"], now=now + H, cutoffs=cut,
                           fetch=fetcher({}), candles_fn=candle_fn(), funding_fn=funding_fn)
    store = fw.ForwardStore(tmp_path, cut)
    assert store.meta("hyperliquid_hip3", "2026-10-08")["completeness"] == "COMPLETE"
    assert store.meta("hyperliquid_hip3", "2026-10-08")["observations"] == 24 * len(fw.HL_SYMBOLS) * 2
    assert store.meta("hyperliquid_btc_funding", "2026-10-08")["observations"] == 48     # unchanged partition from run 1 kept
    assert not list(tmp_path.glob("**/.*.tmp"))
    loaded = fw.load_forward(tmp_path)
    assert {o["source"] for o in loaded} == {"hyperliquid_hip3", "hyperliquid_btc_funding"} and len(loaded) == 24 * 18 + 48


def test_staleness_flag(tmp_path):
    run(tmp_path, archive("2026-10-08", metrics_csv(T0)), T0 + 2 * D)
    idx = fw.ForwardStore(tmp_path, CUT).rebuild_index(T0 + 2 * D)
    assert idx["sources"]["binance_oi_archive"]["stale"] is False
    assert fw.ForwardStore(tmp_path, CUT).rebuild_index(T0 + 10 * D)["sources"]["binance_oi_archive"]["stale"] is True
