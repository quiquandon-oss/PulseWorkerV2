"""
Tests for research/eia_source.py (Phase 1 of the Source Dialogue Lab
design -- EIA research adapter ONLY, approved scope).

Never touches the real EIA API -- fetch_eia_series()'s urllib.request
call is always mocked. No API key is ever required to run these tests.
"""
import json
import re
import urllib.error
from datetime import date, datetime, timezone
from unittest.mock import patch

import pytest

import eia_source as es


# ---- 1. Series identification ----

def test_registry_covers_exactly_the_seven_approved_categories():
    assert set(es.EIA_SERIES_REGISTRY.keys()) == set(es.EIA_CATEGORIES)


def test_eia_energy_price_does_not_exist_in_phase_1():
    assert "EIA_ENERGY_PRICE" not in es.EIA_SERIES_REGISTRY
    assert "EIA_ENERGY_PRICE" not in es.EIA_CATEGORIES


def test_every_registry_entry_declares_verified_in_this_session_false():
    """This session could not reach eia.gov/api.eia.gov -- every entry
    must honestly disclose that its series identity/schedule claims are
    not live-verified, never silently imply otherwise."""
    for category, entry in es.EIA_SERIES_REGISTRY.items():
        assert entry["verified_in_this_session"] is False, category


def test_every_registry_entry_declares_temporally_safe_explicitly():
    for category, entry in es.EIA_SERIES_REGISTRY.items():
        assert isinstance(entry["temporally_safe"], bool), category


def test_energy_shock_is_marked_derived_and_not_an_eia_field():
    entry = es.EIA_SERIES_REGISTRY["EIA_ENERGY_SHOCK"]
    assert entry["derived"] is True
    assert entry["derived_from"] == ("EIA_INVENTORY", "EIA_PRODUCTION", "EIA_DEMAND")
    assert entry["series_id_candidate"] is None
    assert "not an eia-published field" in entry["series_reference"].lower() or "NOT an EIA-published field" in entry["series_reference"]


def test_unsafe_categories_carry_an_explicit_unsafe_reason():
    for category, entry in es.EIA_SERIES_REGISTRY.items():
        if not entry["temporally_safe"]:
            assert entry.get("unsafe_reason"), f"{category} is unsafe but has no unsafe_reason"


def test_safe_non_derived_categories_have_a_release_schedule_model():
    for category, entry in es.EIA_SERIES_REGISTRY.items():
        if entry["temporally_safe"] and not entry.get("derived"):
            assert entry["release_schedule"] == "WPSR_WEDNESDAY_1030_ET"


# ---- 2. API response parsing ----

def _fixture_response(rows):
    return json.dumps({
        "response": {
            "total": str(len(rows)),
            "dateFormat": "YYYY-MM-DD",
            "frequency": "weekly",
            "data": rows,
        },
        "request": {"command": "/v2/petroleum/stoc/wstk/data/"},
        "apiVersion": "2.1.0",
    }).encode("utf-8")


def test_parse_eia_response_extracts_data_array():
    rows = [{"period": "2026-01-02", "value": "420.5", "units": "MBBL"}]
    parsed = es.parse_eia_response(_fixture_response(rows))
    assert parsed == rows


def test_parse_eia_response_raises_on_missing_response_key():
    with pytest.raises(RuntimeError):
        es.parse_eia_response(json.dumps({"not_response": {}}).encode())


def test_parse_eia_response_raises_on_missing_data_key():
    with pytest.raises(RuntimeError):
        es.parse_eia_response(json.dumps({"response": {"total": "0"}}).encode())


def test_parse_eia_response_raises_when_data_is_not_a_list():
    with pytest.raises(RuntimeError):
        es.parse_eia_response(json.dumps({"response": {"data": "not-a-list"}}).encode())


# ---- 3. Historical retrieval (fetch_eia_series, network mocked) ----

