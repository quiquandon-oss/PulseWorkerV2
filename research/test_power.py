"""research/power.py: sample sizes quoted in the frozen H2 plan are the calculator's output, not hand-picked."""
import re
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import power  # noqa: E402

PLAN = (Path(__file__).resolve().parent / "plans" / "2026-11-01-prospective-plans-h2.md").read_text()


def test_fisher_z_matches_the_textbook_formula():
    assert power.n_correlation(0.30) == 85          # standard table value for r = .30, alpha .05, power .80
    assert power.n_correlation(0.20) == 194
    assert power.n_partial_correlation(0.20, 1) == 195
    assert 780 <= power.n_partial_correlation(0.10, 1) <= 790     # plan: "rho = 0.10 would need about 785 days"


def test_exact_sign_test_power_is_correct_and_guarded_against_sawtooth():
    assert power.exact_sign_power(10, 0.5) <= 0.05                  # size at H0 never exceeds alpha
    for n in range(20, 200, 7):
        assert power.exact_sign_power(n, 0.5) <= 0.05 + 1e-12
    n = power.n_sign_test(0.65, m=2)
    assert all(power.exact_sign_power(k, 0.65, 0.025) >= 0.80 for k in range(n, n + 11))
    assert power.exact_sign_power(n - 1, 0.65, 0.025) < 0.80 or power.exact_sign_power(n - 5, 0.65, 0.025) < 0.80


def test_plan_checkpoints_equal_the_calculator():
    t = power.plan_table()
    rows = {r.split("|")[1].strip(): r for r in PLAN.splitlines() if r.startswith("| EXP-0")}
    assert f"**{t['EXP-025 hloi']}**" in rows["EXP-025"]
    assert f"**{t['EXP-027 rrs-v1']}**" in rows["EXP-027"]
    assert f"**{t['EXP-028 DVOL']}** each" in rows["EXP-028"] and t["EXP-028 DVOL"] == t["EXP-028 USDT supply"]
    assert f"**{t['EXP-002 per horizon']}** kept informative units per horizon" in PLAN
    assert f"**{t['EXP-011']}** units" in PLAN


def test_earliest_dates_follow_from_one_unit_a_day_from_2026_11_01():
    t = power.plan_table()
    for key, label in (("EXP-025 hloi", "EXP-025"), ("EXP-028 DVOL", "EXP-028")):
        last_outcome = date(2026, 11, 1) + timedelta(days=t[key])        # day n's 24h outcome resolves the next day
        row = next(r for r in PLAN.splitlines() if r.startswith(f"| {label}"))
        assert f"~{last_outcome.isoformat()}" in row


def test_population_start_is_after_the_seal_and_the_plan_says_frozen():
    assert "**2026-11-01T00:00:00Z** (epoch ms 1793491200000)" in PLAN
    assert re.search(r"^# .*\(FROZEN\)$", PLAN, re.M)
