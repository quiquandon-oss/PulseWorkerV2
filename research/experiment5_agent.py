"""
Experiment 5: the minimum deterministic agentic loop.

OBSERVE -> COMPARE -> DETECT CHANGE -> CLASSIFY -> INVESTIGATE (reusing
already-collected PR4/PR6 evidence, never a fresh network call) ->
UPDATE STATE -> CREATE PROPOSAL -> WAIT FOR OUTCOME -> EVALUATE -> LEARN.

No AI call anywhere in this module -- every step is a pure/deterministic
function over already-persisted data (the archive, btc_data via
outcome_engine). Zero-cost per the build brief's hard constraint.

V1 CONTROL: this module never reads or writes the frontend's composite-
weights default list, the production selection-variant registry, or any
production weight or table. It is a pure
research reader/writer against research_sentiment_archive (own table)
and research_hypotheses (an existing, already-migration-defined table
no production code path writes to or reads from).

Why research_hypotheses, not a new table (explicit design decision)
---------------------------------------------------------------------
hypothesis_gate.persist_hypothesis() writes a PR5e-specific payload
shape (six-part statistical decomposition, gate_results, evidence_type)
that does not fit a trend/reversal/confirmation decision -- forcing
Experiment 5's decisions into that exact shape would fabricate fields
that don't apply (there is no "gate_results" for "three sources
confirmed a bullish move together"). This module therefore writes ITS
OWN, differently-shaped evidence_summary_json into the SAME table,
reusing the table and the status-lifecycle VOCABULARY
(OBSERVATION -> MONITOR -> ...) rather than hypothesis_gate's function
body, which is the wrong shape for this content. This is the reuse the
build brief asks for ("use the existing hypothesis-gate lifecycle where
appropriate instead of inventing another parallel lifecycle") applied
at the schema/vocabulary level, not the code level.

Why this MVP caps its own decisions at OBSERVATION/MONITOR
-------------------------------------------------------------
Reaching RESEARCH_HYPOTHESIS/VALIDATION_READY/BUILD_REQUEST in
hypothesis_gate.py's own lifecycle requires passing real statistical
gates (association, incremental value, out-of-sample validation) built
from an accumulated sample this brand-new agent does not have yet on
day one. Experiment 5's MVP therefore structurally refuses (raises) any
attempt to persist a decision above MONITOR -- see
EXPERIMENT5_ALLOWED_STATUSES -- so it can never even accidentally look
like a validated, human-approved finding before it has earned one. A
human deciding to extend this once outcome data accumulates is future
work, not built here, per PR5e's own "no experiment automatically
authorizes a production change" governance rule.

Append-only spirit, applied to a mutable table
-------------------------------------------------
research_sentiment_archive (this module's OWN table, via
sentiment_archive.py) is append-only in the strict sense: no UPDATE or
DELETE statement is ever issued against it. research_hypotheses is NOT
schema-enforced append-only (PR5e already updates it elsewhere), so
this module adopts the same discipline PR5e itself uses: a decision's
own "decision" payload key, once written, is never touched again;
evaluate_pending_decisions() only ever ADDS a sibling "outcome" key and
updates out_of_sample_status/last_updated_ts -- it never rewrites
"decision" with the benefit of hindsight, which is the literal
no-lookahead requirement applied to this row's own history.
"""
import json

from outcome_engine import OUTCOME_HORIZON_MS, compute_forward_returns_from_history
from sentiment_archive import get_archive_range
from source_dynamics import (
    classify_acceleration,
    classify_sequence,
    cross_source_confirmation,
    detect_reversal,
)
from source_intelligence import KNOWN_V1_SOURCE_IDS

# Deliberately narrower than hypothesis_gate.py's full ten-stage
# lifecycle -- see module docstring "Why this MVP caps its own
# decisions at OBSERVATION/MONITOR".
EXPERIMENT5_ALLOWED_STATUSES = ("OBSERVATION", "MONITOR")

DEFAULT_OBSERVE_WINDOW_MS = 14 * 24 * 3600000
# 14 days -- comfortably enough to establish a TREND_MIN_RUN=3 run at
# the archive's real, irregular multi-hour cadence, while staying well
# under outcome_engine.MAX_WINDOW_MS (90 days) and small enough that a
# single cycle's D1 read is always a bounded, indexed query
# (research_sentiment_archive.observation_ts is UNIQUE-indexed).

