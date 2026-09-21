"""
EXP-010: Source Dialogue Validation -- research-only.

Research question: "When two existing V1 sources observe information
around the same market event, do they support, contradict, or remain
insufficiently comparable to each other after applying strict temporal
as-of rules?"

This is a VALIDATION experiment for the merged Source Dialogue Engine
(research/source_dialogue.py, PR #68) -- it exists to exercise that
engine against real V1/V2 historical data BEFORE any new external
source (EIA, GDELT) is ever wired into it. It does not itself decide
whether EIA/GDELT integration should proceed; it only proves the
engine behaves correctly on data this project already has.

=====================================================================
Reused, unchanged -- nothing here duplicates existing detection,
relevance, redundancy, or classification logic
=====================================================================

- `event_source_relevance.collect_events()` -- the SAME event
  detection (all 5 PR3 detectors) and `is_internal_model_event` flag
  already used by EXP-009. Never reimplemented here.
- `event_source_relevance.snapshot_v1_source()` -- the SAME as-of V1
  source snapshot (most recent history row at or before event_ts).
- `source_analysis.discover_sources()` / `extract_source_matrix()` --
  the SAME dynamic (never hard-coded) V1 source enumeration EXP-005
  already uses. `extract_source_matrix()` is also the data-fetch this
  module's OWN `compute_asof_source_medians()` reuses (see the
  "temporal-leakage correction" section below) -- bounded per-event to
  `[start_ts, event_ts]` rather than the whole run window.

`event_source_reaction.compute_source_medians()` is deliberately NOT
reused for direction (see the temporal-leakage correction section
below) -- it remains untouched and is still the correct choice for
PR57's own post-hoc use, just not for this module's as-of need.
- `source_dialogue.classify_relationship()` / `classify_pairwise_
  redundancy()` / `canonical_pair()` -- the LOCKED, independently-
  audited engine (PR #68). This module never recomputes a
  relationship/redundancy verdict itself; it only builds the
  normalized observation dicts the engine's own documented contract
  requires, and calls it.

=====================================================================
V1 sources have NO distinct publication-lag concept (a documented
design decision, not an invented timestamp)
=====================================================================

Unlike EIA (a real, researched publication-lag) or GDELT (a `seendate`
distinct from the event), a V1 source's value is computed and written
into `history.sources_json` at the SAME moment it is collected -- there
is no known, separate "this value became publicly knowable N minutes
later" fact anywhere in this system. The one real, verifiable timestamp
that exists for a V1 source snapshot is the `history.ts` of the row it
came from (returned as `source_observation_ts` by `snapshot_v1_source`).
This module therefore sets BOTH `information_available_at` and
`observation_time` to that same real timestamp -- this is not
inventing a timestamp, it is using the only one that genuinely exists,
and it is stated here explicitly rather than left implicit.

=====================================================================
Direction: reuses PR57's own COMPARISON RULE, but NOT its whole-window
statistic (temporal-leakage correction -- see next section)
=====================================================================

`classify_direction_vs_median(value, median)`: `value > median` -> UP,
`value < median` -> DOWN, `value == median` -> None (no directional
signal) -- the EXACT comparison `event_source_reaction.classify_
alignment()` already performs (`source_implies_up = source_value >
source_median`). No new threshold, no new comparison rule -- only the
`median` fed into it is computed differently, per below.

=====================================================================
Temporal-leakage correction (post-audit): the reference median MUST be
as-of-safe, not a whole-window batch statistic
=====================================================================

An earlier version of this module fed `classify_direction_vs_median()`
the SAME whole-window median `event_source_reaction.compute_source_
medians()` computes for PR57's own post-hoc use. That function's own
docstring is explicit: "BATCH descriptive statistic over the whole
analysis window -- NEVER information available at any single
event_ts." Reusing it here was a genuine methodological bug (found by
independent audit, not by this module's own build): the direction
assigned to an event at `event_ts` could depend on source observations
that occurred AFTER `event_ts` (whichever the whole run's `end_ts`
happened to be), which directly violates this experiment's own stated
"strict temporal as-of rules" research question -- even though the
locked Source Dialogue Engine's own `information_available_at <=
information_cutoff` gate is itself correct and was never at fault; the
leak was entirely in what fed the engine's `direction` field before
the engine ever saw it.

The fix is `compute_asof_source_medians()` below -- a NEW function,
LOCAL to this module (never added to `event_source_reaction.py`, which
remains completely unchanged and is still the right choice for PR57's
own whole-window, post-hoc use). It reuses `source_analysis.
extract_source_matrix()` -- the SAME data-fetching function PR57's own
`compute_source_medians()` itself calls -- but bounds it to
`[start_ts, event_ts]` PER EVENT (inclusive of event_ts itself, since
the required semantics are `observation_time <= event_ts`) instead of
the whole run's `[start_ts, end_ts]`. No interpolation, no forward
filling, no external fixed threshold: if fewer than
`EXP010_MIN_ASOF_HISTORY_FOR_MEDIAN` distinct as-of observations exist
for a source at a given event, its median is `None`, which
`classify_direction_vs_median()` already turns into `direction=None`,
which the locked engine already turns into `INSUFFICIENT_EVIDENCE` --
no new fallback path was invented; the existing "missing input"
handling at every layer was simply given a genuinely as-of-safe input.

`EXP010_MIN_ASOF_HISTORY_FOR_MEDIAN` is PROVISIONAL / NOT EMPIRICALLY
VALIDATED. A project-wide search for an existing minimum-sample
constant that could legitimately be cited instead found none that
matches this exact question ("how many strictly-as-of prior
observations of a single source justify trusting a median as a
directional reference"): `source_analysis.MIN_SAMPLE_PER_REGIME` (10)
gates a regime-bucketed aggregate report, not a per-event per-source
statistic; `MIN_SAMPLE_FOR_LEVEL2`/`MIN_SAMPLE_FOR_CONTRADICTED` (30)
and `MIN_SAMPLE_FOR_LEVEL3` (40) gate whole-run discrimination/
robustness levels; `stats_utils.MIN_N_FOR_FISHER_Z` (6) gates a
correlation z-transform's own validity, an unrelated statistic. None
of these answer "how many points make a single as-of median
trustworthy," so a new, small, explicitly-provisional floor is used
instead: 3 (the minimum count for which a median is a genuine middle
value rather than just an average of the only two points available,
or the single point itself). This has not been empirically validated
against real EXP-010 data and must be revisited once real accumulated
runs exist.

=====================================================================
Event types: market-derived vs. internal-model (explicit, never
conflated)
=====================================================================

Only events with `is_internal_model_event == False` (V2_FAILURE_CLUSTER
is the sole internal-model category, per `event_source_relevance.py`'s
own `INTERNAL_MODEL_EVENT_CATEGORIES`) are used for the relationship
leg -- an internal-model event describes V2's own prediction behaviour,
never an independently observed real-world event, so comparing two
sources "around" one would not be validating source dialogue at all.
Internal-model events are counted and disclosed, never silently
dropped.

=====================================================================
Payload-size discipline (learned directly from EXP-009's own 1.3MB
crisis)
=====================================================================

The relationship leg is inherently O(events x source_pairs) -- with
21 V1 sources, C(21,2)=210 pairs per event. Even a modest number of
real events multiplies this into thousands of rows. This module
computes the FULL per-(event, pair) detail (so it is testable and
auditable), but the persisted report built by run_experiment.py keeps
only descriptive summary counts, not the full per-row join -- exactly
the same size-safety principle EXP-009 was forced to adopt reactively.
Applied proactively here instead.

=====================================================================
Methodological finding: DIFFERENT_TIMING is structurally near-
unreachable for pure V1-vs-V1 comparisons (disclosed, not hidden)
=====================================================================

`event_source_relevance.snapshot_v1_source()` always queries the
SINGLE most recent `history` row at-or-before `event_ts` and then
checks whether that ONE row happens to contain a given source key --
it never independently searches backward per source key. Consequence:
whenever TWO V1 sources both have a present value at the same event,
they necessarily came from the SAME row and therefore share the
IDENTICAL `observation_time` -- a timing gap between two PRESENT V1
observations cannot arise through this reused function. When only one
source is present in that row, the other is simply INSUFFICIENT_
EVIDENCE (missing), not "differently timed."

`DIFFERENT_TIMING` therefore remains a real, correctly-implemented,
and fully-tested code path in the locked Source Dialogue Engine, and
`build_v1_observation()`'s own contract makes no assumption that would
prevent it -- but under CURRENT V1 data and the currently-reused
snapshot method, this specific label is expected to be rare-to-absent
in practice for V1-vs-V1 pairs. It is expected to become genuinely
meaningful once a source with an independent timestamp axis (EIA's own
publication-lag semantics, or GDELT's `seendate`) is compared against
a V1 source -- exactly the future use case this validation experiment
exists to prepare for.
"""
import sys
import os
import statistics

