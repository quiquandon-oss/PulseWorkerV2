"""
Tests for research/event_source_evidence_join.py.

Run with: pytest research/test_event_source_evidence_join.py

These tests never touch production D1. `classify_event_source_
interpretation()` is tested exhaustively as a pure function (no
database). `build_event_source_evidence_dataset()` is tested
end-to-end against a real in-memory sqlite fixture carrying all five
tables the reused modules require (history, btc_data, predictions,
research_events, research_event_evidence) -- this proves the join
genuinely works against the real schemas, not just plausible-looking
mocks.
"""
import json
import os
import sqlite3
import sys

_HERE = os.path.dirname(__file__)
sys.path.insert(0, _HERE)

import event_source_evidence_join as join_module  # noqa: E402
import event_detector as ed  # noqa: E402
import evidence_collector as ec  # noqa: E402


# =====================================================================
# classify_event_source_interpretation -- exhaustive decision-table tests
# =====================================================================

def test_insufficient_relevance_always_wins_first():
    assert join_module.classify_event_source_interpretation(
        "INSUFFICIENT_EVIDENCE", "ALIGNED", "REACTION_ABOVE_CONTROL"
    ) == "INSUFFICIENT_EVIDENCE"


def test_insufficient_alignment_or_control_is_insufficient_evidence():
    assert join_module.classify_event_source_interpretation(
        "RELEVANT", "INSUFFICIENT_EVIDENCE", "REACTION_ABOVE_CONTROL"
    ) == "INSUFFICIENT_EVIDENCE"
    assert join_module.classify_event_source_interpretation(
        "RELEVANT", "ALIGNED", "INSUFFICIENT_EVIDENCE"
    ) == "INSUFFICIENT_EVIDENCE"


def test_relevant_aligned_genuine_reaction_is_event_source_aligned():
    for relevance in ("RELEVANT", "POSSIBLY_RELEVANT"):
        for control in ("REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL"):
            assert join_module.classify_event_source_interpretation(
                relevance, "ALIGNED", control
            ) == "EVENT_SOURCE_ALIGNED"


def test_relevant_not_aligned_genuine_reaction_is_misleading_possible():
    for control in ("REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL"):
        assert join_module.classify_event_source_interpretation(
            "RELEVANT", "NOT_ALIGNED", control
        ) == "EVENT_SOURCE_MISLEADING_POSSIBLE"


def test_relevant_mixed_or_no_measurable_alignment_with_genuine_control_reaction_is_not_aligned():
    for pr1_alignment in ("MIXED", "NO_MEASURABLE_REACTION"):
        assert join_module.classify_event_source_interpretation(
            "RELEVANT", pr1_alignment, "REACTION_ABOVE_CONTROL"
        ) == "EVENT_SOURCE_NOT_ALIGNED"


def test_relevant_but_no_measurable_control_reaction_is_informational_only():
    for control in ("NO_MEASURABLE_REACTION", "CONTINUATION_CONSISTENT"):
        for pr1_alignment in ("ALIGNED", "NOT_ALIGNED", "MIXED", "NO_MEASURABLE_REACTION"):
            assert join_module.classify_event_source_interpretation(
                "POSSIBLY_RELEVANT", pr1_alignment, control
            ) == "EVENT_SOURCE_INFORMATIONAL_ONLY"


def test_not_established_aligned_genuine_reaction_is_redundant_possible():
    for control in ("REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL"):
        assert join_module.classify_event_source_interpretation(
            "NOT_ESTABLISHED", "ALIGNED", control
        ) == "EVENT_SOURCE_REDUNDANT_POSSIBLE"


def test_not_established_without_aligned_genuine_reaction_is_insufficient_evidence():
    assert join_module.classify_event_source_interpretation(
        "NOT_ESTABLISHED", "NOT_ALIGNED", "REACTION_ABOVE_CONTROL"
    ) == "INSUFFICIENT_EVIDENCE"
    assert join_module.classify_event_source_interpretation(
        "NOT_ESTABLISHED", "ALIGNED", "CONTINUATION_CONSISTENT"
    ) == "INSUFFICIENT_EVIDENCE"
    assert join_module.classify_event_source_interpretation(
        "NOT_ESTABLISHED", "MIXED", "NO_MEASURABLE_REACTION"
    ) == "INSUFFICIENT_EVIDENCE"


