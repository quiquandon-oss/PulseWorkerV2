"""
Stage 7 (AI-Assisted Internet Research & Full-Source Sentiment
Recalculation) -- Step B: evidence sufficiency assessment.

=====================================================================
Why this module invents NO new scoring/polarity signal
=====================================================================

`evidence_collector.score_headline_optional()` (research/evidence_
collector.py) is a deliberate no-op stub, by explicit prior review:
"Deliberately NOT wired to V1's SENT_LEXICON... don't make PR4's
evidence layer a V1 scoring layer." `research_event_evidence.keyword_score`
is therefore always NULL in production today -- there is no keyword- or
text-based polarity signal anywhere in this pipeline to reuse, and
building a new one here would repeat exactly the thing that prior
review rejected.

`event_source_relevance.py` and `event_source_evidence_join.py` are
similarly explicit, repeatedly, that they compute RELEVANCE and
ALIGNMENT classifications only -- never a sentiment, a score, or a
ranking ("does NOT decide whether a source is useful", "No LLM", "No
source ranking").

This module therefore assesses sufficiency using ONLY signals that
already exist and are already computed by reviewed, unchanged code:
- research_event_evidence's own `evidence_relation` (PRE_EVENT/
  SAME_WINDOW/POST_EVENT) and `publisher` columns.
- event_source_relevance.classify_relevance()'s own four-way verdict,
  passed in already-computed (this module never re-derives it).
- event_source_evidence_join.classify_event_source_interpretation()'s
  own per-source labels, passed in already-computed.

"CONFLICTING" specifically means: two or more topically-relevant
sources' ALREADY-COMPUTED interpretations disagree (one says
EVENT_SOURCE_ALIGNED, another says EVENT_SOURCE_MISLEADING_POSSIBLE,
for the same event) -- a real disagreement in existing classifications,
never an invented text-polarity heuristic.
"""

from collections import Counter

SUFFICIENCY_STATUSES = ("SUFFICIENT", "INSUFFICIENT", "CONFLICTING", "INSUFFICIENT_EVIDENCE")

# Below this many distinct publishers among PRE_EVENT/SAME_WINDOW
# evidence rows, there is no independent corroboration -- a single
# publisher's account, however detailed, is not "adequate evidence" per
# the task's own instruction not to use source count alone but also not
# to accept a single uncorroborated account. Two is the smallest number
# that can show independent corroboration at all.
MIN_DISTINCT_PUBLISHERS_FOR_SUFFICIENT = 2

_ALIGNED_LABELS = ("EVENT_SOURCE_ALIGNED",)
_MISLEADING_LABELS = ("EVENT_SOURCE_MISLEADING_POSSIBLE",)
_PREDICTIVE_RELATIONS = ("PRE_EVENT", "SAME_WINDOW")


def _predictive_evidence_rows(evidence_rows):
    return [r for r in evidence_rows if r.get("evidence_relation") in _PREDICTIVE_RELATIONS]


def _has_topically_relevant_source(relevance_results):
    return any(v.get("result") in ("RELEVANT", "POSSIBLY_RELEVANT") for v in relevance_results.values())


def _has_conflicting_interpretation(interpretation_results):
    labels = set(interpretation_results.values())
    return any(a in labels for a in _ALIGNED_LABELS) and any(m in labels for m in _MISLEADING_LABELS)


def assess_evidence_sufficiency(event, evidence_rows, relevance_results, interpretation_results):
    """
    event: dict with at least event_id, event_ts, category.
    evidence_rows: list of research_event_evidence rows (dicts) for this
        event -- Stage 6's own, unchanged.
    relevance_results: dict source_key -> classify_relevance() output
        (event_source_relevance.py, reused unchanged, computed by caller).
    interpretation_results: dict source_key -> classify_event_source_
        interpretation() output (event_source_evidence_join.py, reused
        unchanged, computed by caller).

    Returns {"status", "reasons": [str], "questions": [str],
    "missing_categories": [str]}. Never returns SUFFICIENT with an
    empty evidence set, and never infers a positive/negative direction
    -- direction is Step G/H's job (human-triggered AI research), not
    this deterministic check's.
    """
    predictive = _predictive_evidence_rows(evidence_rows)
    distinct_publishers = len({r["publisher"] for r in predictive if r.get("publisher")})

    if not evidence_rows or not predictive or not _has_topically_relevant_source(relevance_results):
        reasons = []
        if not evidence_rows:
            reasons.append("No research_event_evidence rows exist for this event.")
        elif not predictive:
            reasons.append("Evidence exists but only as POST_EVENT_CONTEXT -- nothing predates or "
                            "coincides with the event, so it cannot explain what was knowable at the time.")
        else:
            reasons.append("Evidence exists but no source has a RELEVANT or POSSIBLY_RELEVANT "
                            "topical match to it (per event_source_relevance.classify_relevance).")
        return {
            "status": "INSUFFICIENT_EVIDENCE",
            "reasons": reasons,
            "questions": build_research_questions(event, evidence_rows, "INSUFFICIENT_EVIDENCE"),
            "missing_categories": ["primary_reporting", "independent_corroboration"],
        }

    if _has_conflicting_interpretation(interpretation_results):
        return {
            "status": "CONFLICTING",
            "reasons": [
                "Topically-relevant sources' existing interpretations disagree for this event "
                "(at least one EVENT_SOURCE_ALIGNED and at least one EVENT_SOURCE_MISLEADING_POSSIBLE "
                "were computed for the same event by event_source_evidence_join."
                "classify_event_source_interpretation)."
            ],
            "questions": build_research_questions(event, evidence_rows, "CONFLICTING"),
            "missing_categories": ["independent_corroboration", "resolution_of_disagreement"],
        }

    if distinct_publishers < MIN_DISTINCT_PUBLISHERS_FOR_SUFFICIENT:
        return {
            "status": "INSUFFICIENT",
            "reasons": [
                f"Only {distinct_publishers} distinct publisher(s) among pre-event/same-window "
                "evidence -- no independent corroboration of a single account."
            ],
            "questions": build_research_questions(event, evidence_rows, "INSUFFICIENT"),
            "missing_categories": ["independent_corroboration"],
        }

    return {
        "status": "SUFFICIENT",
        "reasons": [],
        "questions": [],
        "missing_categories": [],
    }


def build_research_questions(event, evidence_rows, status):
    """Deterministic, templated, event-specific questions -- never
    free-text content this module invented as if it were a finding.
    The AI research step (Step G) answers these; this function only
    states what is unanswered."""
    category = event.get("category", "this event")
    coin = event.get("coin", "BTC")
    questions = [
        f"What specific real-world development explains the detected {category} event for {coin} "
        f"around event_ts={event.get('event_ts')}?",
        "What independent, credible sources (beyond what is already collected) report on this event?",
    ]
    if status == "CONFLICTING":
        questions.append(
            "Existing sources disagree in how they relate to this event's outcome -- which account, "
            "if any, is better corroborated by primary sources or independent reporting?"
        )
    if status in ("INSUFFICIENT_EVIDENCE", "INSUFFICIENT"):
        questions.append(
            "Is there a plausible transmission mechanism connecting this development to the affected "
            "asset's price, and is it corroborated by more than one independent source?"
        )
    return questions
