"""Tests for the Risk Regime research data contract and point-in-time store.

Every observation here is a FIXTURE built for the test; none is, or stands in for, real provider data.
"""
import pytest

import risk_regime_data as d

H = d.HOUR
T0 = 1790000000000 - 1790000000000 % H          # an hour boundary (FIXTURE time)


def obs(ts, value, *, avail=None, source="src", metric="m", live=False, retrieved=T0 + 1000 * H, interval="1h"):
    return d.make_obs(source=source, provider="P", instrument="I", metric=metric, timestamp=ts, value=value, unit="u",
                      interval=interval, retrieved_at=retrieved if not live else ts, historical_or_live=d.LIVE if live else d.HISTORICAL,
                      source_url="https://example.invalid/fixture", available_at=avail if avail is not None else ts + H)


# ---------------- contract ----------------
def test_make_obs_has_every_contract_field():
    o = obs(T0, 1.5)
    assert set(d.FIELDS) <= set(o) and o["raw"] == {}


@pytest.mark.parametrize("kw,msg", [
    ({"timestamp": 1790000000}, "epoch ms"),                       # seconds, not ms
    ({"available_at": T0 - 1}, "precedes"),                        # availability before the observation
    ({"interval": "7m"}, "unknown interval"),
    ({"historical_or_live": "maybe"}, "historical_or_live"),
    ({"value": float("nan")}, "non-finite"),
    ({"value": [1, 2]}, "must be a number"),
])
def test_make_obs_rejects_bad_input(kw, msg):
    base = dict(source="s", provider="p", instrument="i", metric="m", timestamp=T0, value=1.0, unit="u", interval="1h",
                retrieved_at=T0 + H, historical_or_live=d.HISTORICAL, source_url="x", available_at=T0 + H)
    base.update(kw)
    with pytest.raises(d.ContractError, match=msg):
        d.make_obs(**base)


def test_live_snapshot_cannot_be_available_before_retrieval():
    with pytest.raises(d.ContractError, match="live snapshot"):
        d.make_obs(source="s", provider="p", instrument="i", metric="m", timestamp=T0, value=1, unit="u", interval="snapshot",
                   retrieved_at=T0 + H, historical_or_live=d.LIVE, source_url="x", available_at=T0)


# ---------------- store ----------------
def test_duplicates_dropped_and_conflicts_kept_visible():
    st = d.ObservationStore()
    assert st.add([obs(T0, 1.0), obs(T0, 1.0), obs(T0, 2.0), obs(T0 + H, 3.0)]) == 2
    k = ("src", "I", "m")
    assert st.duplicates[k] == 1
    assert st.conflicts[k] == [{"timestamp": T0, "kept": 1.0, "dropped": 2.0}]
    assert [o["value"] for o in st.series(k)] == [1.0, 3.0]


def test_no_lookahead_uses_available_at_not_timestamp():
    st = d.ObservationStore()
    st.add([obs(T0, 1.0), obs(T0 + H, 2.0)])          # each available one hour after its timestamp
    k = ("src", "I", "m")
    assert st.latest_available(k, T0 + H - 1) is None                 # first value not yet published
    assert st.latest_available(k, T0 + H)["value"] == 1.0
    assert st.latest_available(k, T0 + 2 * H - 1)["value"] == 1.0     # second value exists but is not available
    assert st.latest_available(k, T0 + 2 * H)["value"] == 2.0


def test_historical_backfill_judged_on_publication_not_retrieval():
    st = d.ObservationStore()
    st.add([obs(T0, 1.0, retrieved=T0 + 500 * H)])                     # fetched long after, published at T0+1h
    got = d.get_observations_available_at(st, T0 + 2 * H)
    assert got[("src", "I", "m")]["value"] == 1.0 and got[("src", "I", "m")]["age_ms"] == 2 * H


def test_live_snapshot_never_available_for_past_timestamp():
    st = d.ObservationStore()
    st.add([obs(T0 + 10 * H, 9.0, avail=T0 + 10 * H, live=True, interval="snapshot")])
    assert d.get_observations_available_at(st, T0 + 9 * H) == {}
    assert d.get_observations_available_at(st, T0 + 10 * H)


def test_staleness_is_labelled_not_hidden():
    st = d.ObservationStore()
    st.add([obs(T0, 1.0)])
    got = d.get_observations_available_at(st, T0 + 10 * H, max_age_ms={"src": 2 * H})
    assert got[("src", "I", "m")]["stale"] is True and got[("src", "I", "m")]["value"] == 1.0


def test_change_over_returns_none_when_missing():
    st = d.ObservationStore()
    st.add([obs(T0, 100.0), obs(T0 + 24 * H, 110.0)])
    k = ("src", "I", "m")
    c = d.change_over(st, k, T0 + 25 * H, 24 * H)
    assert c["delta"] == 10.0 and c["pct"] == pytest.approx(10.0)
    assert d.change_over(st, k, T0 + 2 * H, 24 * H) is None


# ---------------- quality ----------------
def test_quality_reports_gaps_coverage_and_anomalies():
    st = d.ObservationStore()
    st.add([obs(T0, 1.0), obs(T0 + H, 1.0), obs(T0 + 5 * H, None), obs(T0 + 6 * H, 2.0), obs(T0 + 6 * H, 2.0)])
    q = d.quality(st, ("src", "I", "m"), (T0, T0 + 9 * H))
    assert q["observations"] == 4 and q["expected"] == 10 and q["coverage_pct"] == 40.0 and q["missing_pct"] == 60.0
    assert q["coverage_within_own_range_pct"] == pytest.approx(100 * 4 / 7, abs=0.01)
    assert q["gaps"] == 1 and q["largest_gaps"][0]["missing_intervals"] == 3
    assert q["duplicates_dropped"] == 1 and q["timestamp_anomalies"] == {"null_value": 1}
    assert q["no_lookahead"].startswith("available_at")


def test_quality_flags_misaligned_and_future_timestamps():
    st = d.ObservationStore()
    st.add([obs(T0 + 1, 1.0, retrieved=T0)])         # not on the hour, and later than its retrieval
    q = d.quality(st, ("src", "I", "m"))
    assert q["timestamp_anomalies"] == {"not_on_interval_boundary": 1, "timestamp_after_retrieval": 1}


def test_quality_empty_series():
    assert d.quality(d.ObservationStore(), ("x", "y", "z"))["coverage_status"] == "NO_DATA"
