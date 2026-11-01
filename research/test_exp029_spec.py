"""The EXP-029 specification quotes the calculator's output, not a hand-picked number, and names no frozen experiment
as changed."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import power  # noqa: E402

SPEC = (Path(__file__).resolve().parent / "plans" / "2026-11-01-exp029-weighting-spec.md").read_text()


def test_checkpoint_equals_the_calculator():
    n = power.n_sign_test(0.65, m=3)
    assert n == 122
    assert f"**{n}** informative units per comparison" in SPEC


def test_three_comparisons_are_declared_and_holm_sized():
    assert "m = 3 comparisons" in SPEC and "Holm across the 3 comparisons" in SPEC
    for w in ("| W0 |", "| W1 |", "| W2 |", "| W3 |"):
        assert w in SPEC


def test_spec_is_proposed_and_preserves_frozen_experiments():
    assert SPEC.startswith("# EXP-029") and "**Status: PROPOSED.**" in SPEC
    for exp in ("EXP-023", "EXP-025", "EXP-027", "EXP-028"):
        assert exp in SPEC
    assert "never filled with a neutral value" in SPEC
    assert "No source is removed for low" in SPEC
