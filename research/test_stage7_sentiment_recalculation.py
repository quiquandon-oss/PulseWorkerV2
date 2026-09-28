"""
Tests for research/stage7_sentiment_recalculation.py.

Run with: python3 -m pytest research/test_stage7_sentiment_recalculation.py -v
"""
import hashlib
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import stage7_sentiment_recalculation as sr  # noqa: E402

EVENT = {"event_id": 42, "event_ts": 1_000_000}


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE history (id INTEGER PRIMARY KEY, ts INTEGER, score INTEGER, "
                 "technical_score INTEGER, sources_json TEXT)")
    return conn


def test_fetch_v1_macro_context_reads_nearest_row_at_or_before_event_ts():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 900000, 55, 60, '{\"fng\": 40}')")
    conn.execute("INSERT INTO history VALUES (2, 1100000, 70, 65, '{\"fng\": 80}')")  # after event, must be ignored
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is True
    assert result["observation_ts"] == 900000
    assert result["composite_score"] == 55
    assert result["sources_snapshot"] == {"fng": 40}


def test_fetch_v1_macro_context_no_row_before_event_is_honestly_unavailable():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 2000000, 55, 60, '{}')")
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is False


def test_fetch_v1_macro_context_malformed_sources_json_degrades_to_empty_dict():
    conn = _fresh_db()
    conn.execute("INSERT INTO history VALUES (1, 900000, 55, 60, 'not-json')")
    conn.commit()
    result = sr.fetch_v1_macro_context(conn, EVENT["event_ts"])
    assert result["available"] is True
    assert result["sources_snapshot"] == {}


def test_dedupe_evidence_no_duplicates_passthrough():
    rows = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 2
    assert groups == {}


def test_dedupe_evidence_keeps_first_seen_and_reports_group():
    rows = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "a"}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 1
    assert deduped[0]["evidence_id"] == 1
    assert groups["a"]["kept_evidence_id"] == 1
    assert groups["a"]["dropped_evidence_ids"] == [2]


def test_dedupe_evidence_missing_content_hash_never_discarded():
    rows = [{"evidence_id": 1, "content_hash": None}, {"evidence_id": 2, "content_hash": None}]
    deduped, groups = sr.dedupe_evidence(rows)
    assert len(deduped) == 2
    assert groups == {}


def test_compute_event_sentiment_no_response_leaves_sentiment_null_even_when_sufficient():
    v1_ctx = {"available": True, "observation_ts": 900000, "composite_score": 55}
    result = sr.compute_event_sentiment(
        EVENT, [{"evidence_id": 1, "content_hash": "a"}], {"cryptonews": "EVENT_SOURCE_ALIGNED"},
        "SUFFICIENT", v1_ctx, validated_response=None,
    )
    assert result["sentiment_label"] is None
    assert result["sentiment_score"] is None
    assert result["evidence_sufficiency"] == "SUFFICIENT"
    assert result["contributing_evidence_ids"] == [1]


def test_compute_event_sentiment_unvalidated_response_never_sets_a_score():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "PENDING",
                "findings": {"sentiment_assessment": "POSITIVE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "INSUFFICIENT", v1_ctx, validated_response=response)
    assert result["sentiment_label"] is None
    assert result["sentiment_score"] is None
    assert result["ai_research_response_id"] is None


def test_compute_event_sentiment_validated_positive_response_sets_score_100():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "VALIDATED",
                "findings": {"sentiment_assessment": "POSITIVE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "INSUFFICIENT_EVIDENCE", v1_ctx, validated_response=response)
    assert result["sentiment_label"] == "POSITIVE"
    assert result["sentiment_score"] == 100.0
    assert result["ai_research_response_id"] == "r1"


def test_compute_event_sentiment_validated_indeterminate_leaves_score_null():
    v1_ctx = {"available": False}
    response = {"response_id": "r1", "validation_status": "VALIDATED",
                "findings": {"sentiment_assessment": "INDETERMINATE"}}
    result = sr.compute_event_sentiment(EVENT, [], {}, "CONFLICTING", v1_ctx, validated_response=response)
    assert result["sentiment_label"] == "INDETERMINATE"
    assert result["sentiment_score"] is None


def test_compute_event_sentiment_full_source_set_includes_both_stage6_and_stage7_evidence():
    # "Never calculate using only newly discovered sources" -- both an
    # original Stage 6 row and a hypothetical Stage-7-added row must
    # both appear in contributing_evidence_ids.
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}]
    result = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert set(result["contributing_evidence_ids"]) == {1, 2}