def test_fetch_eia_series_builds_the_expected_url_and_never_leaks_the_key_on_success():
    captured = {}

    class FakeResponse:
        def read(self):
            return _fixture_response([{"period": "2026-01-02", "value": "1"}])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        return FakeResponse()

    with patch.object(es.urllib.request, "urlopen", side_effect=fake_urlopen):
        result = es.fetch_eia_series("petroleum/stoc/wstk/data/", {"frequency": "weekly"}, "super-secret-key")

    assert "api.eia.gov/v2/petroleum/stoc/wstk/data/" in captured["url"]
    assert "api_key=super-secret-key" in captured["url"]  # sent as a query param, EIA's own required mechanism
    assert result == [{"period": "2026-01-02", "value": "1"}]


def test_fetch_eia_series_never_leaks_the_key_in_an_http_error_message():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", None, None)

    with patch.object(es.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(RuntimeError) as exc_info:
            es.fetch_eia_series("petroleum/stoc/wstk/data/", {}, "super-secret-key")
    assert "super-secret-key" not in str(exc_info.value)


def test_fetch_eia_series_raises_on_network_error():
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    with patch.object(es.urllib.request, "urlopen", side_effect=fake_urlopen):
        with pytest.raises(RuntimeError):
            es.fetch_eia_series("petroleum/stoc/wstk/data/", {}, "k")


# ---- 4. Observation period parsing ----

def test_parse_weekly_period_accepts_a_friday():
    assert es.parse_weekly_period("2026-01-02") == date(2026, 1, 2)


def test_parse_weekly_period_rejects_a_non_friday():
    with pytest.raises(ValueError):
        es.parse_weekly_period("2026-01-01")  # a Thursday


def test_parse_weekly_period_rejects_monthly_format():
    with pytest.raises(ValueError):
        es.parse_weekly_period("2026-01")


# ---- 5. Publication/release timestamp parsing ----

def test_compute_wpsr_release_ts_normal_week_is_wednesday_1030_et():
    ts = es.compute_wpsr_release_ts(date(2026, 1, 2))  # week ending Fri Jan 2, 2026
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    assert dt.year == 2026 and dt.month == 1 and dt.day == 7  # the following Wednesday
    assert dt.hour == 15 and dt.minute == 30  # 10:30am EST = 15:30 UTC in January


def test_compute_wpsr_release_ts_shifts_to_thursday_on_a_holiday_wednesday():
    # Veterans Day 2026-11-11 is a Wednesday -- confirmed via us_federal_holidays(2026)
    holidays = es.us_federal_holidays(2026)
    assert date(2026, 11, 11) in holidays
    assert date(2026, 11, 11).weekday() == 2  # Wednesday
    fri = date(2026, 11, 6)  # week ending the Friday before that Wednesday
    ts = es.compute_wpsr_release_ts(fri)
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    assert dt.day == 12  # shifted to Thursday


def test_compute_wpsr_release_ts_dst_summer_offset():
    ts = es.compute_wpsr_release_ts(date(2026, 7, 3))
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
    assert dt.hour == 14 and dt.minute == 30  # 10:30am EDT = 14:30 UTC in July


def test_compute_wpsr_release_ts_rejects_non_friday_period():
    with pytest.raises(ValueError):
        es.compute_wpsr_release_ts(date(2026, 1, 1))


def test_compute_wpsr_release_ts_refuses_an_unreviewed_year():
    with pytest.raises(ValueError, match="has not been reviewed"):
        es.compute_wpsr_release_ts(date(2029, 1, 5))


# ---- 6. information_available_at correctness ----

def test_normalize_observation_information_available_at_is_after_observation_time():
    obs = es.normalize_observation("EIA_INVENTORY", "2026-01-02", "420.5", collection_ts=1000)
    assert obs["information_available_at"] > obs["observation_time"]


def test_normalize_observation_never_equates_period_with_availability():
    obs = es.normalize_observation("EIA_PRODUCTION", "2026-01-02", "13.2", collection_ts=1000)
    assert obs["observation_period"] == "2026-01-02"
    period_midnight_ms = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)
    assert obs["information_available_at"] != period_midnight_ms
    assert obs["observation_time"] == period_midnight_ms


