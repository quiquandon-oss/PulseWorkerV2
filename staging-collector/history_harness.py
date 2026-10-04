#!/usr/bin/env python3
"""
Runs the UNMODIFIED pinned CryptoPulse index.html in headless Chromium and captures the composite payload the
page would POST to the production V1 /history -- without that request, or any other production request, ever
leaving the machine. Writes nothing anywhere except the capture file.

    python3 history_harness.py --index-file <CryptoPulse checkout>/index.html --out capture.json

Every request the page makes is classified by harness_policy.decide() (deny by default): public data APIs are
fetched by the browser itself; POST <V1>/history is captured and answered locally; three stateless V1 relays
are answered by relay_shims.py from the same public upstreams; everything else -- every other production V1
path, any *.workers.dev host, script.google.com, unknown hosts, WebSockets -- is aborted and logged.

The capture file is the input of `collector.py history-ingest`. It records the pinned commit, the index.html
SHA-256, the payload (or why there is none), the excluded sources and the complete request log.
--self-test-public-fixtures answers PUBLIC requests from a test responder instead of the network (no network at
all); such captures are stamped fixture_mode=true and collector.py refuses to ingest them.
"""
import argparse
import hashlib
import http.server
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness_policy as policy  # noqa: E402
import relay_shims  # noqa: E402
from collector import KNOWN_SOURCE_IDS, PINNED_CRYPTOPULSE_COMMIT, PINNED_CRYPTOPULSE_INDEX_SHA256  # noqa: E402

DEFAULT_TIMEOUT_S = 240
SHIM_TIMEOUT_S = 20


