import hashlib
import io
import zipfile

import risk_regime_oi_collect as oc

METRICS_CSV = ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
               "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
               "2026-09-27 12:00:00,BTCUSDT,80000.5,6790000000.1,1.9,1.5,2.0,0.9\n"
               "2026-09-27 12:05:00,BTCUSDT,80010.0,6791000000.0,1.9,1.5,2.0,1.1\n")
FUNDING_CSV = "calc_time,funding_interval_hours,last_funding_rate\n1790496000000,8,0.0001\n"


def zipped(name, text):
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr(name, text)
    return b.getvalue()


def fake_fetch(files):
    def f(url, timeout=60):
        return files.get(url, (404, b"not found"))
    return f


def archive_files(kind, label, text, good_checksum=True):
    url = f"{oc.ARCHIVE}/{'daily' if kind == 'metrics' else 'monthly'}/{kind}/BTCUSDT/BTCUSDT-{kind}-{label}.zip"
    z = zipped(url.rsplit("/", 1)[-1][:-4] + ".csv", text)
    digest = hashlib.sha256(z).hexdigest() if good_checksum else "0" * 64
    return {url: (200, z), url + ".CHECKSUM": (200, f"{digest}  BTCUSDT-{kind}-{label}.zip".encode())}


def test_metrics_archive_parsed_with_checksum_and_availability_rule():
    files = archive_files("metrics", "2026-09-27", METRICS_CSV)
    r = oc.collect_archive("metrics", ["2026-09-27", "2026-09-28"], fetch=fake_fetch(files), pause_s=0)
    assert r["status"] == "OK" and r["files"] == {"checksum_ok": 1, "http_404": 1}
    oi = [o for o in r["observations"] if o["metric"] == "open_interest"]
    t = 1790510400000                                      # 2026-09-27 12:00 UTC
    assert oi[0]["timestamp"] == t and oi[0]["available_at"] == t + 5 * 60_000 and oi[0]["value"] == 80000.5
    assert oi[0]["unit"] == "BTC" and oi[0]["interval"] == "5m" and oi[0]["raw"]["sum_taker_long_short_vol_ratio"] == "0.9"
    usd = [o for o in r["observations"] if o["metric"] == "open_interest_usd"]
    assert usd[0]["unit"] == "USD"
    kept = [x for x in r["raw"] if x["status"] == 200][0]
    assert kept["checksum_verified"] is True and kept["body"] == METRICS_CSV


def test_checksum_mismatch_is_rejected_not_used():
    files = archive_files("metrics", "2026-09-27", METRICS_CSV, good_checksum=False)
    r = oc.collect_archive("metrics", ["2026-09-27"], fetch=fake_fetch(files), pause_s=0)
    assert r["observations"] == [] and r["files"] == {"checksum_failed": 1}


def test_unreachable_archive_is_reported_not_filled():
    r = oc.collect_archive("metrics", ["2026-09-27"], fetch=lambda u, timeout=60: (403, b"denied"), pause_s=0)
    assert r["status"] == "BLOCKED_OR_UNAVAILABLE" and r["observations"] == []


def test_funding_archive():
    files = archive_files("fundingRate", "2026-09", FUNDING_CSV)
    r = oc.collect_archive("fundingRate", ["2026-09"], fetch=fake_fetch(files), pause_s=0)
    o = r["observations"][0]
    assert o["source"] == "binance_funding_archive" and o["available_at"] == o["timestamp"] == 1790496000000 and o["unit"] == "rate/8h"


def test_location_parses_trace_and_does_not_keep_raw_ip():
    body = b"fl=1\nip=203.0.113.9\nloc=DE\ncolo=FRA\n"
    loc = oc.location(fetch=lambda u, timeout=60: (200, body))
    assert loc["loc"] == "DE" and loc["colo"] == "FRA" and "203.0.113.9" not in str(loc) and loc["ip_sha256_prefix"]


def test_labels():
    from datetime import datetime, timezone
    a, b = datetime(2026, 11, 30, tzinfo=timezone.utc), datetime(2027, 1, 2, tzinfo=timezone.utc)
    assert oc.month_labels(a, b) == ["2026-11", "2026-12", "2027-01"]
    assert oc.day_labels(b, b) == ["2027-01-02"]
