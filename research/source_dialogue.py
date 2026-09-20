"""
Source Dialogue Engine -- research-only, provider-agnostic comparison
of two independently-normalized source observations.

=====================================================================
Objective
=====================================================================

Given two source observations (from any provider -- an existing V1
source, EIA once temporally verified, GDELT once temporally verified,
or any future research source), produce a purely DESCRIPTIVE,
deterministic relationship. This module never ranks sources, never
computes a numeric agreement/confidence score, and never recommends a
weight, coefficient, or production change. It is pure computation: no
network, no database, no environment dependency, fully testable
offline.

=====================================================================
Two orthogonal questions, kept as two separate fields (never collapsed)
=====================================================================

1. TIMING/DIRECTION relationship, for a single pair of observations at
   one comparison window: INSUFFICIENT_EVIDENCE / DIFFERENT_TIMING /
   SUPPORTING / CONTRADICTING (`classify_relationship`).
2. PAIRWISE REDUNDANCY, a whole-series statistical question answered
   the exact same way research/source_analysis.py's own EXP-005
   machinery already answers it: REDUNDANCY_UNRESOLVED /
   NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED / INSUFFICIENT_EVIDENCE
   (`classify_pairwise_redundancy`).

These are NOT alternatives to pick between for one "the" label -- they
answer genuinely different questions from genuinely different inputs
(one pair of points vs. an entire aligned time series), exactly as
distinct as PR3's event-relative classification and PR5c's
prediction-time eligibility are kept distinct (`evidence_temporal.py`'s
own docstring: "the two checks answer different questions ... stay
separate"). `build_interaction()` reports both as separate fields on
the same interaction record, never forcing one into the other.

=====================================================================
Reused, unchanged (per explicit instruction -- nothing here duplicates
existing redundancy, temporal, or relevance logic)
=====================================================================

- `source_analysis.pairwise_source_redundancy()` and
  `STRONG_REDUNDANCY_THRESHOLD = 0.7` -- the ONLY Pearson-correlation
  computation and the ONLY redundancy threshold in this project. This
  module supplies the correctly-aligned observation matrix that
  function requires; it never recomputes a correlation itself.
- Timing tolerance: audit finding F1 (independent audit of commit
  01cbc70) established that `evidence_collector.SAME_WINDOW_TOLERANCE_MS`
  answers a DIFFERENT question -- how close an evidence article's
  publication time is to the ONE EVENT it is about -- from what this
  engine needs: how close two SOURCES' OWN `information_available_at`
  values are to EACH OTHER, with no event involved at all. Reusing the
  same numeric value for a different research question was accepted
  too readily in the first build and is corrected here: this module
  now defines its own, distinctly-named `SOURCE_DIALOGUE_TIMING_
  TOLERANCE_MS`, explicitly marked PROVISIONAL / NOT EMPIRICALLY
  VALIDATED (see that constant's own docstring). A project-wide search
  for an existing constant that already matches this exact semantic
  (source-to-source information-availability gap) found none --
  `EVIDENCE_MATCH_TOLERANCE_MS` (error_classification.py) is a fuzzy
  event-ID matching tolerance, not a source-timing one; every other
  `*_MS` constant in research/ bounds an analysis WINDOW, not a
  pairwise gap. `event_source_relevance.py`'s own PRE_EVENT/SAME_
  WINDOW/POST_EVENT vocabulary is still deliberately not reused here
  either, for the same reason as before -- it is a one-source-vs-one-
  EVENT classification, while this engine compares two SOURCES with no
  event required.
- `REDUNDANCY_UNRESOLVED` / `NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED` --
  the exact terminology `hypothesis_gate.py`'s own v2 correction
  already established (see that module's "Source redundancy caveat"
  section and `research/README.md`'s own copy of it). The two label
  STRINGS are reproduced here (not imported from `hypothesis_gate.py`,
  which expects a different, gate-pipeline-specific candidate dict
  shape this module has no reason to depend on -- the same deliberate-
  decoupling precedent `event_source_relevance.py` already set for
  reproducing PR3 detection independently of
  `event_source_reaction.py` "so that dependency cannot even
  accidentally leak in"). Their MEANING is enforced by literally
  calling `pairwise_source_redundancy`/`STRONG_REDUNDANCY_THRESHOLD`,
  never a re-derived threshold.

=====================================================================
Why |r| < 0.7 is NEVER "complementary" (deliberate methodological
correction, per explicit instruction)
=====================================================================

`NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED` states only that no single
pairwise Pearson |r| crossed 0.7 -- it is NOT a claim of independence,
and it is certainly not a claim that the two sources are usefully
"complementary." Complementarity is a claim about INCREMENTAL VALUE
(does source B improve a forecast beyond what source A alone gives?),
which is an out-of-sample, predictive question this module never asks
or answers -- it only ever asks whether two series moved together
historically. A future, separately-authorized weighting/OOS research
experiment may establish incremental value; this module supplies
descriptive evidence for that experiment, never the conclusion itself.
`research/README.md`'s own concrete counter-example is the canonical
illustration: `global` and `gold` correlate at r=-0.624 in production
(well below 0.7) yet that alone says nothing about whether either
carries information the other lacks.

=====================================================================
Golden rules (same discipline as every other research/ module)
=====================================================================

- $0, pure computation, no network/database/environment dependency.
- No `source_score`/`dialogue_score`/`consensus_score`/`confidence_
  score`/`weight_score`/ranking/`winner`/`loser` anywhere.
- No `KEEP`/`INCREASE`/`DECREASE`/`REMOVE`/`BUILD_REQUEST` anywhere --
  no weighting or coefficient recommendation of any kind.
- Never writes to history/btc_data/predictions/selection_decisions or
  any V1/V2 table -- this module issues zero SQL of any kind.
- Never creates a production V1 source key.
- Future observations (`information_available_at > information_cutoff`)
  are always rejected outright, never partially used.
- A missing `information_available_at` is never guessed at via
  `collection_ts`, `observation_time`, or any other substitute --
  always `INSUFFICIENT_EVIDENCE`.
- A malformed (non-numeric, or `bool`) `information_available_at`/
  `observation_time` degrades to `INSUFFICIENT_EVIDENCE`, never an
  uncaught exception (audit finding F3) -- a single malformed
  observation from a real future provider must never be able to crash
  an entire batch comparison run.
- When a comparison window (`window_start`/`window_end`) is supplied,
  BOTH observations' own `observation_time` (never
  `information_available_at`, a different axis entirely -- see
  `classify_relationship`'s own docstring) must fall inside it,
  inclusive on both ends, or the result is `INSUFFICIENT_EVIDENCE`
  (audit finding F2 -- previously accepted but never enforced).
- A duplicate `observation_time` within one series is rejected
  (raises) rather than silently resolved by picking one value over the
  other -- audit finding F4: silently overwriting would change the
  statistical population fed to `pairwise_source_redundancy()` without
  any visible trace.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
import source_analysis as sa  # noqa: E402 -- UNCHANGED, reused for redundancy only

RELATIONSHIP_LABELS = (
    "INSUFFICIENT_EVIDENCE",
    "DIFFERENT_TIMING",
    "SUPPORTING",
    "CONTRADICTING",
)

REDUNDANCY_LABELS = (
    "REDUNDANCY_UNRESOLVED",
    "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED",
    "INSUFFICIENT_EVIDENCE",
)

_VALID_DIRECTIONS = ("UP", "DOWN")
_SUPPORTING_PAIRS = {("UP", "UP"), ("DOWN", "DOWN")}
_CONTRADICTING_PAIRS = {("UP", "DOWN"), ("DOWN", "UP")}

# Audit finding F1: NOT the same concept as evidence_collector.SAME_
# WINDOW_TOLERANCE_MS (an event-anchored evidence-publication
# tolerance -- see module docstring). This is a distinct, source-to-
# source information-availability tolerance with its own name.
#
# PROVISIONAL / NOT EMPIRICALLY VALIDATED: a project-wide search for an
# existing constant matching THIS exact semantic (how far apart two
# independent sources' own publication times can be while still
# counting as "the same information environment" for a directional
# comparison) found none. The 1-hour value below is carried over from
# SAME_WINDOW_TOLERANCE_MS only as a starting placeholder -- it is NOT
# a discovered or validated figure for this new question, and must not
# be presented as one. Revisit with real cross-source timing data
# (e.g. once EIA/GDELT pass primary verification and real observations
# exist) before this tolerance is treated as settled.
SOURCE_DIALOGUE_TIMING_TOLERANCE_MS = 1 * 3600000  # PROVISIONAL, see above


def _is_valid_timestamp(value):
    """True only for a real numeric epoch-ms value. `bool` is
    deliberately excluded despite being an `int` subclass in Python
    (`isinstance(True, int) is True`) -- a boolean can never be a
    legitimate timestamp, and silently accepting `True`/`False` as 1/0
    would be exactly the kind of silent coercion this module must not
    do (audit finding F3). Negative numeric values are NOT rejected --
    nothing in this module's contract restricts timestamps to
    non-negative epoch values, and rejecting them would be inventing a
    new constraint this engine has no basis to assert."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# =====================================================================
