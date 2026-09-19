"""
PR5e: research hypothesis & evidence gate.

Objective (per PR5e build authorization): turn the ALREADY-COMPUTED,
already-reviewed outputs of PR5c (research/source_analysis.py),
PR5d (research/error_classification.py), and PR5d-followup
(research/error_overlap_analysis.py) into explicit, persistent,
auditable research-hypothesis records, and determine -- via a fixed,
deterministic gate pipeline -- whether the accumulated evidence is
sufficient to propose a BUILD_REQUEST candidate. PR5e is an EVIDENCE
GATE, not a discovery generator: it never asserts that a hypothesis is
true, only how far it has advanced through a fixed set of checks.

PR5e does NOT recompute Level 1/2/3 statistics, event overlap, or
suppression counts -- it consumes source_analysis.build_source_
effectiveness_report(), error_classification.build_error_classification_
report(), and error_overlap_analysis.build_overlap_report() as inputs
(all three called exactly as their own modules define them, unmodified)
and adds one new layer on top: candidate derivation, gate evaluation,
lifecycle assignment, and persistence into the already-existing (but
not yet production-deployed) research_hypotheses schema.

No V1/V2/Worker/PR3/PR5d-classifier change. No new production table --
research_hypotheses (.ai/migrations/0008_research_hypotheses.sql) is
reused as-is; see module docstring section "Schema reuse" below for why
no migration is proposed here. No production write of any kind --
persist_hypothesis() is the only function that writes anything, and it
is exercised only against an in-memory sqlite3 fixture in
test_hypothesis_gate.py, never against production.

=====================================================================
Schema reuse -- no migration proposed
=====================================================================

research_hypotheses (created by PR5a, migration 0008) already has
every column this PR needs:
  - subject / statement: the hypothesis identity and human-readable text.
  - status: free text (not a SQL CHECK constraint), already documented
    to hold exactly the ten-stage lifecycle
    (OBSERVATION -> MONITOR -> RESEARCH_HYPOTHESIS -> VALIDATION_READY ->
    BUILD_REQUEST -> AWAITING_APPROVAL -> IMPLEMENTED -> VALIDATED ->
    REJECTED -> ROLLED_BACK) this PR reuses verbatim, not a second one.
  - out_of_sample_status: reused for this PR's VALIDATION_STATUS concept
    (see "Two different 'validation' concepts" below) -- NOT the same
    thing as the lifecycle's own later "VALIDATED" stage.
  - source_analysis_ids: JSON array of research_analyses.analysis_id,
    for traceability -- populated from whatever real analysis_id values
    exist (production research_analyses currently has ZERO rows --
    confirmed by direct read-only count this session -- so this array
    is honestly empty for any hypothesis built from the current
    production snapshot; this module never fabricates a provenance ID).
  - evidence_summary_json: the free-form JSON payload where this PR's
    six-part decomposition (observation/association/incremental_
    information/hypothesis_statement/evidence_status/validation_status),
    gate-by-gate results, evidence_type, and source-redundancy caveat
    all live -- exactly PR5a's own stated intent ("must retain, at
    minimum: effect size, confidence interval, sample size, p-value...
    out-of-sample stability... economic/practical magnitude"), never
    collapsed into a single opaque flag or free-text blurb.

research_hypotheses itself is NOT deployed to production (confirmed by
direct read-only sqlite_master check this session, same as every prior
PR5 round found) -- this is not schema insufficiency, it is an
unapplied (but already-reviewed, already-merged-in-repo) migration.
Per the build authorization ("do not apply production migrations
before PR approval"), this PR does not apply it. persist_hypothesis()
is written and tested against an in-memory fixture carrying this exact
schema; a future, separately authorized step decides if/when it is
actually run against production.

=====================================================================
Two different "validation" concepts -- kept explicitly separate
=====================================================================

- lifecycle_status (the research_hypotheses.status column): where in
  the ten-stage PR5 lifecycle a hypothesis currently sits. Includes a
  stage literally named "VALIDATED" -- but that stage means "validated
  in PRODUCTION after being implemented", per the lifecycle's own
  ordering (... -> IMPLEMENTED -> VALIDATED -> ...).
- validation_status (this PR's own concept, stored in the
  out_of_sample_status column): whether THIS PR's own chronological/
  holdout check found the candidate's pattern to hold on unseen data.
  Values: NOT_YET_TESTED, PASSED_HOLDOUT, FAILED_HOLDOUT,
  INSUFFICIENT_DATA_FOR_HOLDOUT. This is a pre-implementation research
  concept and is NEVER equal to, or a synonym for, the lifecycle's
  post-implementation "VALIDATED" stage. A FAILED_HOLDOUT candidate is
  demoted to lifecycle_status="REJECTED" (an EXISTING lifecycle stage,
  reused, not invented) -- it never reaches "VALIDATED" in either sense.

PR5E_MAX_LIFECYCLE_STATUS caps what this module may ever assign: never
AWAITING_APPROVAL, IMPLEMENTED, VALIDATED, or ROLLED_BACK -- those all
require a real human build-request decision and/or a real deployment,
both entirely outside this PR's scope (enforced structurally, see
test_hypothesis_gate.py::test_never_assigns_beyond_build_request).

=====================================================================
Temporal safety
=====================================================================

This module performs NO new database queries against predictions/
history/btc_data of its own for the core gate pipeline -- Gates 0-3
consume already-computed, already-reviewed report fields (PR5c's as-
of joins, PR5d's event-window-overlap checks, PR5d-followup's
contributing_signals reads), which have already been proven
lookahead-safe in their own PRs. The one genuinely NEW computation
here, chronological_holdout_for_taxonomy_candidate() (Gate 4 for
taxonomy-derived candidates), reuses error_classification's own
classify_all()/breakdown_by_*() functions on a chronologically-sorted,
non-overlapping discovery/validation split of the SAME already-fetched
rows -- never a new query, never shuffled (see Section 10 of the
authorization), and the split boundary is a simple index cut over rows
already ordered by prediction_ts ascending (classify_all()'s own
guarantee).

Post-event-only evidence is never allowed to upgrade a hypothesis to
PREDICTIVE. evidence_type_for_candidate() forces evidence_type=
"EXPLANATORY" whenever a candidate's underlying signal is
MISLEADING_SENTIMENT (PR3's own V1_BTC_DIVERGENCE, always
is_post_event_analysis=1) with no independently-PREDICTIVE signal
alongside it, and Gate 5 (BUILD_REQUEST eligibility) hard-requires
evidence_type != "EXPLANATORY" -- an EXPLANATORY-only hypothesis can
reach RESEARCH_HYPOTHESIS/VALIDATION_READY (it is a legitimate research
finding) but can never become a BUILD_REQUEST candidate, because a
post-hoc explanation is not evidence the original prediction could
have used.

=====================================================================
Source redundancy caveat
=====================================================================

Per PR5c's own established scope correction, "incremental beyond the
V1 composite" (Level 3, what Gate 3 checks) was NEVER shown to imply
"incremental beyond correlated/redundant sources" -- that is a
separate, larger question PR5c explicitly did not address.
source_redundancy_note() therefore NEVER reports a source candidate as
cleared "beyond its correlated group": it is always either
"UNKNOWN_STRONG_REDUNDANCY_PRESENT" (a same-direction, |r|>=0.7 partner
exists per PR5c's own pairwise/vs-composite redundancy output) or
"NOT_REDUNDANT_OBSERVED" (no such partner was found in PR5c's own
redundancy report) -- the latter is NOT a claim of Level-3-beyond-
group clearance either, just an absence of a KNOWN confound.

=====================================================================
Gate independence correction (found via the required production-
snapshot validation run, fixed before this PR was opened)
=====================================================================

An earlier draft of Gates 3 and 4 for SOURCE_INCREMENTAL_INFO
candidates both tested the identical field
(level3["oos"]["status"] == "IMPROVED"), and Gate 3 for
TAXONOMY_CONCENTRATION candidates was a bare duplicate of Gate 2's own
check. Running that draft against the real production snapshot
produced 38 of 105 source candidates reaching BUILD_REQUEST -- a
mass-discovery result flatly inconsistent with this module's own
stated purpose ("an evidence gate, not a discovery generator").
Root cause: two gates that appear independent in the ladder but read
the same underlying signal do not add discriminative power; a single
train/validation RMSE comparison is close to a coin flip for a
weak-signal source, so requiring it twice barely restricts anything.

Fixed (this PR, before any external review) by making Gate 3 read a
DIFFERENT, discovery-half-only statistic than Gate 4:
  - SOURCE_INCREMENTAL_INFO: Gate 3 now checks the discovery-half
    partial correlation magnitude (|partial_correlation| >=
    STABILITY_EPSILON, PR5c's own constant) instead of the
    validation-half OOS RMSE result Gate 4 already checks.
  - TAXONOMY_CONCENTRATION: Gate 3 now requires the concentration
    effect to clear TWICE Gate 2's bar
    (TAXONOMY_INCREMENTAL_MULTIPLIER * STABILITY_EPSILON -- not a new
    arbitrary number, just PR5c's own epsilon doubled) instead of
    repeating Gate 2's own check verbatim.
  - Gate 5 additionally requires evidence_status ==
    "STATISTICALLY_SIGNIFICANT" specifically (not merely
    STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE) before a candidate may
    become BUILD_REQUEST-eligible -- the one place "do not manufacture
    statistical significance" is enforced at the BUILD_REQUEST
    boundary. A stable-but-non-significant candidate can still reach
    RESEARCH_HYPOTHESIS / VALIDATION_READY; it just cannot reach
    BUILD_REQUEST on that basis alone.

Re-running the same snapshot after this fix produced 23 of 105 source
candidates reaching BUILD_REQUEST (see the "Known limitation" note
immediately below for why this number is still not "23 independent
discoveries").

=====================================================================
Known limitation -- horizon overlap is not corrected for
=====================================================================

Of the 23 source candidates that reach BUILD_REQUEST on the recorded
production snapshot, most are the SAME underlying source appearing at
several adjacent horizons (e.g. 'global' at 1h/3h/6h/12h/24h, 'gold' at
1h/3h/6h/12h). Forward-return windows across adjacent horizons overlap
substantially and are therefore highly autocorrelated -- a genuine (or
spurious) pattern at one horizon is likely to reappear at the next
horizon for that reason alone, not because it was independently
reconfirmed. Gate 1 (repeatability across horizons) and PR5c's own
Benjamini-Hochberg correction (across the full source x horizon grid)
do not model this within-source horizon overlap. This is an inherited
property of PR5c's already-reviewed, already-merged Level 2/3
methodology, not a new bug in this PR, and this PR does not attempt to
invent a new correction for it (that would itself be an arbitrary new
threshold the build authorization asks this PR not to introduce).
Concretely: the BUILD_REQUEST candidate count in this PR's production
snapshot should be read as "candidates worth an independent human
second look," not as a count of independently-confirmed discoveries --
this caveat is repeated in research/README.md's PR5e section and in
this PR's final report.

=====================================================================
v2 REVISION (independent review of PR #53) -- gates 3-6 strengthened
=====================================================================

A second, deeper independent review of PR #53 (23/105 BUILD_REQUEST)
re-ran the diagnostics the first review requested and found the chain
from "PR5c statistical signal" to "BUILD_REQUEST" was less independent,
gate-to-gate, than the gate numbering implied. This section documents
exactly what changed, why, and with what evidence -- the underlying
gate ARCHITECTURE (0-6, six-part decomposition, lifecycle reuse) is
unchanged; only the STRICTNESS and HONESTY of gates 3-6 changed.

Chain independence -- measured, not assumed
---------------------------------------------
Re-running the same production snapshot with the actual reviewed code:
  corr(Gate 2's r_full, Gate 3's partial_r) = 0.944 across 105 candidates
  mean |r_full - r_sy_discovery| = 0.047
  median |source-vs-composite correlation, discovery half| = 0.38
Gate 3 (partial correlation, discovery-half, controlling for composite)
is NOT materially independent of Gate 2 (full-sample bivariate
significance) in this dataset: the discovery half is 70% of the same
continuous series as the full sample, and the V1 composite's own
correlation with the outcome is usually weak at these horizons, so
partialling it out removes little. gate3_incremental_value() now
returns "independent_of_gate2": False and an explicit note on every
call, and PASSING GATE 3 IS NEVER TREATED AS CORROBORATING GATE 2 --
it is reported for completeness (the partial-correlation magnitude IS
still relevant evidence about incremental-beyond-composite information;
it is the INDEPENDENCE claim, not the metric itself, that was corrected).

Gate 4 strengthening -- the only genuinely held-out gate, now used properly
------------------------------------------------------------------------
The original Gate 4 accepted any positive `rmse_reduction_pct` (as low
as 0.0086% observed in the production snapshot -- plausibly noise on a
~135-160 row validation half). Gate 4 now requires BOTH:
  1. Magnitude: full-validation rmse_reduction_pct >=
     MEANINGFUL_OOS_IMPROVEMENT_PCT (5%). This reuses STABILITY_EPSILON
     (0.05) -- PR5c's own "minimum meaningful effect size" unit,
     already used on a correlation scale (Gate 3) and a share-difference
     scale (taxonomy Gate 2/3) -- interpreted here as a percentage on
     the RMSE-reduction scale. It was NOT picked to hit a target
     BUILD_REQUEST count; see the empirical distribution below.
  2. Stability: source_subsplit_stability() splits the validation half
     itself into two non-overlapping chronological sub-windows and
     requires the SAME discovery-fitted model to show a positive RMSE
     reduction in BOTH -- the same "does the sign replicate" pattern
     PR5c's own level2_association_for_source() already uses for its
     own discovery/validation r-split (`sign_stable`), applied one level
     deeper. Each sub-window must independently clear
     MIN_SAMPLE_FOR_HOLDOUT_HALF (30, PR5c's own, reused); if either
     can't, the result is INSUFFICIENT_DATA_FOR_HOLDOUT, never a forced
     PASSED/FAILED.
A clearly negative full-validation point estimate is still
FAILED_HOLDOUT (demotes to REJECTED, unchanged). A positive point
estimate that fails magnitude and/or stability is the new
NOT_REPLICATED validation_status -- not evidence against the
hypothesis, just insufficient to confirm it; it cannot reach
BUILD_REQUEST but is not REJECTED either.

Empirical effect of the Gate 4 change (same production snapshot, before
any threshold was chosen for its effect on the count -- the criterion
was designed first from what PR5c's own utilities support, then run):
  105 source candidates with an OK Level 3 result
    -> 50 had rmse_reduction_pct > 0 (old Gate 4 criterion)
    -> 26 replicate in the same direction across both validation
       sub-windows (stability alone)
    ->  9 clear the 5% meaningful-effect floor (magnitude alone)
    ->  8 clear BOTH (the new Gate 4 criterion)
All 8 were already among the previous 23 BUILD_REQUEST candidates (this
is a strict narrowing, not a different set). See research/README.md's
PR5e section for the full OOS distribution and the exact 8 candidates.

Gate 5 renamed: SIGNIFICANCE ELIGIBILITY, not independent validation
---------------------------------------------------------------------
The old gate5_build_request_eligible() folded "all priors passed" +
"not explanatory" + "is significant" into one function. It is now split:
gate5_significance_eligibility() is ONLY the significance re-check
(explicitly documented as a stricter re-read of Gate 2's own metric,
never independent evidence), and gate6_build_request_eligible() is the
actual final promotion gate (gates 0-5 + evidence_type +
redundancy_status).

Gate 6 added: BUILD_REQUEST now also requires resolved redundancy
--------------------------------------------------------------------
A candidate whose source_redundancy_note is REDUNDANCY_UNRESOLVED (a
strong, |r|>=0.7 pairwise or vs-composite partner exists) can no longer
reach BUILD_REQUEST, even if every other gate passes -- it caps at
VALIDATION_READY. In the production snapshot this removed `etfflows`
(r vs. V1 composite = 0.75) from the final list, leaving 7 BUILD_REQUEST
candidates across 3 distinct source families (fng, global, onchain) --
see signal_family_summary in the report and research/README.md.

Redundancy terminology corrected
-----------------------------------
"UNKNOWN_STRONG_REDUNDANCY_PRESENT" -> "REDUNDANCY_UNRESOLVED" (clearer:
this is not "unknown," it is a specific, confirmed unresolved overlap).
"NOT_REDUNDANT_OBSERVED" -> "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED"
(the old name could be misread as "independence established"; the new
name states precisely and only what was checked: no single pairwise/
vs-composite Pearson |r| crossed 0.7). Concrete counter-example, kept in
the module and README so this is never re-forgotten: 'global' and
'gold' correlate at r=-0.624 in the production snapshot -- both were
independently reaching BUILD_REQUEST-adjacent status under the OLD
Gate 4 criterion, comfortably below the 0.7 pairwise threshold, so
NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED would have applied to both
without ruling out this shared information. (Under the NEW Gate 4
criterion, 'gold' no longer reaches this far regardless, but the
terminology fix stands on its own and is not contingent on that.)

Horizon-family reporting added
----------------------------------
build_hypothesis_report()'s new `signal_family_summary` groups
SOURCE_INCREMENTAL_INFO candidates by source_key (the natural, already-
existing grouping unit -- no new clustering algorithm) and reports
candidate count vs. distinct-family count separately, so a reader never
mistakes "23 BUILD_REQUEST candidates" for "23 independent discoveries"
when several are the same source at adjacent, overlapping horizons.

Funnel / auditability added
-------------------------------
Every hypothesis record now carries `first_failed_gate` and
`first_failed_reason` (the first of gates 0-6, in order, it did not
pass, and that gate's own result dict) -- `build_hypothesis_report()`'s
new `gate_funnel` aggregates these into a full 105-candidates-in,
N-candidates-through-each-gate funnel. No candidate's fate is hidden.
"""

