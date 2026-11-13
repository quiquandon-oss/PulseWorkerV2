import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import price_row_duplicates as prd  # noqa: E402

H = prd.H


def _rows(n_runs, per_run, step_h=3, differ=False):
    out, price = [], 100.0
    for i in range(n_runs):
        price *= 1.01 if i % 2 else 0.995
        for j in range(per_run):
            out.append({"ts": i * step_h * H + j * 2000, "price": price + (0.5 if differ and j else 0), "tech": 50 + j})
    return out


def test_groups_rows_into_runs_and_counts_duplicates():
    r = prd.analyse(_rows(40, 2))
    assert r["rows"] == 80 and r["runs"] == 40 and r["rows_per_run"] == {"2": 40}
    assert r["multi_row_runs"] == 40 and r["multi_row_runs_with_different_prices"] == 0
    assert r["multi_row_runs_with_different_technical_score"] == 40
    assert r["share_of_rows_that_repeat_an_earlier_row_of_the_same_run"] == 0.5


def test_single_row_series_is_unchanged_by_collapsing():
    r = prd.analyse(_rows(40, 1))
    assert r["multi_row_runs"] == 0 and r["first_run_with_more_than_one_row"] is None
    spans = r["median_hours_covered"]["volatility_lookback_14_rows"]
    assert spans["stored"] == spans["one_row_per_run"] == 39.0
    assert r["trailing_volatility_stored_over_one_row_per_run"]["median_ratio"] == 1.0


def test_two_rows_per_run_halve_the_time_a_row_counted_lookback_covers():
    r = prd.analyse(_rows(60, 2))
    v = r["median_hours_covered"]["volatility_lookback_14_rows"]
    t = r["median_hours_covered"]["technical_lookback_30_rows"]
    assert v["one_row_per_run"] == 39.0 and 18.0 <= v["stored"] <= 21.0
    assert t["one_row_per_run"] == 87.0 and 42.0 <= t["stored"] <= 45.0
    assert r["trailing_volatility_stored_over_one_row_per_run"]["median_ratio"] != 1.0


def test_different_prices_within_a_run_are_counted():
    assert prd.analyse(_rows(10, 2, differ=True))["multi_row_runs_with_different_prices"] == 10


def test_main_reads_exported_csvs_and_reports_missing_files(tmp_path, capsys):
    with open(tmp_path / "btc_data.csv", "w", newline="") as f:
        w = csv.writer(f); w.writerow(["id", "ts", "btc_price", "technical_score"])
        for i, r in enumerate(_rows(20, 2)):
            w.writerow([i, r["ts"], r["price"], r["tech"]])
    assert prd.main(["--dir", str(tmp_path), "--out", str(tmp_path / "o.json")]) == 0
    import json
    d = json.loads((tmp_path / "o.json").read_text())
    assert d["series"]["BTC"]["runs"] == 20 and "missing" in d["series"]["ETH"]


def test_technical_score_port_matches_the_worker_formula():
    assert prd.simple_technical_score([1, 2, 3]) == 50
    assert prd.simple_technical_score([1, 2, 3, 4, 5, 6]) == 100
    assert prd.simple_technical_score([5, 5, 5, 5, 5, 5]) == 50
    assert prd.simple_technical_score([10, 11, 10, 11, 10, 12, 11]) == 57  # gains 4, losses 3 -> 100*4/7


def test_technical_score_comparison_is_zero_without_duplicates_and_reported_with_them():
    assert prd.analyse(_rows(80, 1))["technical_score_stored_series_vs_one_row_per_run"] == {"n": 0}
    r = prd.analyse(_rows(80, 2))["technical_score_stored_series_vs_one_row_per_run"]
    assert r["n"] == 50 and r["max_abs_difference"] >= 0