def test_compute_event_sentiment_duplicate_evidence_excluded_with_reason():
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "a"}]
    result = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert result["contributing_evidence_ids"] == [1]
    assert result["excluded_evidence"] == [{"evidence_id": 2, "reason": "duplicate content_hash of evidence_id 1"}]


def test_input_fingerprint_stable_for_same_inputs():
    a = sr.compute_input_fingerprint(1, [3, 2, 1], "SUFFICIENT", None)
    b = sr.compute_input_fingerprint(1, [1, 2, 3], "SUFFICIENT", None)  # order must not matter
    assert a == b


def test_input_fingerprint_changes_when_evidence_changes():
    a = sr.compute_input_fingerprint(1, [1, 2], "SUFFICIENT", None)
    b = sr.compute_input_fingerprint(1, [1, 2, 3], "SUFFICIENT", None)
    assert a != b


def test_is_idempotent_repeat_true_for_identical_fingerprint():
    v1_ctx = {"available": False}
    evidence = [{"evidence_id": 1, "content_hash": "a"}]
    first = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    second = sr.compute_event_sentiment(EVENT, evidence, {}, "SUFFICIENT", v1_ctx)
    assert sr.is_idempotent_repeat(second, first) is True


def test_is_idempotent_repeat_false_when_evidence_changed():
    v1_ctx = {"available": False}
    first = sr.compute_event_sentiment(EVENT, [{"evidence_id": 1, "content_hash": "a"}], {}, "SUFFICIENT", v1_ctx)
    second = sr.compute_event_sentiment(
        EVENT, [{"evidence_id": 1, "content_hash": "a"}, {"evidence_id": 2, "content_hash": "b"}],
        {}, "SUFFICIENT", v1_ctx,
    )
    assert sr.is_idempotent_repeat(second, first) is False


def test_is_idempotent_repeat_false_when_no_previous_result():
    v1_ctx = {"available": False}
    result = sr.compute_event_sentiment(EVENT, [], {}, "SUFFICIENT", v1_ctx)
    assert sr.is_idempotent_repeat(result, None) is False


# =====================================================================
# Adversarial-review remediation, finding #4: validated Stage 7 response
# sources (stage7_research_responses.sources_json) must actually enter
# the full-source recalculation, not just be stored inertly.
# =====================================================================

CUTOFF = EVENT["event_ts"]  # 1_000_000 -- same historical cutoff convention run_stage7.py uses


def _validated_response(sources, response_id="stage7-resp-r1", assessment="POSITIVE"):
    return {
        "response_id": response_id, "validation_status": "VALIDATED",
        "findings": {"sentiment_assessment": assessment}, "sources": sources,
    }


