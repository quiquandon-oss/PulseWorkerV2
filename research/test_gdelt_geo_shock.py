"""Tests for the GDELT geopolitical component (research only). No network: every row below is a synthetic
fixture in the documented GDELT 2.0 export layout (61 tab-separated columns), not real GDELT data."""
import io
import json
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

import gdelt_geo_shock as g
import gdelt_research_run as run

UTC = timezone.utc
E = datetime(2026, 9, 28, 3, 1, 24, 213000, tzinfo=UTC)   # Event #15


def row(eid=1, added="20260928020000", sql=None, root="19", code=None, quad=4, gold=-10.0, mentions=10, sources=3,
        articles=10, a1="USA", a2="IRN", a1type="", a2type="", geo="IR", geoname="Strait of Hormuz, Iran",
        url="https://www.example.com/world/hormuz-tanker-strike", is_root="1"):
    c = [""] * len(g.EXPORT_COLUMNS)
    c[g.COL["GLOBALEVENTID"]] = str(eid)
    c[g.COL["SQLDATE"]] = sql or added[:8]
    c[g.COL["Actor1CountryCode"]] = a1
    c[g.COL["Actor2CountryCode"]] = a2
    c[g.COL["Actor1Type1Code"]] = a1type
    c[g.COL["Actor2Type1Code"]] = a2type
    c[g.COL["IsRootEvent"]] = is_root
    c[g.COL["EventRootCode"]] = root
    c[g.COL["EventCode"]] = code or (root + "0")
    c[g.COL["EventBaseCode"]] = c[g.COL["EventCode"]][:3]
    c[g.COL["QuadClass"]] = str(quad)
    c[g.COL["GoldsteinScale"]] = str(gold)
    c[g.COL["NumMentions"]] = str(mentions)
    c[g.COL["NumSources"]] = str(sources)
    c[g.COL["NumArticles"]] = str(articles)
    c[g.COL["ActionGeo_CountryCode"]] = geo
    c[g.COL["ActionGeo_FullName"]] = geoname
    c[g.COL["DATEADDED"]] = added
    c[g.COL["SOURCEURL"]] = url
    return "\t".join(c)


def zip_of(lines, name="20260928020000.export.CSV"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, "\n".join(lines) + "\n")
    return buf.getvalue()


# 1. parsing
def test_parses_export_zip_and_columns():
    assert len(g.EXPORT_COLUMNS) == 61 and g.COL["DATEADDED"] == 59 and g.COL["SOURCEURL"] == 60
    data = zip_of([row(eid=1), row(eid=2, root="04", quad=1, gold=1.0)])
    rows = g.parse_export_zip(data, "20260928020000.export.CSV.zip")
    assert [r.event_id for r in rows] == [1, 2]
    r = rows[0]
    assert (r.root, r.code, r.quad, r.goldstein, r.mentions, r.sources, r.articles) == ("19", "190", 4, -10.0, 10, 3, 10)
    assert r.action_country == "IR" and r.actor1_country == "USA" and r.date_added == "20260928020000"
    assert g.batch_ts_from_name("http://data.gdeltproject.org/gdeltv2/20260928020000.export.CSV.zip") == datetime(2026, 9, 28, 2, 0, tzinfo=UTC)


def test_malformed_and_lookahead_rows_are_rejected_not_repaired():
    rej = Counter()
    lines = [row(eid=1), "too\tfew\tcolumns", row(eid=-5), row(eid=3, added="20260928021500"),
             row(eid=4, sql="20260929"), row(eid=5, sql="20260925")]
    rows = list(g.parse_export_lines(lines, "20260928020000", rej))
    assert [r.event_id for r in rows] == [1, 5]          # backdated SQLDATE is fine: it is placed at DATEADDED
    assert rej == Counter(bad_column_count=1, bad_event_id=1, dateadded_not_this_batch=1, event_date_after_discovery=1)


