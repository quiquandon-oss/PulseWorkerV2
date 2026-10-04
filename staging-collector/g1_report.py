#!/usr/bin/env python3
"""
G1-A/G1-B report for the NON-WRITING live validation (test.yml job `g1-live-validation`). Pure: reads the outputs of
the reviewed collector's dry runs and prints the summary. No network, no database, no credentials.

    python3 g1_report.py --btc-log btc-dryrun.txt --capture capture-dryrun.json --history-log history-dryrun.txt
"""
import argparse
import json
import sys

PRODUCTION_MARKERS = ("workers.dev", "script.google")
NOT_SENT_ACTIONS = ("BLOCK", "CAPTURE", "MOCK", "SHIM")


def _result_line(text):
    lines = [l for l in text.splitlines() if l.startswith("RESULT:")]
    return lines[-1][len("RESULT:"):].strip() if lines else None


def parse_btc(text):
    obs_lines = [l for l in text.splitlines() if l.startswith("Observation: ")]
    obs = json.loads(obs_lines[-1][len("Observation: "):]) if obs_lines else None
    result = _result_line(text)
    report = {"btc_result": result, "btc_passed": bool(result and result.startswith("WRITTEN"))}
    if obs:
        hl, cb = obs.get("price"), obs.get("cross_check_price")
        report.update(hyperliquid_price=hl, coinbase_price=cb,
                      pct_difference=round(abs(hl - cb) / cb * 100, 4) if hl and cb else None)
    return report


def summarize_capture(capture):
    payload = capture.get("payload") if isinstance(capture.get("payload"), dict) else None
    sources = (payload or {}).get("sources") or {}
    log = capture.get("request_log") or []
    blocked = sorted({f"{e['method']} {e['url'].split('?')[0]}" for e in log if e.get("action") == "BLOCK"})
    leaked = [e for e in log if any(m in e.get("url", "") for m in PRODUCTION_MARKERS)
              and e.get("action") not in NOT_SENT_ACTIONS]
    return {
        "composite_captured": payload is not None,
        "no_observation_reason": capture.get("no_observation_reason"),
        "sources_resolved": len(sources),
        "unresolved_sources": capture.get("excluded_sources"),
        "btcPrice_populated": isinstance((payload or {}).get("btcPrice"), (int, float)),
        "score": (payload or {}).get("score"),
        "technical_score": (payload or {}).get("technicalScore"),
        "blocked_request_count": len([e for e in log if e.get("action") == "BLOCK"]),
        "blocked_requests": blocked,
        "production_requests_not_blocked": leaked,
        "fixture_mode": capture.get("fixture_mode"),
    }


def build_report(btc_text, capture, history_text):
    report = parse_btc(btc_text)
    report.update(summarize_capture(capture))
    report["history_validation"] = _result_line(history_text)
    history_ok = bool(report["history_validation"] and report["history_validation"].startswith("WRITTEN"))
    if report["production_requests_not_blocked"] or report["fixture_mode"] is not False:
        report["RESULT"] = "FAIL"
    elif report["btc_passed"] and report["composite_captured"] and history_ok:
        report["RESULT"] = "PASS"
    else:
        report["RESULT"] = "INCOMPLETE"
    return report


def to_markdown(r):
    rows = [
        ("BTC Hyperliquid price", r.get("hyperliquid_price")), ("BTC Coinbase price", r.get("coinbase_price")),
        ("Difference (%)", r.get("pct_difference")), ("BTC passed", r.get("btc_passed")),
        ("Composite captured", r.get("composite_captured")), ("Sources resolved", r.get("sources_resolved")),
        ("Unresolved sources", r.get("unresolved_sources")), ("btcPrice populated", r.get("btcPrice_populated")),
        ("Score", r.get("score")), ("Technical score", r.get("technical_score")),
        ("History validation (local replica)", r.get("history_validation")),
        ("Blocked requests", r.get("blocked_request_count")),
        ("Production requests NOT blocked", len(r.get("production_requests_not_blocked") or [])),
        ("RESULT", r.get("RESULT")),
    ]
    out = ["## G1-A / G1-B live validation (non-writing)", "", "| Check | Value |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in rows]
    out += ["", "Blocked (never sent):", ""] + [f"- `{b}`" for b in r.get("blocked_requests") or []]
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--btc-log", required=True)
    parser.add_argument("--capture", required=True)
    parser.add_argument("--history-log", required=True)
    parser.add_argument("--summary-md")
    args = parser.parse_args(argv)
    with open(args.btc_log) as f:
        btc_text = f.read()
    with open(args.history_log) as f:
        history_text = f.read()
    try:
        with open(args.capture) as f:
            capture = json.load(f)
    except (OSError, ValueError):
        capture = {}
    report = build_report(btc_text, capture, history_text)
    print(json.dumps(report, indent=2))
    if args.summary_md:
        with open(args.summary_md, "a") as f:
            f.write(to_markdown(report) + "\n")
    print(f"RESULT: {report['RESULT']}")
    return 0 if report["RESULT"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
