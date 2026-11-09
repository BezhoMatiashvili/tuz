# Automotive API and interface QA — 2026-11-08

## Scope and evidence
- Actual API code exercised through injected ASGI services: 13 tests pass.
- Test command: `/tmp/tuz-test-env/bin/python -m unittest discover -s tests -p test_automotive_api.py -v`.
- Cases: AP104/2 query routing, catalog output field allowlist, missing price/stock, negative fitment and collision candidate preservation, fitment/xref query lookup, VIN/validation error redaction, untrusted provider-status rejection, bool-year rejection, origin/host denial, expensive-call limits, separate operator authorization, durable job before 202, pending review despite imported VERIFIED, body cap, CSP/text-only frontend, safe provider/DB failures, custom CLI port origins.
- Test harness uses AsyncClient ASGITransport and test-only 10ms event-loop heartbeat because this sandbox cannot wake asyncio from sync worker threads; even bare FastAPI TestClient hangs without this accommodation. No HTTP socket/provider/database integration claim.
- Ruff passes for owned Python files; Bandit passes for api.py/__main__.py; `node --check automotive/static/app.js` passes.

## Interface review
- Static preview of actual HTML/CSS/JS source inspected in headless Chrome at 1440px and 375px. Georgian fonts render, forms fit both widths, labels and selected context messages are visible.
- Screenshots: `/tmp/automotive-desktop.png`, `/tmp/automotive-mobile.png`.
- These screenshots show the initial empty application with no private catalog, VIN, credentials, or fabricated successful recommendations.
- Customer facts use labeled text fields, status badges, evidence links and actual inventory product cards, including explicit unknown price/stock. Nested data is never interpreted as HTML.
- Source review: changing vehicle fields, VIN/year, part code/manufacturer or chat question invalidates stale pending responses. VIN change clears previous selected vehicle and results.
- All input/textarea elements have explicit labels; keyboard focus outline is provided. No automated WCAG compliance claim.

## Blocked checks
- Browser MCP could not launch: missing X server for headful browser.
- Repeated headless interaction harness launches encountered sandbox `setsockopt: Operation not permitted` crashpad/FD ownership failures. No automated browser interaction or live E2E acceptance pass claimed.
- No visual baseline exists: visual regression verdict INCONCLUSIVE.
- Real PostgreSQL/catalog and live Anthropic/vPIC checks are outside these injected API tests; lead tracks external verification.

## Files owned
`automotive/api.py`, `automotive/__main__.py`, `automotive/static/index.html`, `automotive/static/style.css`, `automotive/static/app.js`, `tests/test_automotive_api.py`.
