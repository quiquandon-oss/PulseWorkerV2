# Dashboard end-to-end check

Runs the real CryptoPulseV2 `index.html` in Chromium against the real `worker.js`, on the local SQLite D1
stand-in with synthetic data (`tests/replay/world.mjs`). Nothing reaches production.

    # 1. local world on 127.0.0.1:8791 (optional flags: force-variant, no-cycle, PREDICT_SNAPSHOTS=0, ...)
    node tests/e2e/world-server.mjs "$PWD/tests/replay/world.mjs" "$PWD" 8791 &
    # 2. the page's three CDN scripts, once (file name = URL-encoded URL without trailing slash)
    mkdir -p tests/e2e/cdn-cache && for u in https://cdn.tailwindcss.com \
      https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js \
      https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns@3.0.0/dist/chartjs-adapter-date-fns.bundle.min.js; do
      curl -sSL -o "tests/e2e/cdn-cache/$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "$u")" "$u"; done
    # 3. drive the page
    node tests/e2e/dashboard-check.mjs 8791 ../CryptoPulseV2/index.html /tmp/e2e-out

The summary reports the headline, the as-of label, the variant label, the stored snapshot it should match, every
Worker call and its status, blocked hosts and page errors; a screenshot is written to the output folder.
`force-variant` makes the stored BTC 24h selection a non-original variant with distinct cycle and resolved values,
so the headline source can be checked; `no-cycle` removes `cycle_p_up` to check the fallback label.