sys.path.insert(0, os.path.dirname(__file__))
import event_source_relevance as esrel  # noqa: E402 -- UNCHANGED, reused as-is
import event_source_reaction as esr  # noqa: E402 -- UNCHANGED, reused as-is (window constant only, see below)
import source_analysis as sa  # noqa: E402 -- UNCHANGED, reused as-is
import source_dialogue as sd  # noqa: E402 -- UNCHANGED, LOCKED contract (PR #68)

WINDOW_LOOKBACK_MS = esr.PRE_EVENT_LOOKBACK_MS  # 24h, PR57's own reused bound
PROVIDER = "V1"

# PROVISIONAL / NOT EMPIRICALLY VALIDATED -- see module docstring's
# "Temporal-leakage correction" section for why this exists and why no
# existing project minimum-sample constant was reused instead.
EXP010_MIN_ASOF_HISTORY_FOR_MEDIAN = 3


def classify_direction_vs_median(value, median):
    """The exact comparison event_source_reaction.classify_alignment()
    already performs (`source_implies_up = source_value > source_
    median`) -- reproduced here as its own named function rather than
    inventing a new rule. None (no directional signal) when value ==
    median, or either input is missing -- never guessed."""
    if value is None or median is None:
        return None
    if value > median:
        return "UP"
    if value < median:
        return "DOWN"
    return None