# Temporal eligibility -- the one gate everything else depends on
# =====================================================================

def is_eligible(information_available_at, information_cutoff):
    """An observation is eligible only if information_available_at is
    present, a valid numeric timestamp, AND <= information_cutoff
    (inclusive -- matches this engine's own explicit "as-of" contract;
    deliberately NOT the same as evidence_temporal.is_predictive_
    eligible()'s strict `<`, which answers a different question --
    prediction-time eligibility, not a general as-of cutoff -- so it is
    not reused here). Never guesses from collection_ts, observation_
    time, or any other field when information_available_at itself is
    missing. A malformed (non-numeric, or bool) value is treated as
    ineligible rather than raising (audit finding F3)."""
    if information_available_at is None or information_cutoff is None:
        return False
    if not _is_valid_timestamp(information_available_at) or not _is_valid_timestamp(information_cutoff):
        return False
    return information_available_at <= information_cutoff


def _within_window(observation_time, window_start, window_end):
    """True if observation_time falls inside [window_start, window_end]
    inclusive -- the audit's own required semantics (F2): window
    membership is tested against observation_time (the period an
    observation DESCRIBES), never information_available_at (when it
    became knowable) -- the two are independent axes, exactly as
    build_redundancy_matrix already treats them for the redundancy
    leg. A malformed/missing observation_time, or a malformed/missing
    window bound, degrades to "not within" (fails closed) rather than
    raising."""
    if window_start is None or window_end is None:
        return True  # no window supplied -- nothing to enforce
    if not _is_valid_timestamp(observation_time) or not _is_valid_timestamp(window_start) or not _is_valid_timestamp(window_end):
        return False
    return window_start <= observation_time <= window_end