def test_every_returned_label_is_a_declared_interpretation():
    relevances = ("RELEVANT", "POSSIBLY_RELEVANT", "NOT_ESTABLISHED", "INSUFFICIENT_EVIDENCE")
    alignments = ("ALIGNED", "NOT_ALIGNED", "MIXED", "NO_MEASURABLE_REACTION", "INSUFFICIENT_EVIDENCE")
    controls = ("CONTINUATION_CONSISTENT", "REACTION_ABOVE_CONTROL", "REACTION_BELOW_CONTROL",
                "NO_MEASURABLE_REACTION", "INSUFFICIENT_EVIDENCE")
    for relevance in relevances:
        for alignment in alignments:
            for control in controls:
                result = join_module.classify_event_source_interpretation(relevance, alignment, control)
                assert result in join_module.EVENT_SOURCE_INTERPRETATIONS, (
                    f"unexpected label {result!r} for ({relevance}, {alignment}, {control})"
                )


def test_classification_never_uses_a_causal_word_in_its_own_label_set():
    # Deterministic textual guard: the label vocabulary itself must
    # never claim causation ("caused", "predicts", "because").
    for label in join_module.EVENT_SOURCE_INTERPRETATIONS:
        lowered = label.lower()
        assert "caus" not in lowered
        assert "predict" not in lowered


# =====================================================================
# End-to-end: build_event_source_evidence_dataset against a real fixture
# =====================================================================

def _build_fixture_conn():
    """A real sqlite connection with the exact schemas
    event_detector.py / evidence_collector.py / event_source_reaction.py
    / controlled_event_reaction.py / event_source_relevance.py all
    require: history, btc_data, predictions, research_events,
    research_event_evidence."""
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE history (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "score INTEGER, sources_json TEXT, gold_regime TEXT)"
    )
    conn.execute("CREATE TABLE btc_data (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL)")
    conn.execute(
        "CREATE TABLE predictions (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "horizon_hours INTEGER, p_up REAL, realized_up INTEGER)"
    )
    conn.execute(
        "CREATE TABLE research_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL, "
        "event_ts INTEGER NOT NULL, detection_ts INTEGER NOT NULL, category TEXT NOT NULL, direction TEXT, "
        "intensity REAL, available_before_prediction INTEGER NOT NULL, is_post_event_analysis INTEGER NOT NULL DEFAULT 0, "
        "trigger_metric TEXT, trigger_threshold REAL, trigger_version TEXT)"
    )
    conn.execute(
        "CREATE TABLE research_event_evidence (evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "event_id INTEGER NOT NULL REFERENCES research_events(event_id), feed_url TEXT NOT NULL, "
        "article_url TEXT NOT NULL, publisher TEXT NOT NULL, publication_ts INTEGER NOT NULL, "
        "collection_ts INTEGER NOT NULL, headline TEXT NOT NULL, keyword_score REAL, "
        "evidence_relation TEXT NOT NULL, content_hash TEXT NOT NULL)"
    )
    conn.commit()
    return conn