def compute_asof_source_medians(conn, start_ts, event_ts, source_keys,
                                 min_history=EXP010_MIN_ASOF_HISTORY_FOR_MEDIAN):
    """AS-OF-SAFE replacement for feeding classify_direction_vs_median()
    -- see module docstring's "Temporal-leakage correction" section for
    why this exists as a NEW, LOCAL function rather than a modification
    of event_source_reaction.compute_source_medians() (which remains
    completely unchanged).

    Reuses source_analysis.extract_source_matrix() -- the SAME
    data-fetching function PR57's own compute_source_medians() itself
    calls -- but bounded to [start_ts, event_ts] (inclusive of
    event_ts: the required semantics are observation_time <= event_ts,
    never <) instead of the whole run's [start_ts, end_ts]. No future
    observation (ts > event_ts) can ever enter this computation.

    Returns {source_key: median_or_None}. A source's median is None
    (never interpolated, never forward-filled, never a fixed external
    threshold) whenever fewer than `min_history` distinct as-of
    observations of that source exist at or before event_ts -- this
    degrades downstream to direction=None -> INSUFFICIENT_EVIDENCE via
    the SAME existing missing-input handling every other layer already
    has, not a new fallback path.

    Guards against event_ts <= start_ts (e.g. an event at the very
    start of the analysis window) without calling extract_source_
    matrix() at all -- that function's own _validate_bounds() requires
    a strictly positive window and would otherwise raise for a
    legitimate edge case this function must instead resolve to
    INSUFFICIENT_EVIDENCE."""
    if event_ts is None or start_ts is None or event_ts <= start_ts:
        return {key: None for key in source_keys}

    _, matrix_rows, _ = sa.extract_source_matrix(conn, start_ts, event_ts)
    medians = {}
    for key in source_keys:
        values = [row["sources"].get(key) for row in matrix_rows if row["sources"].get(key) is not None]
        medians[key] = statistics.median(values) if len(values) >= min_history else None
    return medians


def build_v1_observation(source_key, value, observation_ts, median):
    """One Source-Dialogue-Engine-shaped observation for a V1 source at
    a given as-of snapshot. information_available_at == observation_time
    == observation_ts -- see module docstring's "no distinct
    publication-lag concept" section. Returns None if the snapshot
    itself has no usable value (never fabricates one)."""
    if value is None or observation_ts is None:
        return None
    return {
        "source_key": source_key,
        "provider": PROVIDER,
        "source_category": "V1_SENTIMENT_SOURCE",
        "information_available_at": observation_ts,
        "observation_time": observation_ts,
        "direction": classify_direction_vs_median(value, median),
        "raw_value": value,
        "reference_value": {"source_median": median, "rule": "value>median=>UP, value<median=>DOWN (comparison reused from event_source_reaction.classify_alignment()); median itself is this module's own AS-OF-SAFE compute_asof_source_medians(), never the whole-window statistic"},
        "evidence_reference": f"history snapshot as of ts={observation_ts}",
    }