# =====================================================================
# 1. Timing/direction relationship -- single pair, single window
# =====================================================================

def classify_relationship(
    obs_a, obs_b, information_cutoff,
    timing_tolerance_ms=SOURCE_DIALOGUE_TIMING_TOLERANCE_MS,
    window_start=None, window_end=None,
):
    """Returns one of RELATIONSHIP_LABELS. Order of checks (audit
    finding F2 added the window-membership step; everything else
    unchanged from the first build):
      1. missing observation -> INSUFFICIENT_EVIDENCE
      2. temporal eligibility (information_available_at <= cutoff,
         valid numeric type) -> INSUFFICIENT_EVIDENCE if either fails
      3. window membership (observation_time inside [window_start,
         window_end], ONLY when a window is actually supplied) ->
         INSUFFICIENT_EVIDENCE if either observation falls outside
      4. timing tolerance (information_available_at gap) ->
         DIFFERENT_TIMING if it exceeds timing_tolerance_ms
      5. direction -> SUPPORTING/CONTRADICTING, else INSUFFICIENT_EVIDENCE

    window membership and temporal eligibility are deliberately
    independent checks over two DIFFERENT fields (observation_time vs.
    information_available_at) -- an observation can fail one without
    the other, and either failure alone is enough for INSUFFICIENT_
    EVIDENCE (see test_f2_* fixtures 7/8 for the two mixed cases this
    guards against)."""
    if obs_a is None or obs_b is None:
        return "INSUFFICIENT_EVIDENCE"

    avail_a = obs_a.get("information_available_at")
    avail_b = obs_b.get("information_available_at")
    if not is_eligible(avail_a, information_cutoff) or not is_eligible(avail_b, information_cutoff):
        return "INSUFFICIENT_EVIDENCE"

    obs_time_a = obs_a.get("observation_time")
    obs_time_b = obs_b.get("observation_time")
    if not _within_window(obs_time_a, window_start, window_end) or not _within_window(obs_time_b, window_start, window_end):
        return "INSUFFICIENT_EVIDENCE"

    # Same boundary convention as evidence_collector.classify_relation:
    # strictly greater than tolerance is the only case that counts as
    # "different" -- exactly at the tolerance is still within it.
    if abs(avail_a - avail_b) > timing_tolerance_ms:
        return "DIFFERENT_TIMING"

    direction_a = obs_a.get("direction")
    direction_b = obs_b.get("direction")
    if direction_a not in _VALID_DIRECTIONS or direction_b not in _VALID_DIRECTIONS:
        return "INSUFFICIENT_EVIDENCE"

    pair = (direction_a, direction_b)
    if pair in _SUPPORTING_PAIRS:
        return "SUPPORTING"
    if pair in _CONTRADICTING_PAIRS:
        return "CONTRADICTING"
    return "INSUFFICIENT_EVIDENCE"  # unreachable given _VALID_DIRECTIONS has only 2 members; fails closed


