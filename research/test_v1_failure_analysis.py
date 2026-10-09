import re
from pathlib import Path

import v1_failure_analysis as fa

JS = Path(__file__).resolve().parent.parent / "learning" / "learning-core.js"


def test_weights_match_the_research_lab_methodology():
    js = {m.group(1): (int(m.group(2)), float(m.group(3)))
          for m in re.finditer(r"\{ id: '(\w+)', label: '[^']*', weight: (\d+), confidence: ([\d.]+)", JS.read_text())}
    assert js == fa.V1_DEFAULTS


def test_js_rounding_and_composite():
    assert fa.js_round(2.5) == 3 and fa.js_round(-0.5) == 0
    assert fa.v1_composite({"fng": 60, "funding": 40}) == fa.js_round((60 * 14 + 40 * 15) / 29)
    assert fa.v1_composite({}) is None and fa.v1_composite({"fng": 60}, drop=["fng"]) is None


def test_lean_convention_and_day_units():
    assert [fa.lean(v) for v in (55, 54, 46, 45, None)] == ["UP", "NEUTRAL", "NEUTRAL", "DOWN", None]
    rows = [{"ts": 0, "failed": True}, {"ts": 3600000, "failed": False}, {"ts": 86400000, "failed": False}]
    assert [r["ts"] for r in fa.first_per_day(rows)] == [0, 86400000]


def test_spearman():
    assert abs(fa.spearman(list(range(10)), list(range(10))) - 1) < 1e-12
    assert abs(fa.spearman(list(range(10)), list(range(10, 0, -1))) + 1) < 1e-12
    assert fa.spearman([1, 2], [1, 2]) is None


def test_registered_candidates_are_frozen():
    import hashlib
    import json
    reg = json.loads((Path(__file__).resolve().parent / "v1_candidate_registration.json").read_text())
    assert hashlib.sha256(json.dumps(reg, sort_keys=True).encode()).hexdigest() == "66067b324743bad052f4404d7ecbaaac76e6678116026ddd22dc8d73b22a266d"
    assert reg["registered_utc"] == "2026-10-09T16:00:00Z" and [c["id"] for c in reg["candidates"]] == ["T-A1", "T-A2", "T-A3"]