MAX_DECISIONS_PER_CYCLE = 20
# Runaway guard on D1 writes per cycle -- mirrors
# evidence_collector.MAX_ARTICLES_STORED_PER_EVENT's own
# bounded-output discipline. A cycle that would otherwise produce more
# than this many decisions still returns status "OK" with whatever it
# produced up to the cap, never raises.

EXPERIMENT5_TARGET_HORIZON_HOURS = 24
# The single evaluation horizon every Experiment 5 decision is created
# against today (replaces what used to be a bare "24" repeated at both
# the decision-creation call site and evaluate_pending_decisions' own
# default parameter). Persisted onto each decision's own record at
# creation time (see _build_decision_record) rather than only supplied
# later as an evaluation-time parameter, so the intended horizon is a
# traceable, immutable fact of the decision itself -- never something
# that could silently drift between the run that created a decision and
# the run that later evaluates it.

EXPERIMENT5_HORIZON_TOLERANCE_MS = 6 * 3600000
# How far the price outcome_engine actually matches may fall SHORT of a
# decision's own declared target (anchor_ts + target_horizon_hours) and
# still count as a valid resolution of that horizon -- not "any price
# after eligible_ts" (the bug a post-build audit asked this module to
# rule out). outcome_engine.compute_forward_returns_from_history's own
# resolution rule picks the NEAREST available btc_data row at or before
# the target, which is correct and UNCHANGED here (this constant, and
# the check built from it below, live entirely in THIS module -- see
# evaluate_pending_decisions) -- but "nearest at or before" can still be
# far short of the target if btc_data has a genuine collection gap
# spanning it. A gap wider than this tolerance means the matched price
# is not a reasonable stand-in for "the price at ~24h" and this module
# refuses to call the decision resolved off it, leaving it pending
# instead (never fabricated, never forced) until either a closer price
# arrives on some later run or the gap simply never closes -- the same
# "no silent interpolation" discipline outcome_engine.py's own docstring
# already states for UNRESOLVED_NO_FUTURE_PRICE_POINT, applied here to a
# price that technically resolves but is too stale to trust.
#
# 6h matches source_dynamics.CONFIRMATION_WINDOW_MS's own precedent
# exactly (chosen there for the identical reason: btc_data's real
# cadence is irregular, not tuned to any statistical target). This does
# NOT change outcome_engine.py, so source_analysis.py/hypothesis_gate.py/
# source_family_discrimination.py/fng_24h_robustness.py -- all of which
# call that shared engine directly, over fully-elapsed historical
# windows where a gap this wide is rare and is absorbed into their own
# much larger aggregate samples rather than gating a single live
# decision -- are structurally unaffected by this constant, which
# Experiment 5's own evaluate_pending_decisions is the only reader of.


def observe(conn, as_of_ts, window_ms=DEFAULT_OBSERVE_WINDOW_MS):
    """The literal no-lookahead enforcement point: reads archive rows
    with observation_ts in [as_of_ts - window_ms, as_of_ts]. This query
    is structurally incapable of returning a row from the future
    relative to as_of_ts -- there is no separate runtime check needed
    because the upper bound is the query itself."""
    return get_archive_range(conn, as_of_ts - window_ms, as_of_ts)


def build_source_series(archive_rows, source_key):
    """Chronological (ts, value) pairs for one source key, skipping rows
    where it's absent -- absence is never imputed as zero or as the
    prior value."""
    return [
        (row["observation_ts"], float(row["sources"][source_key]))
        for row in archive_rows
        if row.get("sources", {}).get(source_key) is not None
    ]


def _build_decision_record(subject, anchor_ts, cycle_ts, classifications, confirmation, primary_source,
                            target_horizon_hours=EXPERIMENT5_TARGET_HORIZON_HOURS):
    lifecycle_status = "MONITOR" if confirmation["classification"] == "CROSS_SOURCE_CONFIRMATION" else "OBSERVATION"
    direction = None
    for info in classifications.values():
        d = info.get("sequence", {}).get("direction")
        if d is not None:
            direction = d
            break
    summary = {k: v["sequence"]["classification"] for k, v in classifications.items()}
    statement = f"At anchor_ts={anchor_ts}, {subject} -- deterministic evidence-dynamics classification {summary}."
    return {
        "subject": subject,
        "statement": statement,
        "lifecycle_status": lifecycle_status,
        "decision": {
            "anchor_ts": anchor_ts,
            "cycle_ts": cycle_ts,
            "primary_source": primary_source,
            "direction": direction,
            "classifications": classifications,
            "confirmation": confirmation,
            "target_horizon_hours": target_horizon_hours,
            # Fixed at creation time, from facts already known at
            # anchor_ts -- never recomputed later, so a decision's own
            # eligibility can never silently drift with a future code
            # change to OUTCOME_HORIZON_MS or the default horizon.
            "eligible_ts": anchor_ts + OUTCOME_HORIZON_MS[target_horizon_hours],
        },
    }