import sys
import os
import json

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402
import error_classification as ec  # noqa: E402
import error_overlap_analysis as eoa  # noqa: E402
import outcome_engine  # noqa: E402
from stats_utils import ols_1var, ols_2var, rmse  # noqa: E402

# ---------------------------------------------------------------------
# Reused constants -- no new arbitrary numbers. Every threshold below
# is imported from the module that already defined and justified it.
# ---------------------------------------------------------------------
MIN_SAMPLE_FOR_GATE = sa.MIN_SAMPLE_FOR_LEVEL2         # 30, PR5c's own
MIN_SAMPLE_FOR_HOLDOUT_HALF = sa.MIN_SAMPLE_FOR_CONTRADICTED  # 30, PR5c's own
MIN_SAMPLE_FOR_LEVEL3 = sa.MIN_SAMPLE_FOR_LEVEL3       # 40, PR5c's own
OOS_SPLIT_FRACTION = sa.OOS_SPLIT_FRACTION             # 0.7, PR5c's own
STABILITY_EPSILON = sa.STABILITY_EPSILON               # 0.05, PR5c's own
STRONG_REDUNDANCY_THRESHOLD = sa.STRONG_REDUNDANCY_THRESHOLD  # 0.7, PR5c's own
# Not a new arbitrary threshold: twice PR5c's own STABILITY_EPSILON, used only
# to make Gate 3 (incremental value) for TAXONOMY_CONCENTRATION candidates a
# STRICTER bar than Gate 2 (association) instead of duplicating it outright
# (see gate3_incremental_value()'s docstring for why duplication was a bug).
TAXONOMY_INCREMENTAL_MULTIPLIER = 2
# v2 revision (independent review of PR #53): STABILITY_EPSILON (0.05),
# already PR5c's own "minimum meaningful effect size" unit on a correlation
# scale (Gate 3) and a share-difference scale (taxonomy Gate 2/3), reused a
# THIRD time here as a percentage on the RMSE-reduction scale: a validation-
# half OOS RMSE reduction must be at least 5% before it counts as
# "meaningful," not merely "not exactly zero." Not a new number picked to
# hit a target BUILD_REQUEST count -- see module docstring's "Gate 4
# strengthening" section for the empirical justification.
MEANINGFUL_OOS_IMPROVEMENT_PCT = STABILITY_EPSILON * 100  # 5.0