def test_normalize_observation_returns_exactly_the_required_fields():
    obs = es.normalize_observation("EIA_DEMAND", "2026-01-02", "20.1", collection_ts=1000)
    required = {
        "source_key", "provider", "category", "observation_period",
        "information_available_at", "observation_time", "direction",
        "raw_value", "reference_value", "evidence_reference", "collection_ts",
    }
    assert set(obs.keys()) == required
    assert obs["provider"] == "EIA"
    assert obs["category"] == "EIA_DEMAND"
    assert obs["source_key"] == "eia_demand"


def test_normalize_observation_direction_from_previous_value():
    up = es.normalize_observation("EIA_INVENTORY", "2026-01-02", "500", collection_ts=1, previous_value=400)
    down = es.normalize_observation("EIA_INVENTORY", "2026-01-02", "300", collection_ts=1, previous_value=400)
    flat = es.normalize_observation("EIA_INVENTORY", "2026-01-02", "400", collection_ts=1, previous_value=400)
    none_prev = es.normalize_observation("EIA_INVENTORY", "2026-01-02", "400", collection_ts=1)
    assert up["direction"] == "UP"
    assert down["direction"] == "DOWN"
    assert flat["direction"] == "FLAT"
    assert none_prev["direction"] is None


# ---- 7. Future-information rejection ----

def test_normalize_observation_raises_if_availability_would_not_be_after_observation_time():
    """Structural guard: even though the current implementation can
    never naturally produce this, the check itself must exist and fire
    correctly against a directly-forced violation."""
    with patch.object(es, "compute_wpsr_release_ts", return_value=0):
        with pytest.raises(RuntimeError, match="look-ahead"):
            es.normalize_observation("EIA_INVENTORY", "2026-01-02", "1", collection_ts=1)


# ---- 8. Missing publication timestamp handling ----

def test_normalize_observation_refuses_temporally_unsafe_oil():
    with pytest.raises(ValueError, match="not TEMPORALLY_SAFE"):
        es.normalize_observation("EIA_OIL", "2026-01-02", "70.5", collection_ts=1)


def test_normalize_observation_refuses_temporally_unsafe_gas():
    with pytest.raises(ValueError, match="not TEMPORALLY_SAFE"):
        es.normalize_observation("EIA_GAS", "2026-01-02", "3.1", collection_ts=1)


def test_normalize_observation_refuses_temporally_unsafe_electricity():
    with pytest.raises(ValueError, match="not TEMPORALLY_SAFE"):
        es.normalize_observation("EIA_ELECTRICITY", "2026-01-02", "13.4", collection_ts=1)


def test_normalize_observation_refuses_unknown_category():
    with pytest.raises(ValueError, match="Unknown EIA category"):
        es.normalize_observation("EIA_MADE_UP", "2026-01-02", "1", collection_ts=1)


def test_normalize_observation_refuses_derived_category_directly():
    with pytest.raises(ValueError, match="derived"):
        es.normalize_observation("EIA_ENERGY_SHOCK", "2026-01-02", "1", collection_ts=1)


# ---- 9. Derived EIA_ENERGY_SHOCK marking ----

def _safe_inputs(period="2026-01-02", directions=("UP", "UP", "UP")):
    cats = ("EIA_INVENTORY", "EIA_PRODUCTION", "EIA_DEMAND")
    prev_map = {"UP": 100, "DOWN": 900, "FLAT": 500}
    out = {}
    for cat, d in zip(cats, directions):
        val = {"UP": 500, "DOWN": 100, "FLAT": 500}[d]
        out[cat] = es.normalize_observation(cat, period, str(val), collection_ts=1, previous_value=prev_map[d])
    return out