def _seed_large_move_with_evidence(conn):
    """Seeds enough btc_data/history to trigger exactly one LARGE_MOVE
    event (a real PR3 detector run, not a fabricated event row), plus a
    real research_events row at the SAME event_ts (required for
    event_source_relevance.fetch_evidence_for_event_ts's exact-ts join)
    and a matching research_event_evidence row with a geopolitics-topic
    feed_url and publication_ts strictly before event_ts, so that the
    'geopolitics' source (a real SOURCE_TOPIC_AFFINITY entry) reaches
    RELEVANT.

    Uses exactly ONE clean, day-aligned btc_data reading per calendar
    day (not continuous hourly data): _resample_daily() keeps only the
    FIRST reading of each UTC calendar day, so a continuous hourly
    series would already have an earlier same-day flat reading before
    any deliberately-placed "jump" tick later that day, silently
    discarding the jump during resampling (confirmed empirically while
    building this fixture). One reading per day sidesteps that
    ambiguity entirely -- each day's single reading is trivially its
    own "first reading."
    """
    from datetime import datetime, timezone
    base_ts = int(datetime(2024, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
    hour = 3_600_000
    day = 24 * hour

    # 9 clean daily flat readings, then a >4% jump on day 10.
    price = 50000.0
    for i in range(9):
        conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (base_ts + i * day, price))
    jump_event_ts = base_ts + 9 * day
    price = price * 1.05  # +5% > LARGE_MOVE_THRESHOLD_PCT
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (jump_event_ts, price))
    # A few genuine post-event ticks (raw btc_data, not part of the
    # daily-resampled event-detection series -- resolve_horizon_reaction
    # queries btc_data directly) so 6h/12h/24h horizons can actually
    # resolve with real reaction data, not just NO_DATA structure.
    for offset_hours, extra_pct in ((1, 0.0), (6, 0.5), (12, 1.0), (24, 2.0)):
        conn.execute(
            "INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)",
            (jump_event_ts + offset_hours * hour, price * (1 + extra_pct / 100.0)),
        )
    conn.commit()

    start_ts = base_ts
    end_ts = jump_event_ts + 25 * hour
    events = ed.detect_large_moves(conn, start_ts, end_ts)
    assert len(events) == 1, "fixture must deterministically trigger exactly one LARGE_MOVE event"
    event_ts = events[0]["event_ts"]

    # A V1 history row at/just before event_ts with a real 'geopolitics'
    # reading, clearly above what will become this source's own median.
    for i, h_ts in enumerate(range(base_ts, event_ts + 1, 6 * hour)):
        geopolitics_value = 20 + (i % 3)  # low baseline
        conn.execute(
            "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
            (h_ts, 50, json.dumps({"geopolitics": geopolitics_value}), "chop"),
        )
    # One clearly-elevated reading right at event_ts so source_value sits
    # well above its own median (self-normalizing convention, per
    # event_source_reaction.py's own docstring).
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, gold_regime) VALUES (?, ?, ?, ?)",
        (event_ts, 60, json.dumps({"geopolitics": 90}), "chop"),
    )
    conn.commit()

    # Persist the SAME event (exact event_ts match, required by
    # fetch_evidence_for_event_ts's join) into research_events.
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, direction, "
        "intensity, available_before_prediction, is_post_event_analysis) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (events[0]["fingerprint"], event_ts, event_ts, "LARGE_MOVE", "UP", 5.0, 1, 0),
    )
    persisted_event_id = conn.execute(
        "SELECT event_id FROM research_events WHERE event_ts = ?", (event_ts,)
    ).fetchone()[0]

    # Real geopolitics-feed evidence, published strictly PRE_EVENT.
    geopolitics_feed_url, geopolitics_publisher = ec.FEEDS["geopolitics"]
    publication_ts = event_ts - 2 * hour  # outside SAME_WINDOW_TOLERANCE_MS (1h)
    conn.execute(
        "INSERT INTO research_event_evidence (event_id, feed_url, article_url, publisher, publication_ts, "
        "collection_ts, headline, keyword_score, evidence_relation, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (persisted_event_id, geopolitics_feed_url, "https://example.test/article-1", geopolitics_publisher,
         publication_ts, event_ts, "Some real-world headline", 1.0, "PRE_EVENT", "hash-1"),
    )
    conn.commit()
    return start_ts, end_ts, event_ts


