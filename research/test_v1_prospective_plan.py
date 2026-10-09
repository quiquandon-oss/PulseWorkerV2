"""Integrity of the T-A1 / T-A2 prospective plan: registration unchanged, addendum frozen, OI experiment untouched."""
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REG = HERE / "v1_candidate_registration.json"
ADD = HERE / "v1_candidate_registration_addendum_1.json"
OI_PREREG_SHA256 = "46c0d52b47350a7c7e83841e83ff646d8e5d0f5b9146d98908dab32411ff3e93"


def test_original_registration_unchanged():
    reg = json.loads(REG.read_text())
    assert hashlib.sha256(json.dumps(reg, sort_keys=True).encode()).hexdigest() == "66067b324743bad052f4404d7ecbaaac76e6678116026ddd22dc8d73b22a266d"
    assert hashlib.sha256(REG.read_bytes()).hexdigest() == "ac88bf2df2968f8ddfe62f76aa10be76ee765cd13f7dc226e929aa54669a7455"
    assert reg["registered_utc"] == "2026-10-09T16:00:00Z"


def test_addendum_frozen_and_consistent_with_registration():
    raw = ADD.read_bytes()
    raw.decode("ascii")
    assert hashlib.sha256(raw).hexdigest() == "c2ebdfcb75a1690b25f3a327bf7537f8ee6853e16a0dc6295bebd18b8ac7ae9c"
    add, reg = json.loads(raw), json.loads(REG.read_text())
    registered = {c["id"]: c["adjustment"] for c in reg["candidates"]}
    for c in add["candidates_evaluated"]:
        assert c["adjustment"] == registered[c["id"]]                      # transformations exactly as registered
    assert add["parent_registration"]["sha256_file_bytes"] == hashlib.sha256(REG.read_bytes()).hexdigest()
    assert add["fixed_horizon"]["units_required"] == 30
    assert add["holdout"]["boundary_utc"] == reg["registered_utc"]
    assert "0.1/3" in add["tests_at_the_look"]["research_level"] and "alpha 0.1" in add["tests_at_the_look"]["product_level"]
    assert OI_PREREG_SHA256 in add["not_changed"]


def test_oi_experiment_not_part_of_this_change():
    # The OI pre-registration lives in research/risk_regime_forward_eval.py on the research branch. This plan neither
    # carries nor edits it; if the file is present in a checkout, its registration must still hash to the pinned value.
    p = HERE / "risk_regime_forward_eval.py"
    if p.exists():
        import importlib.util
        spec = importlib.util.spec_from_file_location("fe", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert mod.PREREG_SHA256 == OI_PREREG_SHA256
    assert "risk_regime_forward" not in (HERE / "v1_prospective_eval.mjs").read_text()
