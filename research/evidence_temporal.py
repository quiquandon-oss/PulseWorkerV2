"""
PR5c: prediction-time evidence eligibility (Section 4 and Section 16).

Pure functions -- no database, no network. PR4 evidence
(research_event_evidence, evidence_collector.py) is explicitly OPTIONAL
for PR5c's core source/outcome analysis (Section 16): source_analysis.py
does not depend on this module at all. This module exists solely to
satisfy Section 4's explicit requirement -- "add an explicit test
proving that post-prediction information cannot enter predictive
analysis" -- for the one place PR4 evidence COULD be joined against a
prediction in a future PR, without conflating two genuinely different
timestamp axes:

  1. event-relative classification (evidence_collector.classify_relation,
     PRE_EVENT / SAME_WINDOW / POST_EVENT) -- publication_ts vs. the
     EVENT's own timestamp. Unchanged, reused, not reimplemented here.
  2. prediction-time availability (this module) -- publication_ts vs.
     the PREDICTION's own timestamp. A publication_ts can be PRE_EVENT
     relative to its event and still be POST-prediction relative to a
     specific prediction made even earlier; the two checks answer
     different questions and Section 4 requires they stay separate
     ("do not use event-relative classification as a substitute for
     prediction-time availability").

Given PR4's real production evidence table is currently sparse (see PR
description), any caller integrating this into an actual analysis must
report INSUFFICIENT_DATA rather than fabricate a conclusion from too few
rows (Section 16) -- that reporting is the caller's responsibility, not
this module's; this module only ever answers the yes/no eligibility
question for a single piece of evidence.
"""


def is_predictive_eligible(publication_ts, prediction_ts):
    """The Section 4 invariant, stated once as one boolean function so it
    can never be duplicated/drift: information_available_ts <
    prediction_ts, strictly. publication_ts is used as the conservative
    proxy for "information available" (the article's own claimed
    publication time, not this system's later collection_ts). Evidence
    with publication_ts >= prediction_ts is explanatory only, never
    predictive.
    """
    if publication_ts is None or prediction_ts is None:
        raise ValueError("publication_ts and prediction_ts are both required")
    return publication_ts < prediction_ts


def filter_predictive_evidence(evidence_rows, prediction_ts):
    """Given a list of evidence dicts (each with at least
    'publication_ts'), returns only those eligible to inform a
    prediction made at prediction_ts (Section 4: post-prediction
    information must never enter predictive analysis).

    Every excluded row is retained in a separate 'excluded' list with
    its own record -- never silently dropped from the caller's
    visibility -- so a caller can report, e.g., "3 of 5 articles were
    post-prediction and excluded" rather than just seeing 2 rows appear
    from nowhere.
    """
    eligible = []
    excluded = []
    for row in evidence_rows:
        if is_predictive_eligible(row["publication_ts"], prediction_ts):
            eligible.append(row)
        else:
            excluded.append(row)
    return {"eligible": eligible, "excluded_post_prediction": excluded}


def annotate_same_window_separation(evidence_rows, event_ts, same_window_tolerance_ms):
    """Section 4: 'same-window ambiguity remains separate' -- this
    reports, alongside (never instead of) prediction-time eligibility,
    whether each row's publication_ts falls within
    [event_ts - tolerance, event_ts + tolerance] of the EVENT it
    concerns. A SAME_WINDOW row is not automatically predictive-eligible
    or automatically excluded by this function -- it is a distinct piece
    of information a caller must combine with
    filter_predictive_evidence()'s own, independent verdict, never a
    substitute for it.
    """
    annotated = []
    for row in evidence_rows:
        publication_ts = row["publication_ts"]
        delta = publication_ts - event_ts
        if abs(delta) <= same_window_tolerance_ms:
            relation = "SAME_WINDOW"
        elif delta < 0:
            relation = "PRE_EVENT"
        else:
            relation = "POST_EVENT"
        annotated.append({**row, "event_relative_relation": relation})
    return annotated