def test_normalize_derived_energy_shock_marks_derived_true_and_discloses_inputs():
    inputs = _safe_inputs()
    obs = es.normalize_derived_energy_shock("2026-01-02", inputs, collection_ts=2)
    ref = json.loads(obs["reference_value"])
    assert ref["derived"] is True
    assert set(ref["derived_from"]) == {"EIA_INVENTORY", "EIA_PRODUCTION", "EIA_DEMAND"}
    assert obs["category"] == "EIA_ENERGY_SHOCK"
    assert obs["raw_value"] is None


def test_normalize_derived_energy_shock_availability_is_max_of_inputs():
    inputs = _safe_inputs()
    obs = es.normalize_derived_energy_shock("2026-01-02", inputs, collection_ts=2)
    expected_max = max(i["information_available_at"] for i in inputs.values())
    assert obs["information_available_at"] == expected_max


def test_normalize_derived_energy_shock_direction_requires_unanimous_agreement():
    all_up = es.normalize_derived_energy_shock("2026-01-02", _safe_inputs(directions=("UP", "UP", "UP")), collection_ts=1)
    mixed = es.normalize_derived_energy_shock("2026-01-02", _safe_inputs(directions=("UP", "DOWN", "UP")), collection_ts=1)
    assert all_up["direction"] == "UP"
    assert mixed["direction"] is None


def test_normalize_derived_energy_shock_raises_on_missing_input():
    inputs = _safe_inputs()
    del inputs["EIA_DEMAND"]
    with pytest.raises(ValueError, match="missing required inputs"):
        es.normalize_derived_energy_shock("2026-01-02", inputs, collection_ts=1)


def test_normalize_derived_energy_shock_raises_on_mismatched_period():
    inputs = _safe_inputs(period="2026-01-02")
    inputs["EIA_DEMAND"] = es.normalize_observation("EIA_DEMAND", "2026-01-09", "1", collection_ts=1)
    with pytest.raises(ValueError, match="observation_period"):
        es.normalize_derived_energy_shock("2026-01-02", inputs, collection_ts=1)


# ---- 10. No production-table mutation ----

def test_no_sql_write_statements_anywhere_in_this_module():
    """This module issues zero SQL of any kind -- confirmed by the
    absence of any write-statement shape, not by banning the plain
    English words "history"/"predictions"/etc., which legitimately
    appear in this module's own docstring disclosure of what it never
    writes to (the exact test-design mistake already caught and fixed
    once this session for exp009-event-source-evidence)."""
    with open(es.__file__.replace(".pyc", ".py")) as f:
        src = f.read()
    assert not re.search(r"\b(INSERT INTO|UPDATE\s+\w+\s+SET|DELETE FROM)\b", src, re.IGNORECASE)
    for table in ("history", "btc_data", "predictions", "selection_decisions", "research_analyses"):
        assert not re.search(rf"\b(INSERT INTO|UPDATE|DELETE FROM)\s+{table}\b", src, re.IGNORECASE)


# ---- 11. No V1 source mutation ----

def test_no_registry_key_collides_with_an_existing_v1_source_key():
    """Real production V1 source keys (from history.sources_json), so a
    future EIA/GDELT category can never silently collide with, or be
    confused for, an existing V1 source in any downstream join."""
    v1_source_keys = {
        "fng", "funding", "longshort", "global", "cryptonews", "macrogeo",
        "geopolitics", "regulatory", "sosovalue", "onchain", "oil",
        "yield10y", "usd", "nasdaq", "sp500", "ninemag", "foufi",
        "etfflows", "hypefunding", "gold", "strc",
    }
    for category in es.EIA_SERIES_REGISTRY:
        assert category.lower() not in v1_source_keys
        assert category.startswith("EIA_")


def test_module_never_references_history_sources_json_write_path():
    with open(es.__file__.replace(".pyc", ".py")) as f:
        src = f.read()
    assert "sources_json" not in src


def test_verify_registry_before_first_production_use_is_a_hard_stop():
    """This session could not live-verify the registry -- this guard
    must still raise until a human has actually done so."""
    with pytest.raises(NotImplementedError):
        es.verify_registry_before_first_production_use()