def test_dataset_shape_and_no_duplicate_event_source_rows():
    """The fixture's added post-event price movement genuinely triggers
    a second real event (REGIME_REVERSAL, chop_to_rally) in addition to
    the seeded LARGE_MOVE -- a realistic multi-event dataset, not
    forced down to exactly one. The join must still produce exactly
    one row per (event, source) pair, with no duplicates, regardless of
    how many real events the detectors found."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    for key in ("window", "events", "source_keys", "results"):
        assert key in dataset
    assert len(dataset["events"]) >= 1
    assert any(e["event_ts"] == event_ts and e["category"] == "LARGE_MOVE" for e in dataset["events"])
    assert dataset["source_keys"] == ["geopolitics"]

    # Exactly one row per (event, source) pair -- no duplicates, and no
    # missing pairs either.
    seen = set()
    for r in dataset["results"]:
        pair = (r["event_id"], r["source_key"])
        assert pair not in seen, f"duplicate event/source row: {pair}"
        seen.add(pair)
    assert len(dataset["results"]) == len(dataset["events"]) * len(dataset["source_keys"])


def test_geopolitics_source_reaches_relevant_with_real_pre_event_evidence():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    row = dataset["results"][0]
    assert row["source_key"] == "geopolitics"
    assert row["n_evidence_rows"] == 1
    assert row["relevance_result"] == "RELEVANT"
    assert row["relevance_affinity_status"] == "DIRECT_TOPIC_RELEVANCE"
    assert "PRE_EVENT_EVIDENCE" in row["relevance_temporal_statuses"]
    # A deterministic label must be produced -- never left unset.
    assert row["event_source_interpretation"] in join_module.EVENT_SOURCE_INTERPRETATIONS


def test_horizons_report_6h_12h_24h_explicitly():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    row = dataset["results"][0]
    assert set(row["horizons"].keys()) == {"6h", "12h", "24h"}
    for h in ("6h", "12h", "24h"):
        detail = row["horizons"][h]
        for field in ("status", "quality", "return_pct", "residual_pct", "control_classification"):
            assert field in detail


def test_no_evidence_event_yields_insufficient_evidence_interpretation():
    """An event with zero research_event_evidence rows must classify as
    INSUFFICIENT_EVIDENCE regardless of what BTC did -- evidence absence
    is recorded honestly, never inferred into a narrative."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    # Remove the evidence row seeded above -- leaves the persisted event
    # with zero evidence, the honest INSUFFICIENT_EVIDENCE case.
    conn.execute("DELETE FROM research_event_evidence")
    conn.commit()

    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    row = dataset["results"][0]
    assert row["n_evidence_rows"] == 0
    assert row["relevance_result"] == "INSUFFICIENT_EVIDENCE"
    assert row["event_source_interpretation"] == "INSUFFICIENT_EVIDENCE"


def test_post_event_only_evidence_never_establishes_relevance():
    """Temporal integrity: evidence published strictly AFTER event_ts
    (beyond SAME_WINDOW tolerance) must never be used to explain the
    event -- this is PR59's own constructively-tested rule, exercised
    again here through the join to confirm it survives unmodified."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    hour = 3_600_000
    conn.execute("DELETE FROM research_event_evidence")
    geopolitics_feed_url, geopolitics_publisher = ec.FEEDS["geopolitics"]
    persisted_event_id = conn.execute(
        "SELECT event_id FROM research_events WHERE event_ts = ?", (event_ts,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO research_event_evidence (event_id, feed_url, article_url, publisher, publication_ts, "
        "collection_ts, headline, keyword_score, evidence_relation, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (persisted_event_id, geopolitics_feed_url, "https://example.test/after", geopolitics_publisher,
         event_ts + 5 * hour, event_ts + 5 * hour, "After-the-fact headline", 1.0, "POST_EVENT", "hash-2"),
    )
    conn.commit()

    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    row = dataset["results"][0]
    assert row["n_evidence_rows"] == 1  # evidence exists...
    assert row["relevance_result"] != "RELEVANT"  # ...but never establishes RELEVANT
    assert "POST_EVENT_CONTEXT" in row["relevance_temporal_statuses"]


def test_v2_failure_cluster_events_are_never_treated_as_real_world_evidence_candidates():
    """is_internal_model_event events (V2_FAILURE_CLUSTER) must always
    classify as INSUFFICIENT_EVIDENCE regardless of relevance/reaction
    inputs -- they describe V2's own prediction behaviour, not a market
    or information event."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    conn.close()
    # Directly exercise the internal-model-event branch without needing
    # a second full fixture: classify_event_source_interpretation is
    # never even reached for such events (checked in the orchestration
    # function), so assert that behaviour via a minimal synthetic
    # dataset shape instead of re-deriving PR3/PR59 internals here.
    fake_event = {"event_ts": 1, "is_internal_model_event": True}
    fake_relevance = {"events": [fake_event], "source_keys": ["fng"],
                       "results": [{"event_id": 1, "event_ts": 1, "event_category": "V2_FAILURE_CLUSTER",
                                    "source_key": "fng", "source_value": 10, "source_observation_ts": 1,
                                    "n_evidence_rows": 0,
                                    "relevance": {"result": "NOT_ESTABLISHED", "affinity_status": "NO_DIRECT_TOPIC_AFFINITY",
                                                  "temporal_statuses": []}}]}
    import unittest.mock as mock
    with mock.patch.object(join_module.esrel, "build_event_source_relevance_dataset", return_value=fake_relevance), \
         mock.patch.object(join_module.cer, "build_controlled_reaction_dataset", return_value={
             "window": {}, "events": [fake_event], "source_keys": ["fng"], "results": [],
             "reaction_noise_floor_by_horizon": {}, "residual_noise_floor_by_horizon": {},
         }), \
         mock.patch.object(join_module.cer, "build_event_level_table", return_value=[]):
        dataset = join_module.build_event_source_evidence_dataset(None, 0, 1)
    assert dataset["results"][0]["event_source_interpretation"] == "INSUFFICIENT_EVIDENCE"
    assert dataset["results"][0]["is_internal_model_event"] is True