# The ten-stage lifecycle PR5a's migration already documents verbatim.
# Reused, not reinvented.
LIFECYCLE_STATUSES = (
    "OBSERVATION",
    "MONITOR",
    "RESEARCH_HYPOTHESIS",
    "VALIDATION_READY",
    "BUILD_REQUEST",
    "AWAITING_APPROVAL",
    "IMPLEMENTED",
    "VALIDATED",
    "REJECTED",
    "ROLLED_BACK",
)
# This module never assigns anything past this point in LIFECYCLE_STATUSES.
PR5E_MAX_LIFECYCLE_STATUS = "BUILD_REQUEST"
_ALLOWED_ASSIGNABLE_STATUSES = ("OBSERVATION", "MONITOR", "RESEARCH_HYPOTHESIS",
                                 "VALIDATION_READY", "BUILD_REQUEST", "REJECTED")

# PR5c's own four implemented evidence labels (classify_evidence()),
# reused verbatim, plus ONE new label this PR adds for the case the
# authorization explicitly calls for: "return UNKNOWN / INSUFFICIENT_
# EVIDENCE rather than forcing a decision." Free-text column, no
# migration needed for a new string value.
EVIDENCE_STATUS_LABELS = (
    "STATISTICALLY_SIGNIFICANT",
    "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE",
    "INCONCLUSIVE",
    "CONTRADICTED",
    "INSUFFICIENT_EVIDENCE",
)

VALIDATION_STATUSES = (
    "NOT_YET_TESTED",
    "PASSED_HOLDOUT",
    # v2 addition: the full-validation-half point estimate was positive
    # but did NOT clear MEANINGFUL_OOS_IMPROVEMENT_PCT, and/or did not
    # replicate in the SAME direction across both chronological
    # sub-windows of the validation half. This is deliberately NOT
    # "FAILED_HOLDOUT" (the point estimate was not negative -- there is
    # no clear evidence AGAINST the hypothesis) and NOT "PASSED_HOLDOUT"
    # (a single positive point estimate is not, by itself, replication).
    # A label PR5a's own migration 0008 docstring already anticipated
    # for exactly this "positive but not independently reconfirmed"
    # case -- reused, not invented.
    "NOT_REPLICATED",
    "FAILED_HOLDOUT",
    "INSUFFICIENT_DATA_FOR_HOLDOUT",
)

EVIDENCE_TYPES = ("PREDICTIVE", "EXPLANATORY", "MIXED")

CANDIDATE_TYPES = ("SOURCE_INCREMENTAL_INFO", "TAXONOMY_CONCENTRATION")

# Source-redundancy labels (v2 rename -- see module docstring's "Redundancy
# terminology" section for why the old names were misleading):
#   REDUNDANCY_UNRESOLVED: a same-direction, |r|>=STRONG_REDUNDANCY_THRESHOLD
#     partner (another source, or the V1 composite) exists in the pairwise/
#     vs-composite comparisons this module checked.
#   NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED: no such partner was found in
#     THOSE comparisons. This is NOT a claim of independence -- it only
#     means no single pairwise Pearson |r| crossed the 0.7 threshold. It
#     says nothing about joint/multi-source multicollinearity below that
#     threshold, and nothing about correlation structure the pairwise/vs-
#     composite check does not evaluate.
#   NOT_APPLICABLE: TAXONOMY_CONCENTRATION candidates (redundancy analysis
#     is source-only).
REDUNDANCY_STATUSES = (
    "REDUNDANCY_UNRESOLVED",
    "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED",
    "NOT_APPLICABLE",
)


# =====================================================================
# Candidate derivation -- from PR5c's report only (source-based)
# =====================================================================

def derive_source_candidates(source_report):
    """One candidate per (source, horizon) that PR5c's own battery
    actually ran, grouped by source so Gate 1 (repeatability across
    horizons) can be evaluated. Never invents a candidate PR5c's report
    doesn't already contain.
    """
    tests = source_report["level2"]["tests"]
    by_source = {}
    for (source_key, horizon_hours), level2_result in tests.items():
        by_source.setdefault(source_key, []).append((horizon_hours, level2_result))

    candidates = []
    for source_key, horizon_results in by_source.items():
        for horizon_hours, level2_result in horizon_results:
            key = f"{source_key}|{horizon_hours}h"
            evidence_label = source_report["evidence_labels"].get(key)
            other_horizons_significant = sum(
                1 for h, r in horizon_results
                if h != horizon_hours
                and source_report["evidence_labels"].get(f"{source_key}|{h}h")
                in ("STATISTICALLY_SIGNIFICANT", "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE")
            )
            this_one_significant = evidence_label in (
                "STATISTICALLY_SIGNIFICANT", "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE"
            )
            candidates.append({
                "candidate_type": "SOURCE_INCREMENTAL_INFO",
                "subject": f"source:{source_key}:{horizon_hours}h",
                "source_key": source_key,
                "horizon_hours": horizon_hours,
                "level2": level2_result,
                "level3": source_report["level3"].get(key),
                "evidence_label": evidence_label,
                "repeatability_count": (1 if this_one_significant else 0) + other_horizons_significant,
                "redundancy_pairwise": {
                    pair: info for pair, info in source_report["redundancy"]["pairwise"].items()
                    if source_key in pair and info["strong_redundancy"]
                },
                "redundancy_vs_composite": source_report["redundancy"]["vs_composite"].get(source_key),
            })
    return candidates


# =====================================================================
# Candidate derivation -- from PR5d / PR5d-followup reports (taxonomy)
# =====================================================================