def persist_experiment5_decision(conn, created_ts, record, source_analysis_ids=None):
    """INSERTs into research_hypotheses (migration 0008's table --
    already defined, not yet applied to production; see module
    docstring). Structurally refuses (raises ValueError, never silently
    clamps) any lifecycle_status outside EXPERIMENT5_ALLOWED_STATUSES."""
    if record["lifecycle_status"] not in EXPERIMENT5_ALLOWED_STATUSES:
        raise ValueError(
            f"Experiment 5 MVP may only create decisions with status in "
            f"{EXPERIMENT5_ALLOWED_STATUSES}, got {record['lifecycle_status']!r}"
        )
    payload = {"decision": record["decision"]}
    cursor = conn.execute(
        "INSERT INTO research_hypotheses "
        "(created_ts, last_updated_ts, subject, statement, source_analysis_ids, "
        " status, evidence_summary_json, out_of_sample_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            created_ts, created_ts, record["subject"], record["statement"],
            json.dumps(source_analysis_ids or []), record["lifecycle_status"],
            json.dumps(payload), None,
        ),
    )
    return cursor.lastrowid


def run_agent_cycle(conn, as_of_ts, created_ts, window_ms=DEFAULT_OBSERVE_WINDOW_MS,
                     target_horizon_hours=EXPERIMENT5_TARGET_HORIZON_HOURS):
    """One full OBSERVE..CREATE PROPOSAL pass. created_ts is required
    (explicit, no wall-clock default, matching this project's own
    persist_* convention). Never raises on "nothing interesting
    happened" -- that is the expected, common case, not an error."""
    archive_rows = observe(conn, as_of_ts, window_ms)
    if len(archive_rows) < 2:
        return {"status": "INSUFFICIENT_ARCHIVE_DATA", "n_archive_rows": len(archive_rows), "decisions_created": 0, "decision_ids": []}

    all_keys = sorted({k for row in archive_rows for k in row.get("sources", {}).keys()})
    anchor_ts = archive_rows[-1]["observation_ts"]

    classifications = {}
    per_source_events = []
    for key in all_keys:
        series = build_source_series(archive_rows, key)
        seq = classify_sequence(series)
        classifications[key] = {
            "sequence": seq,
            "acceleration": classify_acceleration(series),
            "reversal": detect_reversal(series),
        }
        if seq["classification"] in ("BULLISH_TREND", "BEARISH_TREND") and series:
            per_source_events.append((key, series[-1][0], seq["direction"]))

    confirmation = cross_source_confirmation(per_source_events)

    decision_ids = []
    decisions_created = 0

    if confirmation["classification"] == "CROSS_SOURCE_CONFIRMATION" and decisions_created < MAX_DECISIONS_PER_CYCLE:
        keys = confirmation["confirming_sources"]
        record = _build_decision_record(
            subject=f"experiment5:cross_source_confirmation:{','.join(keys)}",
            anchor_ts=anchor_ts, cycle_ts=as_of_ts,
            classifications={k: classifications[k] for k in keys},
            confirmation=confirmation, primary_source=keys[0],
            target_horizon_hours=target_horizon_hours,
        )
        decision_ids.append(persist_experiment5_decision(conn, created_ts, record))
        decisions_created += 1

    for key, info in classifications.items():
        if decisions_created >= MAX_DECISIONS_PER_CYCLE:
            break
        if info["reversal"]["classification"] == "REVERSAL":
            record = _build_decision_record(
                subject=f"experiment5:reversal:{key}",
                anchor_ts=anchor_ts, cycle_ts=as_of_ts,
                classifications={key: info},
                confirmation={"classification": "NO_CONFIRMATION", "confirming_sources": []},
                primary_source=key,
                target_horizon_hours=target_horizon_hours,
            )
            decision_ids.append(persist_experiment5_decision(conn, created_ts, record))
            decisions_created += 1

    return {
        "status": "OK",
        "n_archive_rows": len(archive_rows),
        "n_sources_observed": len(all_keys),
        "candidate_new_sources": sorted(set(all_keys) - KNOWN_V1_SOURCE_IDS),
        "confirmation": confirmation,
        "decisions_created": decisions_created,
        "decision_ids": decision_ids,
    }


