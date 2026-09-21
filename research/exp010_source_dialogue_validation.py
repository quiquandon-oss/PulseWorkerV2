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
- `event_source_reaction.compute_source_medians()` -- the SAME batch,
  whole-window median PR57 already uses to self-normalize a source's
  direction. Explicitly disclosed there as "NEVER information
  available at any single event_ts" -- it is a post-hoc NORMALIZATION
  reference, not a predictive input, exactly as PR57 already
  established.
- `source_analysis.discover_sources()` / `extract_source_matrix()` --
  the SAME dynamic (never hard-coded) V1 source enumeration EXP-005
  already uses.
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
Direction: reuses PR57's own self-normalizing convention, not a new one
=====================================================================

`classify_direction_vs_median(value, median)`: `value > median` -> UP,
`value < median` -> DOWN, `value == median` -> None (no directional
signal) -- the EXACT comparison `event_source_reaction.classify_
alignment()` already performs (`source_implies_up = source_value >
source_median`). No new threshold, no new statistic.

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

sys.path.insert(0, os.path.dirname(__file__))
import event_source_relevance as esrel  # noqa: E402 -- UNCHANGED, reused as-is
import event_source_reaction as esr  # noqa: E402 -- UNCHANGED, reused as-is
import source_analysis as sa  # noqa: E402 -- UNCHANGED, reused as-is
import source_dialogue as sd  # noqa: E402 -- UNCHANGED, LOCKED contract (PR #68)

WINDOW_LOOKBACK_MS = esr.PRE_EVENT_LOOKBACK_MS  # 24h, PR57's own reused bound
PROVIDER = "V1"


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
        "reference_value": {"source_median": median, "rule": "value>median=>UP, value<median=>DOWN, per event_source_reaction.classify_alignment()"},
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
    medians = esr.compute_source_medians(conn, start_ts, end_ts, source_keys)

    results = []
    for event in real_events:
        event_ts = event["event_ts"]
        window_start = event_ts - WINDOW_LOOKBACK_MS
        window_end = event_ts

        snapshots = {}
        for key in source_keys:
            snap = esrel.snapshot_v1_source(conn, event_ts, key)
            obs = build_v1_observation(key, snap["source_value"], snap["source_observation_ts"], medians.get(key))
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