def derive_taxonomy_candidates(ec_report, overlap_report):
    """One candidate per PR5d's own candidate_observations entry
    (already gated at n>=CANDIDATE_MIN_N and share>=CANDIDATE_MIN_SHARE
    by error_classification.py itself -- never re-derived here), cross-
    referenced against PR5d-followup's matched_category_distribution
    for the "beyond baseline" comparison Gate 3 needs.
    """
    candidates = []
    for horizon_hours, ph in ec_report["per_horizon"].items():
        overlap_ph = overlap_report["per_horizon"].get(horizon_hours, {})
        matched_dist = overlap_ph.get("matched_category_distribution", {})
        n_evaluable = matched_dist.get("n_evaluable", 0)
        matched_counts = matched_dist.get("matched_counts", {})

        for obs in ph["candidate_observations"]:
            category = obs["dominant_error_type"]
            baseline_share = (
                (matched_counts.get(category, 0) / n_evaluable) if n_evaluable > 0 else None
            )
            candidates.append({
                "candidate_type": "TAXONOMY_CONCENTRATION",
                "subject": f"taxonomy:{category}:{obs['dimension']}:{obs['bucket']}:{horizon_hours}h",
                "horizon_hours": horizon_hours,
                "category": category,
                "dimension": obs["dimension"],
                "bucket": obs["bucket"],
                "n_resolved": obs["n_resolved"],
                "concentrated_share": obs["share"],
                "baseline_share": baseline_share,
                "n_evaluable_baseline": n_evaluable,
                "evidence_gate_status_from_pr5d": obs["evidence_gate_status"],
                "suppression": overlap_ph.get("suppression_analysis", {}).get(category),
            })
    return candidates


# =====================================================================
# Evidence type -- post-event evidence can never become predictive
# =====================================================================

def evidence_type_for_candidate(candidate):
    """PREDICTIVE by default. Forced to EXPLANATORY for any candidate
    whose evidence is inherently post-event-only (PR3's
    MISLEADING_SENTIMENT / V1_BTC_DIVERGENCE, always
    is_post_event_analysis=1 in event_classification's own output) --
    never upgraded regardless of how strong the pattern looks.
    Source-based candidates (PR5c) are always PREDICTIVE: every V1
    value they use is from the as-of join, strictly at-or-before
    prediction_ts, by PR5c's own proven construction.
    """
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        return "PREDICTIVE"
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        if candidate["category"] == "MISLEADING_SENTIMENT":
            return "EXPLANATORY"
        return "PREDICTIVE"
    return "EXPLANATORY"


# =====================================================================
# Source redundancy caveat (never claims Level-3-beyond-group)
# =====================================================================

def source_redundancy_note(candidate):
    """Returns one of REDUNDANCY_STATUSES. NEVER returns a label implying
    independence was established -- see REDUNDANCY_STATUSES' own
    docstring above and the module docstring's "Redundancy terminology"
    section for the concrete counter-example (global<->gold correlate at
    r=-0.624 in the production snapshot, well below the 0.7 pairwise
    threshold, yet both independently reach this far in the gate
    pipeline -- NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED does not rule
    that out)."""
    if candidate["candidate_type"] != "SOURCE_INCREMENTAL_INFO":
        return "NOT_APPLICABLE"
    if candidate["redundancy_pairwise"]:
        return "REDUNDANCY_UNRESOLVED"
    vs_composite = candidate.get("redundancy_vs_composite")
    if vs_composite and vs_composite.get("strong_redundancy"):
        return "REDUNDANCY_UNRESOLVED"
    return "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED"


# =====================================================================
# Gates 0-5
# =====================================================================

def gate0_observation(candidate):
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        n = candidate["level2"].get("n", 0)
        passed = candidate["level2"].get("status") == "OK" and n > 0
        return {"passed": passed, "n": n, "reason": "level2 status/n" if passed else "no measurable sample"}
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        n = candidate["n_resolved"]
        passed = n > 0
        return {"passed": passed, "n": n, "reason": "concentrated bucket has resolved predictions"}
    return {"passed": False, "n": 0, "reason": "unknown candidate type"}


def gate1_repeatability(candidate):
    """The pattern appears in >1 independent observation/window."""
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        count = candidate["repeatability_count"]
        return {"passed": count >= 2, "independent_instances": count,
                "reason": "significant/stable at >=2 horizons" if count >= 2
                else "only one horizon shows a non-null association"}
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        recurring = candidate["evidence_gate_status_from_pr5d"] == "RESEARCH_HYPOTHESIS"
        return {"passed": recurring, "independent_instances": (2 if recurring else 1),
                "reason": "PR5d's own recurrence-across-dimensions check"
                if recurring else "observed in only one breakdown dimension"}
    return {"passed": False, "independent_instances": 0, "reason": "unknown candidate type"}


def gate2_association(candidate):
    """The relationship with the relevant outcome metric is measurable
    -- reuses PR5c's classify_evidence() for source candidates (never
    re-derives significance from a bare p<0.05, per Section: 'do not
    manufacture statistical significance')."""
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        label = candidate["evidence_label"]
        passed = label in ("STATISTICALLY_SIGNIFICANT", "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE")
        return {"passed": passed, "evidence_label": label,
                "effect_size_r": candidate["level2"].get("effect_size_r"),
                "n": candidate["level2"].get("n"),
                "p_corrected": candidate["level2"].get("p_corrected")}
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        baseline = candidate["baseline_share"]
        concentrated = candidate["concentrated_share"]
        if baseline is None or candidate["n_resolved"] < MIN_SAMPLE_FOR_GATE:
            return {"passed": False, "reason": "insufficient baseline/sample", "n": candidate["n_resolved"]}
        effect = concentrated - baseline
        passed = effect >= STABILITY_EPSILON
        return {"passed": passed, "concentrated_share": concentrated, "baseline_share": baseline,
                "effect_vs_baseline": effect, "n": candidate["n_resolved"]}
    return {"passed": False, "reason": "unknown candidate type"}


def gate3_incremental_value(candidate):
    """Beyond the V1 composite (source candidates) or beyond the
    baseline rate (taxonomy candidates) -- NEVER beyond the correlated
    source group (see source_redundancy_note(), always UNKNOWN there).

    IMPORTANT (post-validation-run correction): this gate must be
    evidence INDEPENDENT of Gate 4 (out-of-sample validation) and of
    Gate 2 (association), or the two-of-five-gate ladder collapses into
    fewer effectively-independent checks than it appears to have --
    which is exactly what a real production-snapshot run surfaced (a
    large fraction of source candidates reached BUILD_REQUEST because
    Gate 3 and Gate 4 both tested the identical
    level3.oos.status=="IMPROVED" field, and Gate 3 for taxonomy
    candidates was a bare duplicate of Gate 2). Concretely:

    - SOURCE_INCREMENTAL_INFO: uses the DISCOVERY-half partial
      correlation (source vs. outcome, controlling for the V1
      composite) that PR5c's level3_incremental_for_source() already
      computes -- a different half of the data and a different
      statistic than Gate 4's validation-half OOS RMSE comparison.
    - TAXONOMY_CONCENTRATION: requires the concentration effect to
      clear TWICE Gate 2's bar (TAXONOMY_INCREMENTAL_MULTIPLIER *
      STABILITY_EPSILON), not merely repeat Gate 2's own check.
    """
    NOT_INDEPENDENT_OF_GATE2_NOTE = (
        "Gate 3's basis (discovery-half partial correlation) and Gate 2's "
        "basis (full-sample bivariate r) are empirically correlated at "
        "r=0.944 across this PR's 105-candidate production snapshot -- "
        "see module docstring's 'Chain independence' section. Passing "
        "Gate 3 must NEVER be read as a confirmation independent of "
        "Gate 2; it is reported for completeness, not as corroborating "
        "evidence, and does not by itself justify BUILD_REQUEST."
    )
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        level3 = candidate.get("level3") or {}
        if level3.get("status") != "OK":
            return {"passed": False, "beyond_composite": "INSUFFICIENT_DATA",
                    "beyond_correlated_group": source_redundancy_note(candidate),
                    "independent_of_gate2": False, "note": NOT_INDEPENDENT_OF_GATE2_NOTE}
        partial_r = level3.get("partial_correlation")
        passed = partial_r is not None and abs(partial_r) >= STABILITY_EPSILON
        return {
            "passed": passed,
            "beyond_composite": "MEASURABLE_PARTIAL_ASSOCIATION" if passed else "NO_PARTIAL_ASSOCIATION",
            "partial_correlation": partial_r,
            "beyond_correlated_group": source_redundancy_note(candidate),
            "independent_of_gate2": False,
            "note": NOT_INDEPENDENT_OF_GATE2_NOTE,
        }
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        baseline = candidate["baseline_share"]
        concentrated = candidate["concentrated_share"]
        if baseline is None or candidate["n_resolved"] < MIN_SAMPLE_FOR_GATE:
            return {"passed": False, "reason": "insufficient baseline/sample",
                    "n": candidate["n_resolved"], "beyond_correlated_group": "NOT_APPLICABLE",
                    "independent_of_gate2": False, "note": NOT_INDEPENDENT_OF_GATE2_NOTE}
        effect = concentrated - baseline
        passed = effect >= TAXONOMY_INCREMENTAL_MULTIPLIER * STABILITY_EPSILON
        return {"passed": passed, "beyond_baseline": passed,
                "effect_vs_baseline": effect,
                "required_effect": TAXONOMY_INCREMENTAL_MULTIPLIER * STABILITY_EPSILON,
                "beyond_correlated_group": "NOT_APPLICABLE",
                "independent_of_gate2": False, "note": NOT_INDEPENDENT_OF_GATE2_NOTE}
    return {"passed": False, "reason": "unknown candidate type"}