# 2./3. CAMEO filtering and geographic relevance
def test_filter_uses_reusable_cameo_categories_not_country_names():
    rows = list(g.parse_export_lines([
        row(eid=1, root="19", quad=4),                                             # fight, Iran/US
        row(eid=2, root="13", code="138", quad=3, gold=-7.0, a1="RUS", a2="UKR", geo="UP", geoname="Kyiv, Ukraine"),
        row(eid=3, root="16", code="163", quad=3, gold=-8.0, a1="USA", a2="VEN", geo="VE", geoname="Caracas"),
        row(eid=4, root="04", code="040", quad=1, gold=1.0),                        # consult: cooperative
        row(eid=5, root="19", quad=1, gold=-10.0),                                  # inconsistent quad: not counted
        row(eid=6, root="14", code="141", quad=3, gold=-6.5, a1="FRA", a2="", geo="FR", geoname="Paris", url="https://lemonde.fr/politique/manifestation"),
    ], "20260928020000"))
    cls = {r.event_id: g.classify(r) for r in rows}
    assert cls[1].relevant and cls[1].category == "armed_conflict" and cls[1].escalation and cls[1].corridor and cls[1].major_power
    assert cls[2].relevant and cls[2].category == "threat" and cls[2].escalation and not cls[2].corridor   # non-Gulf conflict still counts
    assert cls[3].relevant and cls[3].category == "sanctions_reduction" and cls[3].escalation and not cls[3].corridor
    assert not cls[4].relevant and not cls[5].relevant
    assert cls[6].relevant and cls[6].category == "political_unrest" and not cls[6].escalation
    assert cls[1].energy_context and not cls[6].energy_context      # URL slug enrichment only
    assert "Iran" not in json.dumps(sorted(g.CATEGORY_BY_ROOT.values()))


# 4. Goldstein is direction only, never magnitude
def test_goldstein_magnitude_never_changes_any_feature():
    a = [row(eid=i, gold=-10.0) for i in range(1, 6)]
    b = [row(eid=i, gold=-0.1) for i in range(1, 6)]
    agg_a = g.aggregate_batch(list(g.parse_export_lines(a, "20260928020000")))
    agg_b = g.aggregate_batch(list(g.parse_export_lines(b, "20260928020000")))
    assert agg_a == agg_b
    pos = g.aggregate_batch(list(g.parse_export_lines([row(eid=1, gold=3.0)], "20260928020000")))
    assert pos["negative_direction_events"] == 0 and pos["geo_events"] == 1    # sign is a separate, reported dimension


# 5./11. aggregation + duplicates
def test_aggregation_counts_mentions_articles_domains_and_dedups():
    lines = [row(eid=1, mentions=5, url="https://www.reuters.com/a"), row(eid=1, mentions=5, url="https://www.reuters.com/a"),
             row(eid=2, mentions=7, url="https://reuters.com/b"), row(eid=3, mentions=2, url="https://apnews.com/c", root="13", code="130", quad=3, gold=-4.4),
             row(eid=4, root="04", quad=1, gold=1.0, url="https://bbc.co.uk/d")]
    seen = set()
    agg = g.aggregate_batch(list(g.parse_export_lines(lines, "20260928020000")), seen)
    assert agg["total_events"] == 4 and agg["duplicate_events"] == 1
    assert agg["geo_events"] == 3 and agg["geo_mentions"] == 14 and agg["geo_articles"] == 3 and agg["geo_sources"] == 2
    assert agg["categories"] == {"armed_conflict": 2, "threat": 1}
    again = g.aggregate_batch(list(g.parse_export_lines([row(eid=2, added="20260928021500")], "20260928021500")), seen)
    assert again["total_events"] == 0 and again["duplicate_events"] == 1      # cross-batch duplicate ignored


def synthetic_series(start, end, spike_from=None, spike_corridor=60):
    """Deterministic batch series: ordinary conflict noise, plus an optional corridor escalation spike."""
    series = {}
    b = start
    i = 0
    while b <= end:
        base_geo = 30 + (i * 7) % 11
        agg = {"total_events": 1000 + (i * 13) % 97, "geo_events": base_geo, "escalation_events": 8 + (i * 5) % 6,
               "corridor_escalation_events": 2 + (i * 3) % 4, "geo_sources": 12 + (i * 11) % 9}
        if spike_from is not None and b >= spike_from:
            agg["geo_events"] += spike_corridor
            agg["escalation_events"] += spike_corridor
            agg["corridor_escalation_events"] += spike_corridor
            agg["geo_sources"] += 25
        series[b] = agg
        b += timedelta(minutes=15)
        i += 1
    return series


# 6./7. time alignment and no look-ahead
def test_score_never_reads_a_batch_before_it_was_available():
    s = synthetic_series(datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 28, 6, tzinfo=UTC))
    t = datetime(2026, 9, 28, 3, 1, 24, tzinfo=UTC)
    r = g.score_at(s, t)
    assert r["batch_used"] == datetime(2026, 9, 28, 2, 45, tzinfo=UTC).isoformat()   # 02:45 + 15min <= 03:01
    poisoned = dict(s)
    for k in list(poisoned):
        if k + g.AVAILABILITY_LAG > t:
            poisoned[k] = {**poisoned[k], "geo_events": 10 ** 6, "escalation_events": 10 ** 6, "corridor_escalation_events": 10 ** 6}
    assert g.score_at(poisoned, t) == r                                              # future batches cannot move the score
    assert g.score_at(s, datetime(2026, 9, 28, 3, 0, tzinfo=UTC))["batch_used"] == datetime(2026, 9, 28, 2, 45, tzinfo=UTC).isoformat()
    assert g.score_at(s, datetime(2026, 9, 28, 2, 59, 59, tzinfo=UTC))["batch_used"] == datetime(2026, 9, 28, 2, 30, tzinfo=UTC).isoformat()


