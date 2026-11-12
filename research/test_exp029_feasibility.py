"""EXP-029 feasibility tools and the Steiger sample-size function used by the EXP-029b alternative."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exp029_feasibility as f  # noqa: E402
import power  # noqa: E402

W = {"a": (10.0, 1.0), "b": (2.0, 0.5), "c": (5.0, 0.2)}


def test_composites_follow_the_weightings_and_never_fill_missing_sources():
    s = {"a": 60, "b": 20}
    assert f.composite(s, W, "W0") == (60 * 10 + 20 * 1) / 11
    assert f.composite(s, W, "W1") == (60 * 1 + 20 * 0.5) / 1.5
    assert f.composite(s, W, "W2") == (60 * 10 + 20 * 2) / 12
    assert f.composite({}, W, "W0") is None
    assert f.composite({"zz": 90}, W, "W0") is None


def test_discordance_counts_only_differing_calls():
    readings = [{"sources": {"a": 60, "b": 20}}, {"sources": {"a": 45, "c": 90}}, {"sources": {"a": 70}}]
    out = f.feasibility(readings, W)
    # reading 1: W0 56.4 (UP), W1 46.7 (DOWN), W2 53.3 (UP); reading 2: W0 49.1 (DOWN), W1 52.5 (UP), W2 60 (UP)
    assert out["W1"]["discordant"] == 2 and out["W2"]["discordant"] == 1
    assert out["n_readings"] == 3


def test_steiger_sample_size_matches_simulation():
    n = power.n_dependent_correlation_difference(0.20, 0.10, 0.955, m=3)
    assert n == 95
    assert power.n_dependent_correlation_difference(0.20, 0.10, 0.992, m=3) < n      # more correlated composites: easier
    assert power.n_dependent_correlation_difference(0.15, 0.10, 0.955, m=3) > n      # smaller effect: harder
    rng = np.random.default_rng(7)
    S = np.array([[1, 0.955, 0.2], [0.955, 1, 0.1], [0.2, 0.1, 1]])
    zc = 2.394  # z_{1-0.05/3/2}
    rej = 0
    for _ in range(600):
        X = rng.multivariate_normal([0, 0, 0], S, size=n)
        C = np.corrcoef(X.T)
        r12, r1, r2 = C[0, 1], C[0, 2], C[1, 2]
        rm = (r1 + r2) / 2
        fh = min(1, (1 - r12) / (2 * (1 - rm ** 2)))
        h = (1 - fh * rm ** 2) / (1 - rm ** 2)
        z = (np.arctanh(r1) - np.arctanh(r2)) * np.sqrt((n - 3) / (2 * (1 - r12) * h))
        rej += abs(z) > zc
    assert rej / 600 > 0.74   # nominal 0.80; the 4000-run check in the decision doc gave 0.828
