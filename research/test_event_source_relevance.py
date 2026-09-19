"""
Tests for research/event_source_relevance.py (PR-3).

Real production `research_event_evidence` currently has 0 rows (see PR
description), so every RELEVANT/POSSIBLY_RELEVANT/NOT_ESTABLISHED path
below is exercised with synthetic evidence fixtures -- this is
intentional and disclosed: PR-3's job is to prove the METHODOLOGY,
not to report a real relevance result that today's sparse evidence
cannot support.

Run with: python3 -m pytest research/test_event_source_relevance.py -v
"""
import inspect
import json
import sqlite3
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import event_source_relevance as esrel  # noqa: E402
import evidence_collector as ec  # noqa: E402

HOUR = 3600000
DAY = 24 * HOUR


def _fresh_db():
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE predictions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, target_ts INTEGER,
        horizon_hours INTEGER NOT NULL, p_up REAL, realized_up INTEGER, realized_return REAL,
        model_version TEXT, git_commit_sha TEXT
    )""")
    conn.execute("""CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, score INTEGER NOT NULL,
        sources_json TEXT, technical_score INTEGER, gold_regime TEXT
    )""")
    conn.execute("""CREATE TABLE btc_data (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, btc_price REAL
    )""")
    conn.execute("""CREATE TABLE research_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT, fingerprint TEXT NOT NULL,
        event_ts INTEGER NOT NULL, detection_ts INTEGER NOT NULL, category TEXT NOT NULL,
        direction TEXT, intensity REAL, available_before_prediction INTEGER NOT NULL,
        is_post_event_analysis INTEGER NOT NULL DEFAULT 0,
        trigger_metric TEXT, trigger_threshold REAL, trigger_version TEXT
    )""")
    conn.execute("""CREATE TABLE research_event_evidence (
        evidence_id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
        feed_url TEXT NOT NULL, article_url TEXT NOT NULL, publisher TEXT NOT NULL,
        publication_ts INTEGER NOT NULL, collection_ts INTEGER NOT NULL,
        headline TEXT NOT NULL, keyword_score REAL, evidence_relation TEXT NOT NULL,
        content_hash TEXT NOT NULL
    )""")
    return conn


def _insert_history(conn, ts, sources, score=50):
    conn.execute(
        "INSERT INTO history (ts, score, sources_json, technical_score, gold_regime) VALUES (?,?,?,?,?)",
        (ts, score, json.dumps(sources), 50, None),
    )


def _insert_btc(conn, ts, price):
    conn.execute("INSERT INTO btc_data (ts, btc_price) VALUES (?, ?)", (ts, price))


def _insert_persisted_event(conn, event_ts, category="LARGE_MOVE"):
    conn.execute(
        "INSERT INTO research_events (fingerprint, event_ts, detection_ts, category, direction, "
        "intensity, available_before_prediction, is_post_event_analysis) VALUES (?,?,?,?,?,?,?,?)",
        (f"{category}|{event_ts}|UP|test", event_ts, event_ts, category, "UP", 5.0, 1, 0),
    )
    return conn.execute("SELECT event_id FROM research_events WHERE event_ts = ?", (event_ts,)).fetchone()[0]


def _insert_evidence(conn, event_id, feed_url, publication_ts, publisher="Test Publisher",
                      headline="headline", collection_ts=0, keyword_score=None, evidence_relation="PRE_EVENT",
                      content_hash=None):
    content_hash = content_hash or f"hash-{event_id}-{publication_ts}-{feed_url}"
    conn.execute(
        "INSERT INTO research_event_evidence (event_id, feed_url, article_url, publisher, publication_ts, "
        "collection_ts, headline, keyword_score, evidence_relation, content_hash) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (event_id, feed_url, f"https://example.com/{content_hash}", publisher, publication_ts,
         collection_ts, headline, keyword_score, evidence_relation, content_hash),
    )


GEOPOLITICS_FEED = ec.FEEDS["geopolitics"][0]
CRYPTO_FEED = ec.FEEDS["crypto"][0]


# =====================================================================
# 1. Pre-event evidence qualifies for relevance analysis
# =====================================================================

def test_pre_event_evidence_qualifies_as_relevant_for_exact_topic_match():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts - 2 * HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    assert result["result"] == "RELEVANT"
    conn.close()


# =====================================================================
# 2. Post-event evidence cannot establish predictive relevance
# =====================================================================

def test_post_event_only_evidence_never_yields_relevant():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts + 5 * HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    assert result["result"] != "RELEVANT"
    assert result["result"] != "POSSIBLY_RELEVANT"
    assert result["result"] == "NOT_ESTABLISHED"
    conn.close()


def test_post_event_only_evidence_temporal_status_is_post_event_context():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    row = {"publication_ts": event_ts + 5 * HOUR}
    status = esrel.classify_evidence_temporal_status(row, event_ts)
    assert status == "POST_EVENT_CONTEXT"


# =====================================================================
# 3. Publication after event_ts is never used as pre-event evidence
# (constructive: mixed pre + post evidence, only pre counted)
# =====================================================================

def test_mixed_evidence_only_pre_event_rows_drive_relevant_result():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts - 3 * HOUR,
                      content_hash="pre")
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts + 10 * HOUR,
                      content_hash="post")
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    assert result["result"] == "RELEVANT"
    # the post-event row's evidence_id must never appear in the RELEVANT reasoning
    pre_row = next(r for r in evidence_rows if r["content_hash"] == "pre")
    post_row = next(r for r in evidence_rows if r["content_hash"] == "post")
    assert pre_row["evidence_id"] in result["evidence_ids"]
    assert post_row["evidence_id"] not in result["evidence_ids"]
    conn.close()


# =====================================================================
# 4. Source snapshot remains no-lookahead
# =====================================================================

def test_source_snapshot_never_uses_a_row_after_event_ts():
    conn = _fresh_db()
    _insert_history(conn, 50 * HOUR, {"fng": 10})
    _insert_history(conn, 150 * HOUR, {"fng": 90})  # strictly after -- must never be used
    conn.commit()
    snap = esrel.snapshot_v1_source(conn, 100 * HOUR, "fng")
    assert snap["source_value"] == 10.0
    assert snap["source_observation_ts"] == 50 * HOUR
    conn.close()


# =====================================================================
# 5. Evidence/source temporal compatibility
# =====================================================================

def test_same_window_evidence_is_distinguished_from_pre_event():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    row_same = {"publication_ts": event_ts}  # exactly at event_ts -> SAME_WINDOW (within tolerance)
    row_pre = {"publication_ts": event_ts - 3 * HOUR}
    assert esrel.classify_evidence_temporal_status(row_same, event_ts) == "SAME_WINDOW_EVIDENCE"
    assert esrel.classify_evidence_temporal_status(row_pre, event_ts) == "PRE_EVENT_EVIDENCE"


# =====================================================================
# 6. Deterministic topic extraction
# =====================================================================

def test_evidence_topic_derived_from_pr4_feeds_dict_unchanged():
    row = {"feed_url": ec.FEEDS["regulatory_coindesk"][0]}
    assert esrel.evidence_topic(row) == "regulatory"
    row2 = {"feed_url": ec.FEEDS["crypto"][0]}
    assert esrel.evidence_topic(row2) == "crypto"


def test_source_topic_affinity_is_deterministic_and_fixed():
    assert esrel.source_topic_affinity("geopolitics") == ("geopolitics", "EXACT")
    assert esrel.source_topic_affinity("regulatory") == ("regulatory", "EXACT")
    assert esrel.source_topic_affinity("cryptonews") == ("crypto", "SUBSTRING")
    assert esrel.source_topic_affinity("macrogeo") == ("macro", "PREFIX")
    assert esrel.source_topic_affinity("fng") == (None, None)
    assert esrel.source_topic_affinity("yield10y") == (None, None)


# =====================================================================
# 7. Deterministic relevance classification (repeatable, explainable)
# =====================================================================

def test_relevance_classification_is_repeatable_and_explainable():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts - HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    r1 = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    r2 = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    assert r1 == r2
    assert r1["reason"]  # every classification carries an explanation
    conn.close()


# =====================================================================
# 8-11. The four relevance outcomes
# =====================================================================

def test_relevant_case():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, GEOPOLITICS_FEED, publication_ts=event_ts - 2 * HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)
    assert result["result"] == "RELEVANT"
    conn.close()


def test_possibly_relevant_case_partial_match():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    # "macro" topic match (macrogeo's own PREFIX affinity), pre-event timing --
    # match_type is not EXACT, so this is POSSIBLY_RELEVANT, not RELEVANT.
    _insert_evidence(conn, event_id, ec.FEEDS["macro"][0], publication_ts=event_ts - HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("macrogeo", evidence_rows, event_ts)
    assert result["result"] == "POSSIBLY_RELEVANT"
    conn.close()


def test_not_established_case_topic_mismatch():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, CRYPTO_FEED, publication_ts=event_ts - HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("geopolitics", evidence_rows, event_ts)  # wrong topic
    assert result["result"] == "NOT_ESTABLISHED"
    conn.close()


def test_not_established_case_no_deterministic_affinity():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, CRYPTO_FEED, publication_ts=event_ts - HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("fng", evidence_rows, event_ts)  # fng has no affinity at all
    assert result["result"] == "NOT_ESTABLISHED"
    conn.close()


def test_insufficient_evidence_case_no_evidence_rows():
    result = esrel.classify_relevance("geopolitics", [], 100 * HOUR)
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


# =====================================================================
# 12. Missing PR4 evidence (event never persisted at all)
# =====================================================================

def test_missing_pr4_evidence_when_event_never_persisted():
    conn = _fresh_db()
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, 999 * HOUR)
    assert evidence_rows == []
    result = esrel.classify_relevance("geopolitics", evidence_rows, 999 * HOUR)
    assert result["result"] == "INSUFFICIENT_EVIDENCE"
    conn.close()


# =====================================================================
# 13. Missing source observation
# =====================================================================

def test_missing_source_observation_reported_honestly():
    conn = _fresh_db()
    conn.commit()  # no history rows at all
    snap = esrel.snapshot_v1_source(conn, 100 * HOUR, "fng")
    assert snap["source_value"] is None
    assert snap["status"] == "NO_HISTORY_BEFORE_EVENT"
    conn.close()


# =====================================================================
# 14. Deterministic repeated execution (full dataset)
# =====================================================================

def test_deterministic_rerun_identical_dataset():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "geopolitics": (i * 3) % 100})
        _insert_btc(conn, ts, 100.0 + i * 0.2 + (5 if i % 15 == 0 else 0))
    conn.commit()
    d1 = esrel.build_event_source_relevance_dataset(conn, 10 * HOUR, 59 * HOUR)
    d2 = esrel.build_event_source_relevance_dataset(conn, 10 * HOUR, 59 * HOUR)
    assert d1 == d2
    conn.close()


# =====================================================================
# 15-19. Safety / scope tests
# =====================================================================

def test_no_writes_anywhere_in_module():
    src = inspect.getsource(esrel)
    for forbidden in ["INSERT INTO", "UPDATE ", "DELETE FROM", "DROP ", "ALTER TABLE", "CREATE TABLE"]:
        assert forbidden not in src
    assert "def persist" not in src


def test_no_network_calls_anywhere_in_module():
    src = inspect.getsource(esrel)
    for forbidden in ["import requests", "requests.get(", "requests.post(", "urllib", "fetch(",
                       "http://", "https://", "openai", "gemini", "anthropic"]:
        assert forbidden not in src.lower()
    # explicitly never calls PR4's live collector (the docstring names
    # the function in prose while explaining why it is not called, so
    # check for the actual call form via the module's own import alias)
    assert "ec.collect_evidence_for_event(" not in src


def test_never_modifies_or_reimports_v1_v2_worker():
    src = inspect.getsource(esrel)
    for forbidden in ["worker.js", "import worker", "V1_WEIGHT", "prediction_generation", "0008", "0009", "0010"]:
        assert forbidden not in src


def test_never_produces_a_weight_recommendation_vocabulary():
    src = inspect.getsource(esrel)
    for forbidden in ['"KEEP"', "'KEEP'", '"INCREASE"', "'INCREASE'", '"DECREASE"', "'DECREASE'"]:
        assert forbidden not in src
    for forbidden_def in ["def recommend", "def rank_source", "def score_source", "def best_source"]:
        assert forbidden_def not in src


def test_never_generates_a_build_request():
    src = inspect.getsource(esrel)
    for forbidden in ['"BUILD_REQUEST"', "'BUILD_REQUEST'", "def build_build_request", "hypothesis_gate"]:
        assert forbidden not in src


def test_never_creates_schema_or_migration():
    src = inspect.getsource(esrel)
    for forbidden in ["ALTER TABLE", "research_event_source_evaluations", ".ai/migrations"]:
        assert forbidden not in src


# =====================================================================
# 20. No ranking / score / "best source"
# =====================================================================

def test_source_summary_never_ranks_sources():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "geopolitics": (i * 3) % 100})
        _insert_btc(conn, ts, 100.0 + i * 0.2)
    conn.commit()
    dataset = esrel.build_event_source_relevance_dataset(conn, 10 * HOUR, 59 * HOUR)
    summary = esrel.summarize_by_source(dataset)
    for source_key, stats in summary.items():
        assert "rank" not in stats
        assert "score" not in stats
        assert "best" not in stats


# =====================================================================
# Additional structural tests: BTC reaction never used as relevance reason
# =====================================================================

def test_module_never_imports_reaction_or_control_modules():
    src = inspect.getsource(esrel)
    assert "import event_source_reaction" not in src
    assert "import controlled_event_reaction" not in src


def test_classify_relevance_signature_never_takes_a_btc_return():
    sig = inspect.signature(esrel.classify_relevance)
    for param_name in sig.parameters:
        assert "return" not in param_name.lower()
        assert "reaction" not in param_name.lower()
        assert "btc" not in param_name.lower()


def test_internal_model_events_are_flagged_not_treated_as_real_world():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": 50})
        p_up = 0.9  # predicts UP every time
        realized_up = 0  # actual is always DOWN -> every prediction is wrong, builds a streak
        conn.execute(
            "INSERT INTO predictions (ts, horizon_hours, p_up, realized_up) VALUES (?,?,?,?)",
            (ts, 12, p_up, realized_up),
        )
    conn.commit()
    events = esrel.collect_events(conn, 10 * HOUR, 59 * HOUR)
    failure_events = [e for e in events if e["category"] == "V2_FAILURE_CLUSTER"]
    assert failure_events
    for e in failure_events:
        assert e["is_internal_model_event"] is True
    conn.close()


# =====================================================================
# Corrected-interpretation tests (per independent audit): a source with
# no direct topical/textual affinity must never be characterized as
# structurally incapable of relevance, and the affinity concept must
# stay a separate field from the relevance verdict.
# =====================================================================

def test_no_direct_affinity_does_not_claim_structural_incapacity():
    conn = _fresh_db()
    event_ts = 100 * HOUR
    event_id = _insert_persisted_event(conn, event_ts)
    _insert_evidence(conn, event_id, CRYPTO_FEED, publication_ts=event_ts - 2 * HOUR)
    conn.commit()
    evidence_rows = esrel.fetch_evidence_for_event_ts(conn, event_ts)
    result = esrel.classify_relevance("fng", evidence_rows, event_ts)
    assert result["result"] == "NOT_ESTABLISHED"
    reason_lower = result["reason"].lower()
    # must NOT bare-assert incapacity (these specific unqualified
    # phrasings must never appear at all)
    for forbidden in ["can never be relevant", "cannot be relevant", "17 sources can never"]:
        assert forbidden not in reason_lower
    # must explicitly NEGATE the incapacity claim, not merely omit it
    assert "does not establish that the source is incapable" in reason_lower
    # must disclose that indirect/consequence relevance is a separate,
    # out-of-scope question rather than a settled negative
    assert "indirect" in reason_lower
    conn.close()


def test_affinity_status_is_separate_field_from_relevance_result():
    result = esrel.classify_relevance("fng", [], 100 * HOUR)
    assert result["affinity_status"] == esrel.NO_DIRECT_TOPIC_AFFINITY
    assert result["result"] == "INSUFFICIENT_EVIDENCE"  # unaffected by affinity_status


def test_source_affinity_status_deterministic_and_fixed():
    assert esrel.source_affinity_status("geopolitics") == esrel.DIRECT_TOPIC_RELEVANCE
    assert esrel.source_affinity_status("regulatory") == esrel.DIRECT_TOPIC_RELEVANCE
    assert esrel.source_affinity_status("cryptonews") == esrel.DIRECT_TOPIC_RELEVANCE
    assert esrel.source_affinity_status("macrogeo") == esrel.DIRECT_TOPIC_RELEVANCE
    for source_key in ("fng", "funding", "longshort", "global", "gold", "hypefunding",
                        "nasdaq", "ninemag", "oil", "onchain", "sosovalue", "sp500",
                        "strc", "usd", "yield10y"):
        assert esrel.source_affinity_status(source_key) == esrel.NO_DIRECT_TOPIC_AFFINITY


def test_relevance_labels_unchanged_no_indirect_label_introduced():
    # Guards against scope creep: exactly the original four relevance
    # labels, no fifth "INDIRECT_RELEVANCE"-style label added.
    assert esrel.RELEVANCE_LABELS == ("RELEVANT", "POSSIBLY_RELEVANT", "NOT_ESTABLISHED", "INSUFFICIENT_EVIDENCE")
    assert esrel.AFFINITY_STATUSES == ("DIRECT_TOPIC_RELEVANCE", "NO_DIRECT_TOPIC_AFFINITY")


def test_direct_affinity_alone_is_not_sufficient_for_relevant():
    # Having DIRECT_TOPIC_RELEVANCE does not by itself produce RELEVANT
    # -- qualifying evidence is still required (affinity is necessary,
    # never sufficient).
    result = esrel.classify_relevance("geopolitics", [], 100 * HOUR)
    assert result["affinity_status"] == esrel.DIRECT_TOPIC_RELEVANCE
    assert result["result"] == "INSUFFICIENT_EVIDENCE"


def test_source_summary_includes_affinity_status_without_ranking():
    conn = _fresh_db()
    for i in range(60):
        ts = i * HOUR
        _insert_history(conn, ts, {"fng": (i * 7) % 100, "geopolitics": (i * 3) % 100})
        _insert_btc(conn, ts, 100.0 + i * 0.2)
    conn.commit()
    dataset = esrel.build_event_source_relevance_dataset(conn, 10 * HOUR, 59 * HOUR)
    summary = esrel.summarize_by_source(dataset)
    assert summary["fng"]["affinity_status"] == esrel.NO_DIRECT_TOPIC_AFFINITY
    assert summary["geopolitics"]["affinity_status"] == esrel.DIRECT_TOPIC_RELEVANCE
    for source_key, stats in summary.items():
        assert "rank" not in stats
        assert "score" not in stats