def test_required_batches_are_minimal_and_never_after_the_cut():
    need = g.required_batches([E])
    assert need[-1] == datetime(2026, 9, 28, 2, 45, tzinfo=UTC)
    assert need[0] == need[-1] - timedelta(hours=73)
    assert len(need) == 73 * 4 + 1
    assert all(b + g.AVAILABILITY_LAG <= E for b in need)
    assert g.export_url(need[0]).startswith("http://data.gdeltproject.org/gdeltv2/") and g.export_url(need[0]).endswith(".export.CSV.zip")


# 8. normalisation is percentile against the feature's own trailing history
def test_normalisation_is_rank_based_and_bounded():
    assert g.percentile_rank(5, [1, 2, 3, 4]) == 100.0
    assert g.percentile_rank(0, [1, 2, 3, 4]) == 0.0
    assert g.percentile_rank(2, [1, 2, 3, 4]) == 37.5
    quiet = synthetic_series(datetime(2026, 9, 24, tzinfo=UTC), E)
    r = g.score_at(quiet, E)
    assert r["status"] == "OK" and 0 <= r["geo_shock_score"] <= 100 and not r["elevated"]
    assert set(r["percentiles"]) == {name for name, _ in g.FEATURES}
    assert r["geo_shock_score"] == sorted(r["percentiles"].values())[1:3][0] or r["geo_shock_score"] == sum(sorted(r["percentiles"].values())[1:3]) / 2


# 9. Event-#15-shaped detection (synthetic: proves the logic, NOT that GDELT saw Event #15)
def test_synthetic_corridor_spike_is_detected_before_the_event_and_persists():
    spike = datetime(2026, 9, 28, 0, 0, tzinfo=UTC)
    s = synthetic_series(datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 28, 12, tzinfo=UTC), spike_from=spike)
    before = g.score_at(s, spike - timedelta(minutes=1))
    at = g.score_at(s, E)
    assert not before["elevated"]
    assert at["status"] == "OK" and at["elevated"] and at["geo_shock_score"] >= g.ELEVATED_PCT
    first = next(t for t in (spike + timedelta(minutes=15 * i) for i in range(16)) if g.score_at(s, t).get("elevated"))
    assert first >= spike + g.AVAILABILITY_LAG                                      # never earlier than availability
    grid = [spike + timedelta(minutes=15 * i) for i in range(4 * 8)]
    assert g.persistence_hours([g.score_at(s, t) for t in grid]) >= 6
    assert g.acceleration(s, datetime(2026, 9, 28, 2, 45, tzinfo=UTC)) > 2


# 10. missing data
def test_missing_batches_are_reported_not_filled():
    s = synthetic_series(datetime(2026, 9, 24, tzinfo=UTC), E)
    holed = {k: v for k, v in s.items() if k != datetime(2026, 9, 28, 2, 30, tzinfo=UTC)}
    assert g.score_at(holed, E)["status"] == "INCOMPLETE_WINDOW"
    short = {k: v for k, v in s.items() if k >= datetime(2026, 9, 27, tzinfo=UTC)}
    assert g.score_at(short, E)["status"] == "INSUFFICIENT_BASELINE"
    assert g.score_at({}, E)["status"] == "NO_DATA"
    stale = {k: v for k, v in s.items() if k <= datetime(2026, 9, 28, 1, 0, tzinfo=UTC)}
    assert g.score_at(stale, E)["status"] == "STALE"


def test_v1_coverage_counts_and_exclusions():
    avail = list(synthetic_series(datetime(2026, 9, 24, tzinfo=UTC), datetime(2026, 9, 28, 6, tzinfo=UTC)))
    ms = lambda d: int(d.timestamp() * 1000)
    obs = [ms(E), ms(datetime(2026, 9, 24, 1, tzinfo=UTC)), ms(datetime(2026, 9, 23, tzinfo=UTC)), ms(datetime(2026, 9, 29, tzinfo=UTC))]
    cov = g.v1_coverage(obs, avail)
    assert cov["v1_observations"] == 4 and cov["aligned"] == 2 and cov["with_sufficient_context"] == 1
    assert cov["excluded"] == {"excluded_no_gdelt_before": 1, "excluded_stale_or_missing_batches": 1,
                               "excluded_incomplete_window": 0, "excluded_insufficient_baseline": 1}
    assert cov["coverage_pct"] == 25.0 and cov["excluded_lookahead_ambiguity"] == 0