def _sha256_file(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _serve_single_file(index_path):
    """127.0.0.1-only server delivering exactly the pinned index.html (at / and /index.html), 404 otherwise."""
    with open(index_path, "rb") as f:
        body = f.read()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.split("?")[0] in ("/", "/index.html"):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _real_fetch_text(url, headers):
    policy.check_shim_upstream(url)
    request = urllib.request.Request(url, headers=dict({"User-Agent": "staging-collector-harness"}, **headers))
    with urllib.request.urlopen(request, timeout=SHIM_TIMEOUT_S) as response:
        return response.read().decode("utf-8", "replace")


def run_harness(index_file, timeout_s=DEFAULT_TIMEOUT_S, public_responder=None, shim_fetch_text=None,
                settle_s=2.0):
    """Returns the capture dict. public_responder(method, url, post_body) -> (status, content_type, body) is
    only for the offline self-test; when given, NOTHING reaches the network."""
    from playwright.sync_api import sync_playwright

    index_sha = _sha256_file(index_file)
    if index_sha != PINNED_CRYPTOPULSE_INDEX_SHA256:
        raise SystemExit(f"index.html SHA-256 {index_sha} is not the pinned {PINNED_CRYPTOPULSE_INDEX_SHA256} "
                         f"(CryptoPulse {PINNED_CRYPTOPULSE_COMMIT}). Refusing to run a different page.")
    fixture_mode = public_responder is not None
    shim_fetch_text = shim_fetch_text or _real_fetch_text
    server = _serve_single_file(index_file)
    port = server.server_address[1]
    log, captures, console = [], [], []
    started = time.time()

    def record(method, url, action, reason, status=None):
        log.append({"t_ms": int((time.time() - started) * 1000), "method": method,
                    "url": policy.redact_url(url), "action": action, "reason": reason, "status": status})

    def on_route(route):
        req = route.request
        action, reason = policy.decide(req.method, req.url, local_port=port)
        if action == policy.LOCAL:
            record(req.method, req.url, action, reason)
            return route.continue_()
        if action == policy.CAPTURE:
            try:
                body = json.loads(req.post_data or "null")
            except ValueError:
                body = {"__unparseable__": req.post_data}
            captures.append({"captured_at_ms": int(time.time() * 1000), "payload": body})
            record(req.method, req.url, action, reason, 200)
            return route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
        if action == policy.MOCK:
            record(req.method, req.url, action, reason, 200)
            return route.fulfill(status=200, content_type="application/json", body='{"history":[]}')
        if action == policy.SHIM:
            status, payload = relay_shims.handle(req.url, shim_fetch_text)
            record(req.method, req.url, action, reason, status)
            return route.fulfill(status=status, content_type="application/json", body=json.dumps(payload))
        if action == policy.PUBLIC:
            if fixture_mode:
                status, ctype, body = public_responder(req.method, req.url, req.post_data)
                record(req.method, req.url, action, "fixture: " + reason, status)
                return route.fulfill(status=status, content_type=ctype, body=body)
            record(req.method, req.url, action, reason)
            return route.continue_()
        record(req.method, req.url, policy.BLOCK, reason)
        return route.abort("blockedbyclient")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        context.route("**/*", on_route)
        if hasattr(context, "route_web_socket"):
            def on_ws(ws):
                record("WS", ws.url, policy.BLOCK, "WebSockets are never opened")
                ws.close()
            context.route_web_socket("**/*", on_ws)
        page = context.new_page()
        page.on("console", lambda m: console.append(f"{m.type}: {m.text}"[:500]))
        page.goto(f"http://127.0.0.1:{port}/index.html", wait_until="domcontentloaded")
        deadline = time.time() + timeout_s
        while not captures and time.time() < deadline:
            page.wait_for_timeout(500)
        if captures:
            page.wait_for_timeout(int(settle_s * 1000))  # let in-flight requests land in the log
        browser.close()
    server.shutdown()

    first = captures[0] if captures else None
    payload = first["payload"] if first else None
    composite_lines = [c for c in console if "Composite" in c or "composite" in c]
    no_obs_reason = None
    if payload is None:
        low = [c for c in composite_lines if "not written to D1" in c or "only" in c]
        no_obs_reason = (low[-1] if low else f"no /history POST within {timeout_s}s")
    sources = payload.get("sources") if isinstance(payload, dict) else None
    return {
        "harness_version": "history-harness-v1",
        "fixture_mode": fixture_mode,
        "cryptopulse_commit": PINNED_CRYPTOPULSE_COMMIT,
        "index_sha256": index_sha,
        "captured_at_ms": first["captured_at_ms"] if first else int(time.time() * 1000),
        "payload": payload,
        "no_observation_reason": no_obs_reason,
        "additional_captures": len(captures) - 1 if captures else 0,
        "excluded_sources": sorted(KNOWN_SOURCE_IDS - set(sources)) if isinstance(sources, dict) else None,
        "blocked_request_count": sum(1 for e in log if e["action"] == policy.BLOCK),
        "request_log": log,
        "console_composite_lines": composite_lines[-20:],
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Capture the pinned CryptoPulse composite (no data is sent).")
    parser.add_argument("--index-file", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S)
    args = parser.parse_args(argv)
    capture = run_harness(args.index_file, timeout_s=args.timeout)
    with open(args.out, "w") as f:
        json.dump(capture, f, indent=2)
    summary = {k: capture[k] for k in ("payload", "no_observation_reason", "excluded_sources",
                                       "blocked_request_count")}
    print(json.dumps(summary, indent=2))
    production = [e for e in capture["request_log"] if e["action"] not in (policy.BLOCK, policy.CAPTURE,
                  policy.MOCK, policy.SHIM) and ("workers.dev" in e["url"] or "script.google" in e["url"])]
    if production:  # unreachable by construction; checked anyway
        print(f"PRODUCTION REQUEST NOT BLOCKED: {production}", file=sys.stderr)
        return 9
    print(f"RESULT: {'CAPTURED' if capture['payload'] is not None else 'NO_OBSERVATION'} -> {args.out}")
    return 0 if capture["payload"] is not None else 7


if __name__ == "__main__":
    sys.exit(main())