def chronological_holdout_for_taxonomy_candidate(rows, classifications, candidate,
                                                  split_fraction=OOS_SPLIT_FRACTION):
    """Gate 4 for TAXONOMY_CONCENTRATION candidates -- genuinely new
    logic (neither PR5d nor PR5d-followup compute a holdout split), but
    built entirely from error_classification's own, unmodified
    breakdown_by_*() functions over a chronological (never shuffled)
    split of the SAME already-fetched rows/classifications. No new
    query, no new threshold beyond OOS_SPLIT_FRACTION (PR5c's own).
    """
    n = len(rows)
    split_idx = int(n * split_fraction)
    discovery_rows, validation_rows = rows[:split_idx], rows[split_idx:]
    discovery_c, validation_c = classifications[:split_idx], classifications[split_idx:]

    dimension = candidate["dimension"]
    bucket = candidate["bucket"]
    breakdown_fn = {
        "v1_composite_bucket": ec.breakdown_by_v1_bucket,
        "regime": ec.breakdown_by_regime,
        "model_version": ec.breakdown_by_model_version,
    }.get(dimension)
    if breakdown_fn is None:
        return {"status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "reason": f"no holdout breakdown for dimension {dimension}"}

    def bucket_share(rows_half, class_half):
        breakdown = breakdown_fn(rows_half, class_half)
        b = breakdown.get(bucket)
        if b is None or b["n_resolved"] < MIN_SAMPLE_FOR_HOLDOUT_HALF:
            return None, (b["n_resolved"] if b else 0)
        return b["error_counts"].get(candidate["category"], 0) / b["n_resolved"], b["n_resolved"]

    d_share, d_n = bucket_share(discovery_rows, discovery_c)
    v_share, v_n = bucket_share(validation_rows, validation_c)

    if d_share is None or v_share is None:
        return {"status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "n_discovery": d_n, "n_validation": v_n}

    baseline = candidate["baseline_share"] or 0.0
    both_elevated = (d_share - baseline >= STABILITY_EPSILON) and (v_share - baseline >= STABILITY_EPSILON)
    return {
        "status": "PASSED_HOLDOUT" if both_elevated else "FAILED_HOLDOUT",
        "n_discovery": d_n, "n_validation": v_n,
        "discovery_share": d_share, "validation_share": v_share, "baseline_share": baseline,
    }


def source_subsplit_stability(matrix_rows, outcome_rows, source_key, horizon_hours,
                               split_fraction=OOS_SPLIT_FRACTION):
    """v2 addition: Gate 4 support for SOURCE_INCREMENTAL_INFO candidates.

    The original Gate 4 ("rmse_reduction_pct > 0") accepted a SINGLE
    point estimate on the validation half as sufficient -- independent
    review of PR #53 found several BUILD_REQUEST candidates whose entire
    out-of-sample evidence was an improvement as small as 0.0086%,
    plausibly noise on a ~135-160 row validation half. This function
    asks a second, genuinely new question with the SAME already-fetched
    data: does the discovery-fitted nested model's RMSE advantage
    replicate in the SAME direction across TWO non-overlapping
    chronological sub-windows of the validation half, not just show up
    once?

    Built entirely from primitives PR5c's own source_analysis.py already
    uses and exposes (extract_source_matrix()'s row shape,
    outcome_engine.compute_forward_returns_from_history(), and
    stats_utils.ols_1var/ols_2var/rmse) -- no new statistical technique,
    no reshuffled data, no new query against predictions/history/
    btc_data beyond what source_analysis.py's own report already issues
    for this exact source/horizon. The model itself (baseline: composite
    only; full: composite + source) is fit ONCE on the discovery half,
    exactly as level3_incremental_for_source() does -- this function
    only evaluates that already-fitted model's RMSE on two sub-halves of
    the validation data as a stability check, it does not refit per
    sub-half (refitting on ~70-80 rows each would be its own new,
    weaker-sample statistical claim).

    Never shuffled: `matrix_rows` and `outcome_rows` are sorted by ts
    ascending before splitting, identical ordering discipline to
    level3_incremental_for_source() and chronological_holdout_for_
    taxonomy_candidate().
    """
    outcome_by_ts = {o["anchor_ts"]: o for o in outcome_rows}
    combined = []
    for row in matrix_rows:
        ts = row["ts"]
        source_value = row["sources"].get(source_key)
        composite_value = row["v1_composite"]
        outcome = outcome_by_ts.get(ts)
        if source_value is None or composite_value is None:
            continue
        if outcome is None or outcome["outcome_status"] != "RESOLVED":
            continue
        combined.append((ts, float(source_value), float(composite_value), outcome["forward_return_pct"]))
    combined.sort(key=lambda t: t[0])

    n = len(combined)
    if n < MIN_SAMPLE_FOR_LEVEL3:
        return {"status": "INSUFFICIENT_DATA"}
    split_idx = int(n * split_fraction)
    discovery, validation = combined[:split_idx], combined[split_idx:]
    if len(discovery) < 10 or len(validation) < 10:
        return {"status": "INSUFFICIENT_DATA"}

    d_composite = [d[2] for d in discovery]
    d_source = [d[1] for d in discovery]
    d_y = [d[3] for d in discovery]
    baseline_slope, baseline_intercept = ols_1var(d_composite, d_y)
    full_b0, full_b1, full_b2 = ols_2var(d_composite, d_source, d_y)
    if baseline_slope is None or full_b0 is None:
        return {"status": "MODEL_UNDEFINED"}

    mid = len(validation) // 2
    sub_halves = (validation[:mid], validation[mid:])

    def rmse_reduction_for(sub):
        if len(sub) < MIN_SAMPLE_FOR_HOLDOUT_HALF:
            return None, len(sub)
        vc = [d[2] for d in sub]
        vs = [d[1] for d in sub]
        vy = [d[3] for d in sub]
        baseline_preds = [baseline_intercept + baseline_slope * c for c in vc]
        full_preds = [full_b0 + full_b1 * c + full_b2 * s for c, s in zip(vc, vs)]
        baseline_rmse = rmse(vy, baseline_preds)
        full_model_rmse = rmse(vy, full_preds)
        if baseline_rmse is None or baseline_rmse == 0:
            return None, len(sub)
        return (baseline_rmse - full_model_rmse) / baseline_rmse * 100.0, len(sub)

    red1, n1 = rmse_reduction_for(sub_halves[0])
    red2, n2 = rmse_reduction_for(sub_halves[1])
    if red1 is None or red2 is None:
        return {"status": "INSUFFICIENT_DATA_FOR_SUBSPLIT", "n_sub1": n1, "n_sub2": n2}

    return {
        "status": "OK",
        "n_sub1": n1, "n_sub2": n2,
        "rmse_reduction_pct_sub1": red1, "rmse_reduction_pct_sub2": red2,
        "sign_stable_across_subsplit": (red1 > 0) and (red2 > 0),
    }


def gate4_out_of_sample(candidate, rows=None, classifications=None,
                         source_matrix_rows=None, source_outcome_rows=None):
    """v2 revision: for SOURCE_INCREMENTAL_INFO candidates, a positive
    rmse_reduction_pct point estimate ALONE is no longer sufficient
    (see source_subsplit_stability()'s docstring for why). The gate now
    requires BOTH:
      1. magnitude: full-validation rmse_reduction_pct >=
         MEANINGFUL_OOS_IMPROVEMENT_PCT (5%, PR5c's own STABILITY_EPSILON
         reused on a percentage scale -- not a new number).
      2. stability: the SAME improvement direction replicates across two
         non-overlapping chronological sub-windows of that validation
         half (source_subsplit_stability()).
    A clearly NEGATIVE full-validation point estimate (the model got
    WORSE out of sample) is still FAILED_HOLDOUT, demoting to REJECTED,
    exactly as before. A POSITIVE point estimate that fails magnitude
    and/or stability is NOT_REPLICATED -- evidence is not against the
    hypothesis, just insufficient to confirm it; this does not demote to
    REJECTED, it simply cannot reach BUILD_REQUEST.
    TAXONOMY_CONCENTRATION is unchanged: chronological_holdout_for_
    taxonomy_candidate() already requires BOTH halves to independently
    clear STABILITY_EPSILON above baseline, which already combines
    magnitude and stability -- no change was needed there.
    """
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        level3 = candidate.get("level3") or {}
        if level3.get("status") != "OK":
            return {"validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False}
        full_red = (level3.get("oos") or {}).get("rmse_reduction_pct")
        if full_red is None:
            return {"validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False}
        if full_red <= 0:
            return {
                "validation_status": "FAILED_HOLDOUT", "passed": False,
                "rmse_reduction_pct": full_red,
                "n_discovery": level3.get("n_discovery"), "n_validation": level3.get("n_validation"),
            }
        if source_matrix_rows is None or source_outcome_rows is None:
            return {"validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False,
                    "rmse_reduction_pct": full_red}
        subsplit = source_subsplit_stability(
            source_matrix_rows, source_outcome_rows, candidate["source_key"], candidate["horizon_hours"])
        if subsplit["status"] != "OK":
            return {
                "validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False,
                "rmse_reduction_pct": full_red, "subsplit": subsplit,
            }
        meaningful = full_red >= MEANINGFUL_OOS_IMPROVEMENT_PCT
        stable = subsplit["sign_stable_across_subsplit"]
        passed = meaningful and stable
        return {
            "validation_status": "PASSED_HOLDOUT" if passed else "NOT_REPLICATED",
            "passed": passed,
            "rmse_reduction_pct": full_red,
            "meaningful_effect": meaningful,
            "required_effect_pct": MEANINGFUL_OOS_IMPROVEMENT_PCT,
            "sign_stable_across_subsplit": stable,
            "subsplit": subsplit,
            "n_discovery": level3.get("n_discovery"), "n_validation": level3.get("n_validation"),
        }
    if candidate["candidate_type"] == "TAXONOMY_CONCENTRATION":
        if rows is None or classifications is None:
            return {"validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False}
        result = chronological_holdout_for_taxonomy_candidate(rows, classifications, candidate)
        return {"validation_status": result["status"], "passed": result["status"] == "PASSED_HOLDOUT",
                **{k: v for k, v in result.items() if k != "status"}}
    return {"validation_status": "INSUFFICIENT_DATA_FOR_HOLDOUT", "passed": False}


def gate5_significance_eligibility(evidence_status):
    """v2 rename (was folded into the old 'gate5_build_request_eligible').
    Renamed and split out on its own per independent review: this is a
    STRICTER FORM OF GATE 2's OWN METRIC (PR5c's classify_evidence()
    label), not an independent evidence layer -- it re-reads the same
    p_corrected computation Gate 2 already used, just requires the
    label to be STATISTICALLY_SIGNIFICANT specifically, not merely
    STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE. A stable-but-non-
    significant candidate can still reach RESEARCH_HYPOTHESIS/
    VALIDATION_READY via Gate 2; it cannot pass this gate, and therefore
    cannot reach BUILD_REQUEST (Gate 6) on that basis alone."""
    is_significant = evidence_status == "STATISTICALLY_SIGNIFICANT"
    return {"passed": is_significant, "evidence_status": evidence_status,
            "note": "Significance eligibility -- a stricter re-read of Gate 2's own "
                    "statistic, NOT independent confirmatory evidence."}


def gate6_build_request_eligible(gate_results, evidence_type, redundancy_note):
    """The final BUILD_REQUEST gate. Requires ALL of:
      - Gates 0-4 passed (observation, repeatability, association,
        incremental information, and the strengthened out-of-sample
        validation).
      - Gate 5 passed (significance eligibility -- STATISTICALLY_
        SIGNIFICANT specifically).
      - evidence_type != EXPLANATORY (a post-event-only explanation is
        not evidence the original prediction could have used).
      - redundancy_note != REDUNDANCY_UNRESOLVED. Per independent
        review: 'A candidate that is significant but has ... unresolved
        redundancy ... must NOT automatically become BUILD_REQUEST.' A
        candidate whose only known redundancy signal is
        NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED (never a claim of
        independence, see REDUNDANCY_STATUSES) may still proceed --
        this gate blocks only the CONFIRMED-unresolved case, it cannot
        block what pairwise/vs-composite redundancy checking does not
        cover (see module docstring's 'Redundancy terminology' section).
    BUILD_REQUEST means 'candidate worthy of human review for a
    possible future implementation,' never 'validated.'
    """
    all_prior_passed = all(gate_results[g]["passed"] for g in ("gate0", "gate1", "gate2", "gate3", "gate4", "gate5"))
    not_explanatory_only = evidence_type != "EXPLANATORY"
    redundancy_resolved = redundancy_note != "REDUNDANCY_UNRESOLVED"
    passed = all_prior_passed and not_explanatory_only and redundancy_resolved
    return {
        "passed": passed,
        "all_prior_gates_passed": all_prior_passed,
        "blocked_by_explanatory_only_evidence": all_prior_passed and not not_explanatory_only,
        "blocked_by_unresolved_redundancy": all_prior_passed and not_explanatory_only and not redundancy_resolved,
        "redundancy_note": redundancy_note,
    }


# =====================================================================
# Lifecycle assignment
# =====================================================================

def assign_lifecycle_status(gate_results):
    """Deterministic, gate-driven. A FAILED_HOLDOUT (gate4 explicitly
    failed -- a clearly negative OOS point estimate, not merely
    NOT_REPLICATED/insufficient) demotes straight to REJECTED -- an
    existing lifecycle stage, reused for exactly this purpose -- never
    silently promoted past it. Gate 5 (significance eligibility) has no
    rung of its own -- like evidence_type, it is an input to Gate 6
    only, never a standalone lifecycle milestone."""
    # Gate 4 is evaluated regardless of earlier gate outcomes (evaluate_
    # candidate() always computes it), so a candidate can fail gate1/2/3
    # (first_failed_gate reports that EARLIER failure for audit purposes)
    # AND separately show a clearly negative full-validation OOS point
    # estimate -- REJECTED still wins over the ladder position in that
    # case: explicit negative evidence is a stronger, more informative
    # verdict than "insufficient repeatability" alone, and burying it
    # behind an earlier, weaker failure would hide real evidence against
    # the hypothesis. first_failed_gate still correctly names gate1/2/3
    # as the first thing that went wrong; lifecycle_status separately
    # reports the strongest available verdict.
    if gate_results["gate4"].get("validation_status") == "FAILED_HOLDOUT":
        return "REJECTED"
    if gate_results["gate6"]["passed"]:
        return "BUILD_REQUEST"
    if gate_results["gate4"]["passed"]:
        return "VALIDATION_READY"
    if gate_results["gate3"]["passed"]:
        return "VALIDATION_READY"
    if gate_results["gate2"]["passed"]:
        return "RESEARCH_HYPOTHESIS"
    if gate_results["gate1"]["passed"]:
        return "MONITOR"
    if gate_results["gate0"]["passed"]:
        return "OBSERVATION"
    return "OBSERVATION"


_GATE_ORDER = ("gate0", "gate1", "gate2", "gate3", "gate4", "gate5", "gate6")


def first_failed_gate(gate_results):
    """Auditability requirement: for every candidate, the first gate (in
    order 0-6) it did not pass, plus that gate's own result dict as the
    reason. Returns (None, None) if all seven passed."""
    for gate_name in _GATE_ORDER:
        if not gate_results[gate_name]["passed"]:
            return gate_name, gate_results[gate_name]
    return None, None


def assign_evidence_status(candidate, gate_results):
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        label = candidate.get("evidence_label")
        return label if label in EVIDENCE_STATUS_LABELS else "INSUFFICIENT_EVIDENCE"
    if not gate_results["gate0"]["passed"] or not gate_results["gate2"].get("passed", False):
        return "INSUFFICIENT_EVIDENCE" if not gate_results["gate2"].get("passed", False) and \
            gate_results["gate2"].get("reason") == "insufficient baseline/sample" else "INCONCLUSIVE"
    return "STATISTICALLY_NON_SIGNIFICANT_BUT_STABLE" if gate_results["gate1"]["passed"] else "INCONCLUSIVE"


# =====================================================================
# Six-part decomposition + orchestration per candidate
# =====================================================================

def build_hypothesis_statement(candidate):
    if candidate["candidate_type"] == "SOURCE_INCREMENTAL_INFO":
        return (
            f"V1 source '{candidate['source_key']}' may carry information about "
            f"subsequent BTC {candidate['horizon_hours']}h returns beyond the V1 "
            f"composite (testable, NOT established as true)."
        )
    return (
        f"Predictions in {candidate['dimension']}={candidate['bucket']!r} at "
        f"{candidate['horizon_hours']}h may be disproportionately classified "
        f"{candidate['category']} relative to the overall base rate "
        f"(testable, NOT established as true)."
    )


def evaluate_candidate(candidate, rows=None, classifications=None,
                        source_matrix_rows=None, source_outcome_rows=None):
    """The single entry point per candidate. Never writes. Returns the
    full six-part decomposition + gate results (gate0-gate6) +
    lifecycle/evidence/validation status + evidence_type + redundancy
    note + first-failed-gate audit trail.
    """
    gate_results = {
        "gate0": gate0_observation(candidate),
        "gate1": gate1_repeatability(candidate),
        "gate2": gate2_association(candidate),
        "gate3": gate3_incremental_value(candidate),
    }
    gate_results["gate4"] = gate4_out_of_sample(
        candidate, rows=rows, classifications=classifications,
        source_matrix_rows=source_matrix_rows, source_outcome_rows=source_outcome_rows,
    )
    evidence_type = evidence_type_for_candidate(candidate)
    evidence_status = assign_evidence_status(candidate, gate_results)
    redundancy_note = source_redundancy_note(candidate)
    gate_results["gate5"] = gate5_significance_eligibility(evidence_status)
    gate_results["gate6"] = gate6_build_request_eligible(gate_results, evidence_type, redundancy_note)

    lifecycle_status = assign_lifecycle_status(gate_results)
    validation_status = gate_results["gate4"].get("validation_status", "NOT_YET_TESTED")
    failed_gate, failed_reason = first_failed_gate(gate_results)

    return {
        "subject": candidate["subject"],
        "candidate_type": candidate["candidate_type"],
        "observation": {
            "candidate_type": candidate["candidate_type"],
            "n": gate_results["gate0"]["n"],
            "detail": {k: v for k, v in candidate.items() if k not in ("level2", "level3")},
        },
        "association": gate_results["gate2"],
        "incremental_information": gate_results["gate3"],
        "oos_validation": gate_results["gate4"],
        "significance_eligibility": gate_results["gate5"],
        "hypothesis_statement": build_hypothesis_statement(candidate),
        "evidence_status": evidence_status,
        "validation_status": validation_status,
        "lifecycle_status": lifecycle_status,
        "evidence_type": evidence_type,
        "source_redundancy_note": redundancy_note,
        "first_failed_gate": failed_gate,
        "first_failed_reason": failed_reason,
        "gate_results": gate_results,
        "caveat": (
            "Descriptive, gate-evaluated research candidate -- NOT a causal "
            "claim, NOT a validated production finding, and NOT permission "
            "to modify V1/V2. winner_label/all_matched_categories overlap is "
            "preserved in observation.detail, never collapsed. BUILD_REQUEST "
            "means 'worthy of human review for a possible future "
            "implementation', never 'validated'."
        ),
    }


# =====================================================================
# BUILD_REQUEST candidate assembly (never implemented, never auto-approved)
# =====================================================================

def build_build_request_candidate(hypothesis_record, candidate):
    """Only meaningful when lifecycle_status == 'BUILD_REQUEST'. Assembles
    exactly the fields the build authorization requires. This function
    NEVER modifies production and NEVER implies approval -- every
    instance carries human_approval_required=True, hardcoded.
    """
    if hypothesis_record["lifecycle_status"] != "BUILD_REQUEST":
        raise ValueError("build_build_request_candidate() called on a non-BUILD_REQUEST hypothesis")

    return {
        "exact_hypothesis": hypothesis_record["hypothesis_statement"],
        "affected_component": "research_only",  # PR5e never proposes a V1/V2 change directly;
        # it proposes further validation work. A real V1/V2 change proposal is a
        # separate, later, human-authored step -- never generated by this module.
        "association_evidence": hypothesis_record["association"],
        "incremental_evidence": hypothesis_record["incremental_information"],
        "oos_effect": {
            "rmse_reduction_pct": hypothesis_record["oos_validation"].get("rmse_reduction_pct"),
            "required_effect_pct": hypothesis_record["oos_validation"].get("required_effect_pct"),
        },
        "oos_stability_evidence": hypothesis_record["oos_validation"].get("subsplit"),
        "significance_eligibility": hypothesis_record["significance_eligibility"],
        "redundancy_status": hypothesis_record["source_redundancy_note"],
        "evidence_supporting": {
            "association": hypothesis_record["association"],
            "incremental_information": hypothesis_record["incremental_information"],
            "oos_validation": hypothesis_record["oos_validation"],
            "validation_status": hypothesis_record["validation_status"],
        },
        "evidence_against": {
            "source_redundancy_note": hypothesis_record["source_redundancy_note"],
            "evidence_type": hypothesis_record["evidence_type"],
            "gate3_independent_of_gate2": hypothesis_record["incremental_information"].get("independent_of_gate2"),
        },
        "sample_size": hypothesis_record["observation"]["n"],
        "time_period": candidate.get("horizon_hours"),
        "validation_method": (
            "chronological_holdout (never shuffled) + chronological sub-split "
            "sign-stability check across the validation half"
        ),
        "baseline_comparison": hypothesis_record["incremental_information"],
        "known_confounders": [hypothesis_record["source_redundancy_note"]],
        "overlap_redundancy_considerations": candidate.get("redundancy_pairwise")
        or candidate.get("suppression") or "NOT_APPLICABLE",
        "expected_measurable_effect": hypothesis_record["association"],
        "why_it_passed_the_promotion_gate": (
            f"Gates 0-6 all passed: measurable sample, repeatable across "
            f"horizons, statistically significant association "
            f"(Gate 2 AND the stricter Gate 5 significance-eligibility "
            f"re-check), a discovery-half partial-correlation signal "
            f"(Gate 3 -- reported for completeness, NOT independent of "
            f"Gate 2), an out-of-sample RMSE reduction of "
            f"{hypothesis_record['oos_validation'].get('rmse_reduction_pct')}% "
            f"that both cleared the {MEANINGFUL_OOS_IMPROVEMENT_PCT}% meaningful-"
            f"effect floor AND replicated in the same direction across two "
            f"non-overlapping validation sub-windows (Gate 4), evidence_type "
            f"!= EXPLANATORY, and redundancy_status != REDUNDANCY_UNRESOLVED "
            f"(Gate 6)."
        ),
        "known_limitations": [
            "BUILD_REQUEST means 'candidate worthy of human review for a "
            "possible future implementation' -- it does NOT mean 'validated'.",
            "Gate 3's partial correlation is empirically correlated with "
            "Gate 2's own statistic (r=0.944 across this snapshot) and is "
            "NOT independent confirmation.",
            f"redundancy_status={hypothesis_record['source_redundancy_note']} "
            "means only that no single pairwise/vs-composite Pearson |r| "
            "crossed 0.7 in the comparisons this module checked -- it does "
            "NOT establish independence from a correlated source group, "
            "across horizons, or in the validation split.",
            "Adjacent horizons for the same source are not independent "
            "observations (overlapping forward-return windows) -- see the "
            "report's signal_family_summary for candidate count vs. "
            "distinct-source-family count.",
        ],
        "proposed_change_description": (
            "NONE -- this module proposes further human-reviewed validation, "
            "not a specific V1/V2 code change. Any such change is a separate, "
            "explicitly human-authored decision."
        ),
        "human_approval_required": True,
    }


# =====================================================================
# Orchestration
# =====================================================================

def _funnel_counts(hypotheses):
    """REPORT EVERY GATE (auditability requirement): how many candidates
    PASSED each gate, out of the total -- a true elimination funnel, not
    merely "how many were evaluated up to this point" (which would
    trivially equal the total for gate0). Uses first_failed_gate: a
    candidate passed gate N if and only if its first failure (if any)
    occurred STRICTLY AFTER gate N -- i.e. first_failed_gate is None, or
    a later gate than N."""
    total = len(hypotheses)
    counts = {}
    for i, gate_name in enumerate(_GATE_ORDER):
        counts[gate_name] = sum(
            1 for h in hypotheses
            if h["first_failed_gate"] is None or _GATE_ORDER.index(h["first_failed_gate"]) > i
        )
    counts["BUILD_REQUEST"] = sum(1 for h in hypotheses if h["lifecycle_status"] == "BUILD_REQUEST")
    return {"total_candidates": total, **counts}


def _oos_distribution(hypotheses):
    """Full distribution of validation-half RMSE-reduction point
    estimates across every SOURCE_INCREMENTAL_INFO candidate with an OK
    Level 3 result -- required so the OOS promotion criterion's effect
    is auditable, not just its pass/fail outcome."""
    values = sorted(
        h["oos_validation"]["rmse_reduction_pct"]
        for h in hypotheses
        if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO"
        and h["oos_validation"].get("rmse_reduction_pct") is not None
    )
    n = len(values)
    if n == 0:
        return {"n": 0}

    def pct(p):
        idx = min(n - 1, max(0, int(round(p * (n - 1)))))
        return values[idx]

    # Smallest positive full-validation point estimates among candidates
    # that DID reach BUILD_REQUEST under the new criterion (shows how far
    # above the noise floor the promoted candidates actually sit) --
    # separately from candidates blocked specifically at Gate 4 despite a
    # positive point estimate (NOT_REPLICATED: positive but insufficient).
    smallest_positive_among_build_requests = sorted(
        h["oos_validation"]["rmse_reduction_pct"]
        for h in hypotheses
        if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO" and h["lifecycle_status"] == "BUILD_REQUEST"
    )
    positive_but_not_replicated = sorted(
        h["oos_validation"]["rmse_reduction_pct"]
        for h in hypotheses
        if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO"
        and h["oos_validation"].get("validation_status") == "NOT_REPLICATED"
    )
    return {
        "n": n,
        "min": values[0],
        "max": values[-1],
        "median": pct(0.5),
        "q1": pct(0.25),
        "q3": pct(0.75),
        "count_le_zero": sum(1 for v in values if v <= 0),
        "count_gt_zero": sum(1 for v in values if v > 0),
        "count_ge_meaningful_threshold": sum(1 for v in values if v >= MEANINGFUL_OOS_IMPROVEMENT_PCT),
        "count_passing_stability_and_magnitude": sum(
            1 for h in hypotheses
            if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO"
            and h["oos_validation"].get("validation_status") == "PASSED_HOLDOUT"
        ),
        "smallest_positive_reductions_among_build_requests": smallest_positive_among_build_requests,
        "positive_but_not_replicated_reductions": positive_but_not_replicated,
        "meaningful_threshold_pct": MEANINGFUL_OOS_IMPROVEMENT_PCT,
    }


def _signal_family_summary(hypotheses):
    """Horizon-overlap requirement: adjacent horizons of the SAME source
    are not independent observations (overlapping forward-return
    windows). This groups by source_key (the natural, already-existing
    grouping unit -- no new clustering algorithm invented) so the report
    can show candidate count vs. distinct-source-family count
    separately, at every lifecycle stage."""
    source_hyps = [h for h in hypotheses if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO"]
    by_family = {}
    for h in source_hyps:
        source_key = h["observation"]["detail"]["source_key"]
        by_family.setdefault(source_key, []).append(h)

    build_request_families = {
        source_key: [h["subject"] for h in hyps if h["lifecycle_status"] == "BUILD_REQUEST"]
        for source_key, hyps in by_family.items()
    }
    build_request_families = {k: v for k, v in build_request_families.items() if v}

    return {
        "n_source_candidates": len(source_hyps),
        "n_distinct_source_families": len(by_family),
        "n_build_request_candidates": sum(len(v) for v in build_request_families.values()),
        "n_distinct_source_families_with_a_build_request": len(build_request_families),
        "build_request_horizons_by_family": build_request_families,
        "note": (
            "candidate_count != independent_signal_family_count: horizons "
            "for the same source share overlapping forward-return windows "
            "and are NOT independent confirmations of each other."
        ),
    }


def build_hypothesis_report(conn, start_ts, end_ts, horizons=ec.SUPPORTED_HORIZONS,
                             source_horizons=(1, 3, 6, 12, 24)):
    """The single top-level entry point. Read-only. Never writes.
    Calls PR5c/PR5d/PR5d-followup's own, unmodified report builders,
    derives candidates, evaluates every one through the gate pipeline.
    """
    source_report = sa.build_source_effectiveness_report(conn, start_ts, end_ts, horizons=source_horizons)
    ec_report = ec.build_error_classification_report(conn, start_ts, end_ts, horizons=horizons)
    overlap_report = eoa.build_overlap_report(conn, start_ts, end_ts, horizons=horizons)

    source_candidates = derive_source_candidates(source_report)
    taxonomy_candidates = derive_taxonomy_candidates(ec_report, overlap_report)

    # Gate 4's v2 sub-split stability check needs the same raw source
    # matrix / forward-return rows source_analysis.py's own report
    # already derived from -- fetched here via the SAME public functions
    # (extract_source_matrix, outcome_engine.compute_forward_returns_
    # from_history) source_analysis.py itself calls, no new query shape.
    _, source_matrix_rows, _ = sa.extract_source_matrix(conn, start_ts, end_ts)
    source_outcome_rows_by_horizon = {
        h: outcome_engine.compute_forward_returns_from_history(conn, start_ts, end_ts, h)
        for h in source_horizons
    }

    hypotheses = []
    for candidate in source_candidates:
        h = candidate["horizon_hours"]
        hypotheses.append(evaluate_candidate(
            candidate, source_matrix_rows=source_matrix_rows,
            source_outcome_rows=source_outcome_rows_by_horizon.get(h),
        ))

    rows_by_horizon = {}
    classifications_by_horizon = {}
    for horizon_hours in horizons:
        result = ec.classify_all(conn, horizon_hours, start_ts, end_ts)
        rows_by_horizon[horizon_hours] = result["rows"]
        classifications_by_horizon[horizon_hours] = result["classifications"]

    for candidate in taxonomy_candidates:
        h = candidate["horizon_hours"]
        hypotheses.append(evaluate_candidate(
            candidate, rows=rows_by_horizon.get(h), classifications=classifications_by_horizon.get(h)
        ))

    build_requests = []
    for hyp, candidate in zip(
        [h for h in hypotheses if h["candidate_type"] == "SOURCE_INCREMENTAL_INFO"] +
        [h for h in hypotheses if h["candidate_type"] == "TAXONOMY_CONCENTRATION"],
        source_candidates + taxonomy_candidates,
    ):
        if hyp["lifecycle_status"] == "BUILD_REQUEST":
            build_requests.append(build_build_request_candidate(hyp, candidate))

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "n_source_candidates": len(source_candidates),
        "n_taxonomy_candidates": len(taxonomy_candidates),
        "hypotheses": hypotheses,
        "build_requests": build_requests,
        "lifecycle_status_counts": {
            status: sum(1 for h in hypotheses if h["lifecycle_status"] == status)
            for status in _ALLOWED_ASSIGNABLE_STATUSES
        },
        "gate_funnel": _funnel_counts(hypotheses),
        "oos_distribution": _oos_distribution(hypotheses),
        "signal_family_summary": _signal_family_summary(hypotheses),
        "methodology_note": (
            "Every hypothesis here is a gate-evaluated research candidate. "
            "winner_label frequency, source correlation, and post-event "
            "evidence are never treated as sufficient evidence by themselves. "
            "No hypothesis is 'true' -- only as far through the gate pipeline "
            "as the current evidence takes it. BUILD_REQUEST means "
            "'candidate worthy of human review for a possible future "
            "implementation', never 'validated'."
        ),
    }


# =====================================================================
# Persistence into the already-existing (not production-deployed)
# research_hypotheses schema. THE ONLY FUNCTION IN THIS MODULE THAT
# WRITES ANYTHING. Tested only against an in-memory sqlite3 fixture;
# never invoked against production in this PR.
# =====================================================================

def persist_hypothesis(conn, created_ts, hypothesis_record, source_analysis_ids=None):
    """INSERTs one row into research_hypotheses (schema unchanged from
    PR5a's migration 0008 -- not deployed to production; see module
    docstring). status <- lifecycle_status. out_of_sample_status <-
    validation_status (see "Two different 'validation' concepts").
    evidence_summary_json holds the full six-part decomposition, gate
    results, evidence_type, and redundancy note -- never an opaque
    text blurb as the only evidence trail.
    """
    payload = {
        "observation": hypothesis_record["observation"],
        "association": hypothesis_record["association"],
        "incremental_information": hypothesis_record["incremental_information"],
        "evidence_status": hypothesis_record["evidence_status"],
        "evidence_type": hypothesis_record["evidence_type"],
        "source_redundancy_note": hypothesis_record["source_redundancy_note"],
        "gate_results": hypothesis_record["gate_results"],
        "caveat": hypothesis_record["caveat"],
    }
    cursor = conn.execute(
        "INSERT INTO research_hypotheses "
        "(created_ts, last_updated_ts, subject, statement, source_analysis_ids, "
        " status, evidence_summary_json, out_of_sample_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_ts, created_ts, hypothesis_record["subject"],
            hypothesis_record["hypothesis_statement"],
            json.dumps(source_analysis_ids or []),
            hypothesis_record["lifecycle_status"],
            json.dumps(payload),
            hypothesis_record["validation_status"],
        ),
    )
    return cursor.lastrowid