def test_master_list_parsing_and_integrity():
    text = ("12345 0123456789abcdef0123456789abcdef http://data.gdeltproject.org/gdeltv2/20260928020000.export.CSV.zip\n"
            "999 aaaa http://data.gdeltproject.org/gdeltv2/20260928020000.mentions.CSV.zip\n garbage line\n")
    assert g.parse_master_list(text) == {"http://data.gdeltproject.org/gdeltv2/20260928020000.export.CSV.zip": (12345, "0123456789abcdef0123456789abcdef")}
    assert g.md5_hex(b"abc") == "900150983cd24fb0d6963f7d28e17f72"


def test_runner_fetch_uses_cache_and_rejects_corrupt_files(tmp_path, monkeypatch):
    b = datetime(2026, 9, 28, 2, 0, tzinfo=UTC)
    data = zip_of([row(eid=1)])
    url = g.export_url(b)
    idx = {url: (len(data), g.md5_hex(data))}
    calls = []
    monkeypatch.setattr(run, "http_get", lambda u, *a, **k: (calls.append(u), (200, data))[1])
    assert run.fetch_batch(b, idx, tmp_path)[0] == "FETCHED"
    assert run.fetch_batch(b, idx, tmp_path)[0] == "CACHED" and len(calls) == 1      # never downloaded twice
    monkeypatch.setattr(run, "http_get", lambda u, *a, **k: (200, b"corrupt"))
    (tmp_path / url.rsplit("/", 1)[-1]).write_bytes(b"corrupt")
    assert run.fetch_batch(b, idx, tmp_path)[0] == "INTEGRITY_FAILED"
    assert run.fetch_batch(b + timedelta(minutes=15), idx, tmp_path)[0] == "NOT_LISTED"
    monkeypatch.setattr(run, "http_get", lambda u, *a, **k: (404, b""))
    assert run.fetch_batch(b, idx, tmp_path)[0] == "MISSING_404"


def test_runner_does_not_fake_success_when_gdelt_is_unreachable(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("CONNECT tunnel failed, response 403")
    monkeypatch.setattr(run, "http_get", boom)
    v1 = [{"ts": int(E.timestamp() * 1000) - 3600 * 1000 * h, "g": 10, "m": 30, "o": 60, "y": 9, "n": 62, "s": 62, "u": 48, "score": 55} for h in range(30, 0, -1)]
    (tmp_path / "v1.json").write_text(json.dumps(v1))
    out = tmp_path / "a.json"
    assert run.main(["--v1", str(tmp_path / "v1.json"), "--cache", str(tmp_path / "c"), "--out", str(out)]) == 2
    a = json.loads(out.read_text())
    assert a["status"] == "LIVE_FETCH_FAILED" and "event15" not in a and a["v1_coverage"]["measured"] is False
    assert a["v1_impact"].startswith("NONE")


# 13. deterministic output
def test_analysis_and_artifact_are_deterministic():
    s = synthetic_series(datetime(2026, 9, 23, tzinfo=UTC), datetime(2026, 9, 29, 6, tzinfo=UTC), spike_from=datetime(2026, 9, 28, tzinfo=UTC))
    v1 = [{"ts": int((E - timedelta(hours=h)).timestamp() * 1000), "g": 10, "m": 30 + h, "o": 60, "y": 9, "n": 62, "s": 62, "u": 48, "score": 55} for h in range(20, -20, -2)]
    a1 = g.dumps_deterministic(run.analyse(s, [], v1, E))
    a2 = g.dumps_deterministic(run.analyse(dict(reversed(list(s.items()))), [], list(v1), E))
    assert a1 == a2
    res = json.loads(a1)
    assert res["detected"] and res["elevated_before_event"]
    # Quiet-period false positives exist (isolated top-decile readings) but stay rare; after the spike every
    # scored point is elevated.
    pts = res["score_series"]
    quiet = [p for p in pts if p["t"] < "2026-09-28T00:15" and p["status"] == "OK"]
    spiked = [p for p in pts if "2026-09-28T00:15" <= p["t"] <= "2026-09-28T06:00" and p["status"] == "OK"]
    assert sum(p["geo_shock_score"] >= g.ELEVATED_PCT for p in quiet) <= 0.05 * len(quiet)
    assert spiked and all(p["geo_shock_score"] >= g.ELEVATED_PCT for p in spiked)
