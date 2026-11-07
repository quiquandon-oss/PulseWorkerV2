import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import feature_information as fi  # noqa: E402
import prediction_eval as pe  # noqa: E402


def test_spearman_known_values_and_ties():
    assert fi.spearman_ci(list(range(20)), list(range(20)))["rho"] == 1.0
    assert fi.spearman_ci(list(range(20)), list(range(20, 0, -1)))["rho"] == -1.0
    assert fi.ranks([1, 2, 2, 3]) == [0, 1.5, 1.5, 3]
    assert fi.spearman_ci([1, 2, 3], [1, 2, 3])["rho"] is None          # n < 10: not reported


def test_uses_pre_seal_data_only_and_past_volatility():
    closes = {pe.SEAL_START_TS - 2000 * pe.H + i * pe.H: 100 + math.sin(i / 7) * 3 + i * 0.001 for i in range(4000)}
    closes = {t: p for t, p in closes.items() if pe.price_usable(t)}
    out = fi.analyse(closes)
    assert out["24h"]["n_units"] > 0
    # trailing volatility only uses closes at or before t
    t = max(closes) - 100 * pe.H
    v1 = fi.trailing_vol(closes, t)
    later = dict(closes); later.update({k: v * 5 for k, v in closes.items() if k > t})
    assert fi.trailing_vol(later, t) == v1
