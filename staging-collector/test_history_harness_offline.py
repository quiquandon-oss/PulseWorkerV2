"""Runs the real pinned CryptoPulse page in headless Chromium with test fixtures for every public API (no network).
Skipped when Playwright or the pinned index.html is not available (e.g. the default CI job)."""
import os

import pytest

pytest.importorskip("playwright.sync_api")
INDEX = os.environ.get("CRYPTOPULSE_INDEX_FILE", "")
pytestmark = pytest.mark.skipif(not os.path.isfile(INDEX), reason="set CRYPTOPULSE_INDEX_FILE to the pinned index.html")


def test_pinned_page_computes_a_composite_unattended_and_nothing_reaches_production():
    import collector as c
    import harness_fixtures as fx
    import harness_policy as p
    from history_harness import run_harness

    cap = run_harness(INDEX, timeout_s=180, public_responder=fx.public_responder, shim_fetch_text=fx.shim_fetch_text)
    payload = cap["payload"]
    assert payload is not None, cap["no_observation_reason"]
    assert cap["fixture_mode"] is True
    assert c.validate_history_payload(payload, cap["captured_at_ms"], cap["captured_at_ms"]) == []
    assert "fng" in payload["sources"] and payload["technicalScore"] is not None
    for entry in cap["request_log"]:
        if "workers.dev" in entry["url"] or "script.google" in entry["url"]:
            assert entry["action"] in (p.BLOCK, p.CAPTURE, p.MOCK, p.SHIM), entry
    blocked = {e["url"].split("?")[0] for e in cap["request_log"] if e["action"] == p.BLOCK}
    assert any("script.google.com" in u for u in blocked)
    assert any(u.endswith("/foufi-latest") for u in blocked)