class TestParseSourcePublicationTs:
    def test_numeric_ms_epoch_passthrough(self):
        assert sr._parse_source_publication_ts(500000) == 500000
        assert sr._parse_source_publication_ts(500000.0) == 500000

    def test_iso_date_string_treated_as_utc_midnight(self):
        # 1970-01-01T00:16:40Z == 1_000_000 ms -- a bare ISO date (no time
        # component) must be treated as UTC midnight, never the server's
        # local timezone (which would make this test's own result depend
        # on where it runs).
        assert sr._parse_source_publication_ts("1970-01-01") == 0

    def test_iso_datetime_string_with_explicit_offset(self):
        # 1970-01-01T00:16:40+00:00 == exactly 1_000_000 ms.
        assert sr._parse_source_publication_ts("1970-01-01T00:16:40+00:00") == 1_000_000

    def test_naive_iso_datetime_treated_as_utc_not_local(self):
        assert sr._parse_source_publication_ts("1970-01-01T00:16:40") == 1_000_000

    def test_unparseable_string_is_none(self):
        assert sr._parse_source_publication_ts("not a date") is None

    def test_missing_or_blank_is_none(self):
        assert sr._parse_source_publication_ts(None) is None
        assert sr._parse_source_publication_ts("") is None
        assert sr._parse_source_publication_ts("   ") is None

    def test_wrong_type_is_none_never_coerced(self):
        assert sr._parse_source_publication_ts(True) is None  # bool is not a timestamp
        assert sr._parse_source_publication_ts([1, 2]) is None
        assert sr._parse_source_publication_ts({"ts": 1}) is None


class TestNormalizeResponseSources:
    def test_valid_source_is_normalized_with_synthetic_id_and_url_only_hash(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "https://example.com/a", "publisher": "X", "publication_date": 100, "claim": "c"}],
            historical_cutoff_ts=CUTOFF,
        )
        assert excluded == []
        assert len(normalized) == 1
        row = normalized[0]
        assert row["evidence_id"] == "stage7-source-r1-0"
        assert row["origin"] == "stage7_response_source"
        assert row["response_id"] == "r1"
        assert row["publication_ts"] == 100
        assert row["content_hash"] == hashlib.sha256(b"https://example.com/a").hexdigest()

    def test_missing_url_excluded(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"publisher": "X", "publication_date": 100}], historical_cutoff_ts=CUTOFF
        )
        assert normalized == []
        assert excluded == [{"evidence_id": "stage7-source-r1-0", "reason": "missing a verifiable url"}]

    def test_blank_url_excluded(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "   ", "publication_date": 100}], historical_cutoff_ts=CUTOFF
        )
        assert normalized == []
        assert excluded[0]["reason"] == "missing a verifiable url"

    def test_non_dict_entry_excluded(self):
        normalized, excluded = sr.normalize_response_sources("r1", ["not-a-dict"], historical_cutoff_ts=CUTOFF)
        assert normalized == []
        assert excluded == [{"evidence_id": "stage7-source-r1-0", "reason": "malformed source record (not an object)"}]

    def test_missing_publication_date_excluded(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "https://example.com/a"}], historical_cutoff_ts=CUTOFF
        )
        assert normalized == []
        assert excluded[0]["reason"] == "publication date is missing or unparseable"

    def test_unparseable_publication_date_excluded(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "https://example.com/a", "publication_date": "garbage"}], historical_cutoff_ts=CUTOFF
        )
        assert excluded[0]["reason"] == "publication date is missing or unparseable"

    def test_publication_after_cutoff_excluded(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "https://example.com/a", "publication_date": CUTOFF + 1}],
            historical_cutoff_ts=CUTOFF,
        )
        assert normalized == []
        assert excluded == [{"evidence_id": "stage7-source-r1-0", "reason": "published after the historical cutoff"}]

    def test_publication_exactly_at_cutoff_is_eligible(self):
        normalized, excluded = sr.normalize_response_sources(
            "r1", [{"url": "https://example.com/a", "publication_date": CUTOFF}],
            historical_cutoff_ts=CUTOFF,
        )
        assert excluded == []
        assert len(normalized) == 1

    def test_independent_corroboration_different_urls_same_claim_both_survive(self):
        # Two DIFFERENT sources reporting the SAME claim must never be
        # collapsed -- dedup is by url only, never by claim text.
        normalized, excluded = sr.normalize_response_sources(
            "r1", [
                {"url": "https://a.example.com/x", "publication_date": 100, "claim": "price moved because of X"},
                {"url": "https://b.example.com/y", "publication_date": 200, "claim": "price moved because of X"},
            ],
            historical_cutoff_ts=CUTOFF,
        )
        assert excluded == []
        assert len(normalized) == 2
        assert normalized[0]["content_hash"] != normalized[1]["content_hash"]

    def test_url_normalized_case_and_whitespace_insensitively_for_hashing(self):
        normalized, _ = sr.normalize_response_sources(
            "r1", [{"url": "  HTTPS://Example.com/A  ", "publication_date": 100}], historical_cutoff_ts=CUTOFF
        )
        assert normalized[0]["content_hash"] == hashlib.sha256(b"https://example.com/a").hexdigest()

    def test_first_rule_reported_when_multiple_would_fail(self):
        # Missing url AND missing date -- only the url reason is reported
        # (checked first), never both stacked.
        _, excluded = sr.normalize_response_sources("r1", [{}], historical_cutoff_ts=CUTOFF)
        assert excluded == [{"evidence_id": "stage7-source-r1-0", "reason": "missing a verifiable url"}]

    def test_empty_sources_list_and_none_both_produce_no_rows(self):
        assert sr.normalize_response_sources("r1", [], historical_cutoff_ts=CUTOFF) == ([], [])
        assert sr.normalize_response_sources("r1", None, historical_cutoff_ts=CUTOFF) == ([], [])