def _direction_to_up_down(direction):
    """Converts source_dynamics' +1/-1/0/None direction vocabulary to
    outcome_engine's own "UP"/"DOWN"/"FLAT"/None vocabulary (see
    _resolve_outcome) so the two can be compared directly without
    silently mismatching on representation."""
    if direction is None or direction == 0:
        return None
    return "UP" if direction > 0 else "DOWN"


def _v1_baseline_direction(v1_composite_score):
    """V1's own baseline call for the SAME ts, reusing this codebase's
    existing >=50-midpoint convention (0-100 composite score; the same
    convention worker.js's own getCalibration()-style functions apply
    to a 0-1 p_up) -- not a new rule invented for this comparison.
    Returned in outcome_engine's own "UP"/"DOWN" vocabulary, not
    source_dynamics' +1/-1, so it compares directly against
    realized_direction without a representation mismatch."""
    if v1_composite_score is None:
        return None
    return "UP" if v1_composite_score >= 50 else "DOWN"


def evaluate_pending_decisions(conn, as_of_ts, horizon_hours=EXPERIMENT5_TARGET_HORIZON_HOURS):
    """Finds Experiment 5's own research_hypotheses rows
    (subject LIKE 'experiment5:%') not yet evaluated
    (out_of_sample_status IS NULL) whose horizon has now resolved, using
    outcome_engine.compute_forward_returns_from_history UNCHANGED (so the
    no-lookahead resolution rule is inherited verbatim, never
    reimplemented). Appends an "outcome" key to evidence_summary_json
    WITHOUT touching the existing "decision" key -- see module docstring
    "Append-only spirit, applied to a mutable table".

    Eligibility gate (fixes a real bug found by post-build audit): a
    decision anchored recently can already have a technically-"future"
    (later than anchor_ts, still real/already-elapsed) btc_data point
    sitting in the fetched window well before its own declared horizon
    has actually elapsed -- e.g. an archive/sentiment observation lags
    btc_data's own denser collection cadence, so by the time a decision
    is created its anchor_ts can already be a few hours "behind" the
    price feed. outcome_engine's own resolution rule (_resolve_outcome)
    is deliberately lenient -- "any strictly-later price point at/before
    anchor_ts+horizon resolves it" -- which is correct and UNCHANGED for
    every other consumer (source_analysis.py, hypothesis_gate.py,
    source_family_discrimination.py, fng_24h_robustness.py all query it
    over windows that have manifestly already fully elapsed by
    construction). This function's own usage pattern was the one place
    that assumption didn't hold: it gates a single LIVE pending decision
    whose intended horizon may not have elapsed yet at all. Calling
    compute_forward_returns_from_history before the intended horizon has
    elapsed does not leak future information (every price point involved
    already existed in D1 at as_of_ts) -- but it can resolve the
    decision off a price only minutes/hours ahead instead of the
    declared horizon, silently downgrading what the decision's own
    out_of_sample_status claims to have tested. The fix: never even
    attempt resolution until as_of_ts has reached the decision's own
    eligible_ts (anchor_ts + its own target_horizon_hours, fixed at
    creation time -- see _build_decision_record), leaving it pending
    exactly as an insufficient-data case would be left pending."""
    rows = conn.execute(
        "SELECT hypothesis_id, evidence_summary_json FROM research_hypotheses "
        "WHERE subject LIKE 'experiment5:%' AND out_of_sample_status IS NULL"
    ).fetchall()

    results = []
    for hypothesis_id, evidence_json in rows:
        payload = json.loads(evidence_json)
        decision = payload["decision"]
        anchor_ts = decision["anchor_ts"]
        # Older records (pre-dating this field) fall back to this call's
        # own horizon_hours parameter -- today that is always the same
        # EXPERIMENT5_TARGET_HORIZON_HOURS every decision was created
        # with, so this changes nothing for any decision actually created
        # by this codebase; it only avoids a KeyError if this function is
        # ever handed a hand-built payload that omits the field.
        decision_horizon_hours = decision.get("target_horizon_hours", horizon_hours)
        eligible_ts = decision.get("eligible_ts", anchor_ts + OUTCOME_HORIZON_MS[decision_horizon_hours])
        if as_of_ts < eligible_ts:
            continue  # target horizon has not elapsed yet -- not eligible, never forced

        outcomes = compute_forward_returns_from_history(conn, anchor_ts, anchor_ts + 1, decision_horizon_hours)
        if not outcomes or outcomes[0]["outcome_status"] != "RESOLVED":
            continue  # eligible, but no qualifying price point resolved yet -- left pending, never forced

        outcome = outcomes[0]
        # Horizon-tolerance check: outcome_engine matched SOME price at or
        # before the target (anchor_ts + horizon), but "at or before" can
        # still be far short of it if btc_data has a genuine gap spanning
        # the target -- see EXPERIMENT5_HORIZON_TOLERANCE_MS. This is
        # deliberately checked here, not inside outcome_engine.py, so the
        # shared engine's behavior for every other caller is untouched.
        target_ts = anchor_ts + OUTCOME_HORIZON_MS[decision_horizon_hours]
        realized_future_ts = outcome["realized_future_ts"]
        if target_ts - realized_future_ts > EXPERIMENT5_HORIZON_TOLERANCE_MS:
            continue  # nearest available price is too far short of the declared horizon -- left pending, never resolved off a stale price
        realized_direction = outcome["realized_direction"]  # "UP"/"DOWN"/"FLAT"/None
        agent_direction_raw = decision.get("direction")  # +1/-1/0/None
        agent_direction = _direction_to_up_down(agent_direction_raw)
        v1_direction = _v1_baseline_direction(outcome.get("v1_composite"))

        agent_correct = (agent_direction == realized_direction) if agent_direction is not None else None
        v1_correct = (v1_direction == realized_direction) if v1_direction is not None else None

        payload["outcome"] = {
            "evaluated_ts": as_of_ts,
            "horizon_hours": decision_horizon_hours,
            "eligible_ts": eligible_ts,
            "realized_direction": realized_direction,
            "forward_return_pct": outcome["forward_return_pct"],
            "agent_direction": agent_direction,
            "agent_correct": agent_correct,
            "v1_baseline_direction": v1_direction,
            "v1_baseline_correct": v1_correct,
        }
        if agent_correct is True:
            out_of_sample_status = "PASSED_HOLDOUT"
        elif agent_correct is False:
            out_of_sample_status = "FAILED_HOLDOUT"
        else:
            out_of_sample_status = "INSUFFICIENT_DATA_FOR_HOLDOUT"

        conn.execute(
            "UPDATE research_hypotheses SET evidence_summary_json = ?, out_of_sample_status = ?, last_updated_ts = ? "
            "WHERE hypothesis_id = ?",
            (json.dumps(payload), out_of_sample_status, as_of_ts, hypothesis_id),
        )
        results.append({"hypothesis_id": hypothesis_id, **payload["outcome"]})

    return {"n_evaluated": len(results), "results": results}


