"""
Tests for research/stage7_evidence_sufficiency.py.

Run with: python3 -m pytest research/test_stage7_evidence_sufficiency.py -v
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import stage7_evidence_sufficiency as suff  # noqa: E402

EVENT = {"event_id": 1, "event_ts": 1_000_000, "category": "LARGE_MOVE", "coin": "BTC"}


def _evidence(publisher, relation="PRE_EVENT", evidence_id=1):
    return {"evidence_id": evidence_id, "publisher": publisher, "evidence_relation": relation}


def test_no_evidence_rows_is_insufficient_evidence():
    result = suff.assess_evidence_sufficiency(EVENT, [], {}, {})
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert result["reasons"]
    assert result["questions"]


def test_only_post_event_evidence_is_insufficient_evidence():
    evidence = [_evidence("CoinDesk", relation="POST_EVENT")]
    relevance = {"cryptonews": {"result": "RELEVANT"}}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, {})
    assert result["status"] == "INSUFFICIENT_EVIDENCE"
    assert "POST_EVENT_CONTEXT" in result["reasons"][0] or "POST_EVENT" in result["reasons"][0]


def test_evidence_with_no_relevant_source_is_insufficient_evidence():
    evidence = [_evidence("CoinDesk"), _evidence("The Block", evidence_id=2)]
    relevance = {"fng": {"result": "NOT_ESTABLISHED"}, "usd": {"result": "INSUFFICIENT_EVIDENCE"}}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, {})
    assert result["status"] == "INSUFFICIENT_EVIDENCE"


def test_conflicting_interpretations_across_sources_yields_conflicting():
    evidence = [_evidence("CoinDesk"), _evidence("The Block", evidence_id=2)]
    relevance = {"cryptonews": {"result": "RELEVANT"}, "regulatory": {"result": "RELEVANT"}}
    interpretation = {"cryptonews": "EVENT_SOURCE_ALIGNED", "regulatory": "EVENT_SOURCE_MISLEADING_POSSIBLE"}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, interpretation)
    assert result["status"] == "CONFLICTING"
    assert result["questions"]


def test_conflicting_check_never_triggers_on_agreeing_interpretations():
    evidence = [_evidence("CoinDesk"), _evidence("The Block", evidence_id=2)]
    relevance = {"cryptonews": {"result": "RELEVANT"}, "regulatory": {"result": "RELEVANT"}}
    interpretation = {"cryptonews": "EVENT_SOURCE_ALIGNED", "regulatory": "EVENT_SOURCE_ALIGNED"}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, interpretation)
    assert result["status"] != "CONFLICTING"


def test_single_publisher_is_insufficient_no_source_count_shortcut():
    # Many articles, but all from ONE publisher -- must not be SUFFICIENT
    # just because there are "enough" rows (task's own explicit instruction).
    evidence = [_evidence("CoinDesk", evidence_id=i) for i in range(1, 6)]
    relevance = {"cryptonews": {"result": "RELEVANT"}}
    interpretation = {"cryptonews": "EVENT_SOURCE_ALIGNED"}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, interpretation)
    assert result["status"] == "INSUFFICIENT"
    assert "publisher" in result["reasons"][0]


def test_two_distinct_publishers_with_relevance_is_sufficient():
    evidence = [_evidence("CoinDesk"), _evidence("The Block", evidence_id=2)]
    relevance = {"cryptonews": {"result": "RELEVANT"}, "regulatory": {"result": "POSSIBLY_RELEVANT"}}
    interpretation = {"cryptonews": "EVENT_SOURCE_ALIGNED", "regulatory": "EVENT_SOURCE_INFORMATIONAL_ONLY"}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, interpretation)
    assert result["status"] == "SUFFICIENT"
    assert result["reasons"] == []
    assert result["questions"] == []


def test_sufficient_never_returned_with_zero_evidence():
    # Defensive: even a maliciously-empty relevance/interpretation dict
    # must never combine with empty evidence to reach SUFFICIENT.
    result = suff.assess_evidence_sufficiency(EVENT, [], {"cryptonews": {"result": "RELEVANT"}}, {})
    assert result["status"] != "SUFFICIENT"


def test_same_window_evidence_counts_as_predictive():
    evidence = [_evidence("CoinDesk", relation="SAME_WINDOW"), _evidence("The Block", relation="SAME_WINDOW", evidence_id=2)]
    relevance = {"cryptonews": {"result": "RELEVANT"}}
    interpretation = {"cryptonews": "EVENT_SOURCE_ALIGNED"}
    result = suff.assess_evidence_sufficiency(EVENT, evidence, relevance, interpretation)
    assert result["status"] == "SUFFICIENT"


def test_build_research_questions_is_deterministic_and_event_specific():
    q1 = suff.build_research_questions(EVENT, [], "INSUFFICIENT_EVIDENCE")
    q2 = suff.build_research_questions(EVENT, [], "INSUFFICIENT_EVIDENCE")
    assert q1 == q2
    assert any(str(EVENT["event_ts"]) in q for q in q1)


def test_build_research_questions_varies_by_status():
    q_conflicting = suff.build_research_questions(EVENT, [], "CONFLICTING")
    q_sufficient_like = suff.build_research_questions(EVENT, [], "INSUFFICIENT_EVIDENCE")
    assert q_conflicting != q_sufficient_like