class TestComputeEventSentimentWithResponseSources:
    def test_validated_source_enters_contributing_evidence_ids(self):
        response = _validated_response([{"url": "https://example.com/a", "publication_date": 100}])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
        )
        assert result["contributing_evidence_ids"] == ["stage7-source-stage7-resp-r1-0"]

    def test_unvalidated_response_sources_never_enter_evidence_or_counts(self):
        response = {
            "response_id": "stage7-resp-r1", "validation_status": "PENDING",
            "findings": {"sentiment_assessment": "POSITIVE"},
            "sources": [{"url": "https://example.com/a", "publication_date": 100}],
        }
        result = sr.compute_event_sentiment(
            EVENT, [{"evidence_id": 1, "content_hash": "a"}], {}, "INSUFFICIENT", {"available": False},
            validated_response=response,
        )
        assert result["contributing_evidence_ids"] == [1]  # only the real Stage 6 row
        assert result["excluded_evidence"] == []
        assert result["sentiment_label"] is None  # unvalidated -- Step H's own rule, unaffected by this fix

    def test_full_source_set_includes_stage6_and_validated_stage7_sources_together(self):
        response = _validated_response([{"url": "https://example.com/a", "publication_date": 100}])
        result = sr.compute_event_sentiment(
            EVENT, [{"evidence_id": 1, "content_hash": "a"}], {}, "SUFFICIENT", {"available": False},
            validated_response=response,
        )
        assert set(result["contributing_evidence_ids"]) == {1, "stage7-source-stage7-resp-r1-0"}

    def test_duplicate_stage7_sources_deduped_deterministically_first_kept(self):
        response = _validated_response([
            {"url": "https://example.com/a", "publication_date": 100},
            {"url": "https://example.com/a", "publication_date": 200},  # same url, different index
        ])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
        )
        assert result["contributing_evidence_ids"] == ["stage7-source-stage7-resp-r1-0"]
        assert result["excluded_evidence"] == [{
            "evidence_id": "stage7-source-stage7-resp-r1-1",
            "reason": "duplicate content_hash of evidence_id stage7-source-stage7-resp-r1-0",
        }]

    def test_independent_corroboration_preserved_through_full_pipeline(self):
        response = _validated_response([
            {"url": "https://a.example.com/x", "publication_date": 100, "claim": "same claim"},
            {"url": "https://b.example.com/y", "publication_date": 200, "claim": "same claim"},
        ])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
        )
        assert len(result["contributing_evidence_ids"]) == 2  # neither collapsed into the other

    def test_contradictory_sources_both_represented_not_filtered(self):
        # Two sources with directly contradictory claims -- neither is
        # dropped on the basis of disagreeing with the other; claim
        # content never drives inclusion/exclusion.
        response = _validated_response([
            {"url": "https://a.example.com/x", "publication_date": 100, "claim": "the move was bullish"},
            {"url": "https://b.example.com/y", "publication_date": 200, "claim": "the move was bearish"},
        ])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "CONFLICTING", {"available": False}, validated_response=response,
        )
        assert len(result["contributing_evidence_ids"]) == 2

    def test_malformed_and_future_dated_sources_excluded_with_reasons_alongside_valid_ones(self):
        response = _validated_response([
            {"url": "https://example.com/good", "publication_date": 100},
            {"url": "https://example.com/future", "publication_date": CUTOFF + 1},
            {"publication_date": 100},  # no url
        ])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
            historical_cutoff_ts=CUTOFF,
        )
        assert result["contributing_evidence_ids"] == ["stage7-source-stage7-resp-r1-0"]
        reasons = {e["evidence_id"]: e["reason"] for e in result["excluded_evidence"]}
        assert reasons["stage7-source-stage7-resp-r1-1"] == "published after the historical cutoff"
        assert reasons["stage7-source-stage7-resp-r1-2"] == "missing a verifiable url"

    def test_historical_cutoff_defaults_to_event_ts_when_not_passed_explicitly(self):
        response = _validated_response([{"url": "https://example.com/a", "publication_date": EVENT["event_ts"] + 1}])
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
            # historical_cutoff_ts omitted -- must fall back to event["event_ts"]
        )
        assert result["contributing_evidence_ids"] == []
        assert result["excluded_evidence"][0]["reason"] == "published after the historical cutoff"

    def test_provenance_fields_identify_stage7_sources_distinctly_from_stage6_rows(self):
        response = _validated_response([{"url": "https://example.com/a", "publication_date": 100}])
        result = sr.compute_event_sentiment(
            EVENT, [{"evidence_id": 1, "content_hash": "zzz"}], {}, "SUFFICIENT", {"available": False},
            validated_response=response,
        )
        ids = result["contributing_evidence_ids"]
        assert 1 in ids  # Stage 6's own integer id, unchanged
        assert "stage7-source-stage7-resp-r1-0" in ids  # Stage 7's own string id, self-describing

    def test_sentiment_assessment_and_source_evidence_are_independently_gated(self):
        # An unrecognized sentiment_assessment must not suppress source
        # inclusion, and vice versa -- these are two distinct concerns.
        response = _validated_response(
            [{"url": "https://example.com/a", "publication_date": 100}], assessment="BULLISH",  # not a real label
        )
        result = sr.compute_event_sentiment(
            EVENT, [], {}, "SUFFICIENT", {"available": False}, validated_response=response,
        )
        assert result["sentiment_label"] is None  # unrecognized assessment -- never fabricated
        assert result["ai_research_response_id"] == "stage7-resp-r1"  # still linked -- provenance is separate
        assert result["contributing_evidence_ids"] == ["stage7-source-stage7-resp-r1-0"]  # sources unaffected

    def test_v1_sources_never_appear_in_contributing_evidence_ids(self):
        # V1's composite is context only (v1_macro_context) -- it must
        # never be folded into the contributing/full-source set.
        v1_ctx = {"available": True, "observation_ts": 900000, "composite_score": 55,
                  "sources_snapshot": {"fng": 40, "funding": 12}}
        response = _validated_response([{"url": "https://example.com/a", "publication_date": 100}])
        result = sr.compute_event_sentiment(
            EVENT, [{"evidence_id": 1, "content_hash": "a"}], {}, "SUFFICIENT", v1_ctx,
            validated_response=response,
        )
        for source_key in ("fng", "funding"):
            assert source_key not in result["contributing_evidence_ids"]
        assert result["v1_macro_context"] == v1_ctx  # reported separately, verbatim
