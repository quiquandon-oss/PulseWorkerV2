"""PEV-1P window and its no-peeking boundary (protocol research/plans/2026-11-02-pev1p-protocol.md).

The protocol allows no label value, metric or prediction for the PEV-1P window before the checkpoint; only `counts`
mode may run. This module enforces that inside the research code.

Provenance, not mutable timestamps. A close read from a candle file is a `Price` that carries the time it became
available; a label is a `Label` that carries its original decision time (set only by prediction_eval.build_rows).
Both are read-only and survive copy, deepcopy and pickle. Every check validates the data's own provenance and
refuses data whose provenance disagrees with the timestamp it is filed under, so copying rows, re-stamping rows or
units, and shifting candle timestamps are all refused before the checkpoint.

Enforced at:
  - prediction_eval.build_rows: no shifted or re-filed close (any horizons); no label for a window decision time;
  - prediction_eval model functions (model_a, model_corrected, model_lr, model_shrink, climatology, persistence):
    no prediction for a window decision time;
  - prediction_eval.predict_all: computes no prediction for window units (PEV-1's last units are such units; they are
    unresolved and excluded from every metric, so PEV-1's report is unchanged), and refuses re-stamped rows;
  - prediction_eval.score_block and goal_a: refuse window or re-stamped labels;
  - prospective_pev.final_for_coin and `prospective_pev.py final`: refuse outright.

The time used is the machine's wall clock, read by wall_clock_ms() here, never a caller's argument: the `now_ts`
parameters of prospective_pev only choose a population. Tests on synthetic data replace wall_clock_ms with pytest's
monkeypatch; committed research code must not (test_pev1p_guard scans for it).

Plain floats carry no provenance; they are treated as synthetic data and checked by the timestamp they are filed
under. The guarantee is therefore: before the checkpoint, nothing built from real candle files by this code yields a
window label, prediction, metric or final result, however its rows are copied, re-stamped or shifted.

What no code check can stop, because Python cannot make provenance unforgeable: a caller who deliberately builds
inputs by hand — real prices or outcomes converted to plain numbers, or Price/Label objects constructed with false
stamps — and passes them to the public functions, or recomputes returns from raw candle files with its own
arithmetic. Such a caller has already computed or moved window data outside this code. That stays a policy and review
matter (AGENTS.md).
"""
import time
from datetime import datetime, timezone

TEST_START_TS = 1793491200000          # 2026-11-01T00:00:00Z: first decision time of the PEV-1P window
CHECKPOINT_TS = 1809129600000          # 2027-05-01T00:00:00Z: the one confirmatory analysis


class PrematurePEV1PEvaluation(RuntimeError):
    """A PEV-1P window outcome or prediction, or the final analysis, was requested before the checkpoint."""


class _Stamped(float):
    """A float with a read-only provenance timestamp. Arithmetic on it returns plain floats."""
    __slots__ = ("_stamp",)

    def __new__(cls, value, stamp=None):
        obj = super().__new__(cls, value)
        object.__setattr__(obj, "_stamp", stamp)
        return obj

    def __setattr__(self, name, value):
        raise AttributeError(f"{type(self).__name__} provenance is read-only")

    def __reduce__(self):
        return (type(self), (float(self), self._stamp))


class Price(_Stamped):
    """A close and the time it became available, as read from a candle file."""
    __slots__ = ()

    @property
    def available_at(self):
        return self._stamp


class Label(_Stamped):
    """A forward return and the decision time it belongs to, as computed by build_rows."""
    __slots__ = ()

    @property
    def decision_ts(self):
        return self._stamp


def wall_clock_ms():
    return int(time.time() * 1000)


def _iso(ts):
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def in_window(ts):
    return ts is not None and TEST_START_TS <= ts < CHECKPOINT_TS


def before_checkpoint():
    return wall_clock_ms() < CHECKPOINT_TS


def refuse(what):
    raise PrematurePEV1PEvaluation(
        f"PEV-1P no-peeking guard: refusing {what} before the checkpoint {_iso(CHECKPOINT_TS)} "
        f"(wall clock {_iso(wall_clock_ms())}). Only `counts` mode may run before then. "
        "PEV-1 re-runs must use the pinned inputs in research/evaluations/pev1-inputs/.")


def _price_stamp(p):
    return p.available_at if isinstance(p, Price) else None


def _check_filed(stamp, ts, what):
    if stamp is not None and stamp != ts:
        refuse(f"{what}: data from {_iso(stamp)} filed under {_iso(ts)} (copied, re-stamped or shifted)")


def check_prices(closes):
    """Call before building rows: every stamped close must be filed under the time it became available. Covers every
    use of a price (labels, prev_ret, features), whatever horizons are requested."""
    if not before_checkpoint():
        return
    for t, p in closes.items():
        _check_filed(_price_stamp(p), t, "a price")


def check_label(t, p, fut, fut_ts):
    """Call before computing the label of decision time t from close p (filed at t) and fut (filed at fut_ts)."""
    if not before_checkpoint():
        return
    _check_filed(_price_stamp(p), t, "a label")
    _check_filed(_price_stamp(fut), fut_ts, "a label")
    if in_window(t):
        refuse(f"to compute the outcome of window decision time {_iso(t)}")


def is_window_unit(row):
    """True when a row or unit belongs to the window by its timestamp or by its price's provenance."""
    return in_window(row["ts"]) or in_window(_price_stamp(row.get("price")))


def check_query(row):
    """Call at the start of a model function: no prediction for a window decision time."""
    if not before_checkpoint():
        return
    _check_filed(_price_stamp(row.get("price")), row["ts"], "a prediction")
    if is_window_unit(row):
        refuse(f"a prediction for window decision time {_iso(row['ts'])}")


def check_rows(rows):
    """Call before using rows or units: none may carry a window label, or provenance that disagrees with its ts."""
    if not before_checkpoint():
        return
    for r in rows:
        _check_filed(_price_stamp(r.get("price")), r["ts"], "a row")
        for v in r.get("labels", {}).values():
            if v is None:
                continue
            stamp = v.decision_ts if isinstance(v, Label) else None
            _check_filed(stamp, r["ts"], "a label")
            if in_window(r["ts"]) or in_window(stamp) or in_window(_price_stamp(r.get("price"))):
                refuse(f"to use the outcome of window decision time {_iso(stamp or r['ts'])}")


def check_final():
    """Call at the start of the confirmatory (final) analysis."""
    if before_checkpoint():
        refuse("the PEV-1P final analysis")