# =====================================================================
# Summary functions -- descriptive only, no ranking
# =====================================================================

def test_summarize_evidence_coverage_counts_real_world_events_only():
    """Two real events exist (the seeded LARGE_MOVE, which has evidence
    attached, and an emergent REGIME_REVERSAL, which does not) -- this
    must correctly differentiate the two rather than just counting
    events."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    coverage = join_module.summarize_evidence_coverage(dataset)
    assert coverage["n_real_world_events"] == len(dataset["events"])
    assert coverage["n_real_world_events_with_any_evidence"] == 1
    assert coverage["n_real_world_events_insufficient_evidence"] == len(dataset["events"]) - 1


def test_summarize_by_source_interpretation_covers_every_declared_label():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    summary = join_module.summarize_by_source_interpretation(dataset)
    assert set(summary.keys()) == {"geopolitics"}
    assert set(summary["geopolitics"].keys()) == set(join_module.EVENT_SOURCE_INTERPRETATIONS)
    assert sum(summary["geopolitics"].values()) == len(dataset["events"])  # one count per event/source row


def test_summarize_btc_outcome_coverage_reports_6h_12h_24h():
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    coverage = join_module.summarize_btc_outcome_coverage(dataset)
    assert set(coverage.keys()) == {"6h", "12h", "24h"}
    n_real_events = sum(1 for e in dataset["events"] if not e["is_internal_model_event"])
    for h in ("6h", "12h", "24h"):
        assert coverage[h]["n_events"] == n_real_events
        assert 0 <= coverage[h]["n_resolved"] <= n_real_events


# =====================================================================
# Production-table isolation: this module never writes anything
# =====================================================================

def test_full_dataset_is_directly_json_serializable():
    """Regression guard against the exact failure class EXP-005 hit in
    production: report["level2"]["tests"] and ["redundancy"]["pairwise"]
    were keyed by tuples there, which json.dumps() cannot serialize.
    This module's own internal tuple-keyed structure
    (controlled_event_reaction's `control_by_event_horizon`) is a
    private variable never exposed in any returned dataset -- verified
    here directly against a REAL multi-event dataset (not just field-by-
    field) before this is ever wired into a persistence path."""
    conn = _build_fixture_conn()
    start_ts, end_ts, event_ts = _seed_large_move_with_evidence(conn)
    dataset = join_module.build_event_source_evidence_dataset(conn, start_ts, end_ts)
    conn.close()

    serialized = json.dumps(dataset)  # must not raise
    assert json.loads(serialized) == dataset


def test_module_defines_no_persist_or_write_function():
    with open(os.path.join(_HERE, "event_source_evidence_join.py")) as f:
        src = f.read()
    assert "def persist_" not in src
    assert "INSERT INTO" not in src
    assert "UPDATE " not in src
    assert "DELETE FROM" not in src
    assert ".execute(" not in src  # this module issues no SQL of its own at all