def compare_v1_vs_challenger(evaluated_results):
    """Aggregate scoreboard over already-evaluated decisions (the output
    of evaluate_pending_decisions) -- V1 BASELINE vs. AGENTIC CHALLENGER,
    directional accuracy only, no claim of improvement (per the build
    brief's own "do not claim improvement before testing"). Returns
    counts and rates, never a verdict label."""
    n = len(evaluated_results)
    agent_correct_n = sum(1 for r in evaluated_results if r["agent_correct"] is True)
    agent_evaluable_n = sum(1 for r in evaluated_results if r["agent_correct"] is not None)
    v1_correct_n = sum(1 for r in evaluated_results if r["v1_baseline_correct"] is True)
    v1_evaluable_n = sum(1 for r in evaluated_results if r["v1_baseline_correct"] is not None)
    return {
        "n_decisions_evaluated": n,
        "agent_accuracy": round(agent_correct_n / agent_evaluable_n, 3) if agent_evaluable_n else None,
        "agent_n": agent_evaluable_n,
        "v1_baseline_accuracy": round(v1_correct_n / v1_evaluable_n, 3) if v1_evaluable_n else None,
        "v1_baseline_n": v1_evaluable_n,
        "note": (
            "Descriptive comparison only over whatever decisions have resolved so far -- "
            "not a significance test, not a claim of improvement. See research/README.md's "
            "Experiment 5 section for the same walk-forward discipline PR5e-g already applies "
            "before any such claim would be warranted."
        ),
    }