# =====================================================================
# 2. Pairwise redundancy -- whole-series, reuses source_analysis.py
# unchanged. This module never recomputes Pearson r and never redefines
# STRONG_REDUNDANCY_THRESHOLD.
# =====================================================================

def _index_by_observation_time(series, series_label):
    """Builds {observation_time: raw_value}, RAISING on a duplicate
    observation_time within this single series (audit finding F4)
    rather than silently keeping whichever happened to be last. A
    series is expected to hold one observation per observation_time;
    two conflicting values for the same period is malformed input this
    module has no safe way to resolve on its own -- averaging/
    interpolation would be inventing a new statistical operation this
    module has no authorization to perform, so it fails closed
    instead."""
    index = {}
    for obs in series:
        t = obs.get("observation_time")
        if t is None:
            continue
        if t in index:
            raise ValueError(
                f"{series_label}: duplicate observation_time {t} -- "
                f"a series must have at most one observation per observation_time."
            )
        index[t] = obs["raw_value"]
    return index


def build_redundancy_matrix(series_a, series_b, source_key_a, source_key_b):
    """Builds the exact `rows`/`source_keys` shape
    source_analysis.pairwise_source_redundancy() requires, from two
    independent observation lists aligned by their own
    `observation_time` (the point each observation DESCRIBES -- never
    information_available_at, which is a publication-timing concept,
    not a comparison-window join key). Pairwise deletion (rows with
    only one side present) is left entirely to pairwise_source_
    redundancy() itself -- this function only aligns, never filters.
    Raises on a duplicate observation_time within either series --
    see _index_by_observation_time()."""
    by_time_a = _index_by_observation_time(series_a, "series_a")
    by_time_b = _index_by_observation_time(series_b, "series_b")
    all_times = sorted(set(by_time_a) | set(by_time_b))
    rows = [
        {"sources": {source_key_a: by_time_a.get(t), source_key_b: by_time_b.get(t)}}
        for t in all_times
    ]
    return rows, sorted([source_key_a, source_key_b])