def build_relationship_dataset(conn, start_ts, end_ts):
    """For every REAL (non-internal-model) event and every canonical
    V1 source pair, builds one Source Dialogue Engine interaction via
    classify_relationship() -- the LOCKED engine, never reimplemented.
    window_start/window_end = [event_ts - WINDOW_LOOKBACK_MS, event_ts]
    (snapshot_v1_source's own SQL already guarantees observation_time
    <= event_ts, so window_end=event_ts is a structural upper bound;
    window_start is what actually rejects a stale/missing snapshot).
    information_cutoff = event_ts -- this is an as-of historical
    validation ("was this comparable AS OF the event"), never a
    live/current-time cutoff.

    Returns {"window", "events", "source_keys", "results"} where every
    result also carries is_internal_model_event=False (all persisted
    rows are real-world by construction) and the two raw observations
    for full auditability."""
    events = esrel.collect_events(conn, start_ts, end_ts)
    real_events = [e for e in events if not e["is_internal_model_event"]]
    source_keys = sa.discover_sources(conn, start_ts, end_ts)

    results = []
    for event in real_events:
        event_ts = event["event_ts"]
        window_start = event_ts - WINDOW_LOOKBACK_MS
        window_end = event_ts

        # AS-OF-SAFE per event -- see compute_asof_source_medians()'s own
        # docstring and the module docstring's "Temporal-leakage
        # correction" section. Deliberately recomputed per event (never
        # hoisted out of this loop): a whole-run-hoisted median is
        # exactly the bug this replaces.
        asof_medians = compute_asof_source_medians(conn, start_ts, event_ts, source_keys)

        snapshots = {}
        for key in source_keys:
            snap = esrel.snapshot_v1_source(conn, event_ts, key)
            obs = build_v1_observation(key, snap["source_value"], snap["source_observation_ts"], asof_medians.get(key))
            snapshots[key] = obs

        for i, key_a in enumerate(source_keys):
            for key_b in source_keys[i + 1:]:
                interaction = sd.build_interaction(
                    key_a, key_b, snapshots[key_a], snapshots[key_b],
                    information_cutoff=event_ts,
                    window_start=window_start, window_end=window_end,
                    event_id=event["event_id"],
                )
                interaction["event_ts"] = event_ts
                interaction["event_category"] = event["category"]
                interaction["is_internal_model_event"] = False
                results.append(interaction)

    return {
        "window": {"start_ts": start_ts, "end_ts": end_ts},
        "events": real_events,
        "n_events_total": len(events),
        "n_internal_model_events_excluded": len(events) - len(real_events),
        "source_keys": source_keys,
        "results": results,
    }


def build_redundancy_dataset(conn, start_ts, end_ts):
    """The whole-window pairwise redundancy leg -- computed ONCE per
    run (never multiplied by events), reusing source_analysis.
    pairwise_source_redundancy() via source_dialogue.classify_
    pairwise_redundancy() exactly as the locked engine requires.
    Builds per-source observation SERIES from extract_source_matrix()'s
    own rows (one entry per history.ts where that source has a value)
    -- never re-derives the matrix itself."""
    source_keys, matrix_rows, _non_numeric_counts = sa.extract_source_matrix(conn, start_ts, end_ts)
    series_by_key = {
        key: [
            {"observation_time": row["ts"], "raw_value": row["sources"][key]}
            for row in matrix_rows if row["sources"].get(key) is not None
        ]
        for key in source_keys
    }

    results = []
    for i, key_a in enumerate(source_keys):
        for key_b in source_keys[i + 1:]:
            label, detail = sd.classify_pairwise_redundancy(series_by_key[key_a], series_by_key[key_b], key_a, key_b)
            key_a_c, key_b_c = sd.canonical_pair(key_a, key_b)
            results.append({
                "source_key_a": key_a_c, "source_key_b": key_b_c,
                "redundancy": label, "n": detail["n"], "r": detail["r"],
            })
    return {"window": {"start_ts": start_ts, "end_ts": end_ts}, "source_keys": source_keys, "results": results}


# =====================================================================
# Descriptive summaries only -- counts, never a ranking/score/weight
# =====================================================================

def summarize_relationship_counts(dataset):
    counts = {label: 0 for label in sd.RELATIONSHIP_LABELS}
    for r in dataset["results"]:
        counts[r["relationship"]] += 1
    return {
        "n_events_used": len(dataset["events"]),
        "n_internal_model_events_excluded": dataset["n_internal_model_events_excluded"],
        "n_source_pair_observations": len(dataset["results"]),
        "by_relationship": counts,
    }


def summarize_redundancy_counts(dataset):
    counts = {label: 0 for label in sd.REDUNDANCY_LABELS}
    for r in dataset["results"]:
        counts[r["redundancy"]] += 1
    return {"n_source_pairs": len(dataset["results"]), "by_redundancy": counts}