def classify_pairwise_redundancy(series_a, series_b, source_key_a, source_key_b):
    """Returns (label, detail) where label is one of REDUNDANCY_LABELS
    and detail is source_analysis.pairwise_source_redundancy()'s own
    per-pair result dict ({"n", "r", "strong_redundancy"}), passed
    through unchanged for full auditability.

    r is None whenever pairwise_source_redundancy()'s own underlying
    pearson_correlation() cannot compute a value -- fewer than 2 paired
    observations, OR either series has zero variance (a constant
    series). Both cases are reported here as INSUFFICIENT_EVIDENCE,
    never as NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED -- an undefined
    correlation is not evidence of "no strong redundancy detected", it
    is simply not evidence of anything (source_analysis.pearson_
    correlation's own docstring: "never as 0.0, since 0.0 would
    misleadingly imply 'no relationship' rather than 'undefined'" --
    this module extends that same discipline to its own label choice)."""
    rows, keys = build_redundancy_matrix(series_a, series_b, source_key_a, source_key_b)
    result = sa.pairwise_source_redundancy(rows, keys)
    pair_key = tuple(sorted([source_key_a, source_key_b]))
    detail = result[pair_key]
    if detail["r"] is None:
        return "INSUFFICIENT_EVIDENCE", detail
    if detail["strong_redundancy"]:
        return "REDUNDANCY_UNRESOLVED", detail
    return "NO_STRONG_PAIRWISE_REDUNDANCY_DETECTED", detail


# =====================================================================
# 3. Pairing -- canonical, order-independent
# =====================================================================

def canonical_pair(source_key_a, source_key_b):
    """A/B and B/A must always produce the same canonical pair --
    min() first, max() second (lexical, per the design instructions).
    Raises if the two keys are identical (a source cannot dialogue with
    itself)."""
    if source_key_a == source_key_b:
        raise ValueError(f"source_key_a and source_key_b must differ, both were {source_key_a!r}")
    return (min(source_key_a, source_key_b), max(source_key_a, source_key_b))


# =====================================================================
# 4. Interaction record -- combines both questions, supports both
# event-anchored and rolling-window modes. Event association is a pure
# passthrough field -- this module never queries or recomputes
# research_events in any way.
# =====================================================================

def build_interaction(
    source_key_a, source_key_b, obs_a, obs_b, information_cutoff,
    window_start, window_end, event_id=None,
    series_a=None, series_b=None, timing_tolerance_ms=SOURCE_DIALOGUE_TIMING_TOLERANCE_MS,
):
    """Returns one interaction record:
      {source_key_a, source_key_b} (canonical order),
      event_id (None for a rolling-window comparison),
      window_start, window_end,
      relationship (RELATIONSHIP_LABELS, from classify_relationship --
        now ACTUALLY enforcing window_start/window_end against each
        observation's own observation_time, audit finding F2; the
        first build stored these two fields but never consulted them),
      redundancy (REDUNDANCY_LABELS, or None if series_a/series_b were
        not supplied -- redundancy requires a historical series, not
        just the single pair being compared this window),
      redundancy_detail (the reused function's own {"n","r",
        "strong_redundancy"}, or None).

    `source_key_a`/`source_key_b` in the OUTPUT are always the
    canonical (min, max) pair, regardless of the order the caller
    passed them in -- obs_a/obs_b are swapped alongside if needed, so
    the relationship/redundancy computation itself is never affected by
    call-site ordering."""
    key_a, key_b = canonical_pair(source_key_a, source_key_b)
    if key_a != source_key_a:
        obs_a, obs_b = obs_b, obs_a
        series_a, series_b = series_b, series_a

    relationship = classify_relationship(
        obs_a, obs_b, information_cutoff, timing_tolerance_ms,
        window_start=window_start, window_end=window_end,
    )

    redundancy = None
    redundancy_detail = None
    if series_a is not None and series_b is not None:
        redundancy, redundancy_detail = classify_pairwise_redundancy(series_a, series_b, key_a, key_b)

    return {
        "source_key_a": key_a,
        "source_key_b": key_b,
        "event_id": event_id,
        "window_start": window_start,
        "window_end": window_end,
        "relationship": relationship,
        "redundancy": redundancy,
        "redundancy_detail": redundancy_detail,
    }
