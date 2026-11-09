# Unified VIN source search — 2026-11-09

Implemented one VIN entry point for all currently connected data sources. This is a
bounded discovery service, not a complete vehicle EPC or all-supplier coverage claim.
No migrations, package pins, warehouse records, synchronizer or explorer files changed
for this follow-up.

## Acceptance evidence

| Check | Evidence / result |
|---|---|
| Full Python suite | 167 tests: 154 passed, 13 skipped; `.venv/bin/python -m unittest discover -s tests -q` |
| New-code Ruff | `.venv/bin/ruff check automotive tests/test_automotive_*.py` passes |
| Security scan | `.venv/bin/bandit -q -r warehouse_sync automotive` passes |
| JavaScript syntax | `node --check automotive/static/app.js` passes |
| UI contract | `node tests/automotive_ui_test.cjs` passes against synthetic DOM/responses: one VIN request, polling, pagination, external text rendering, chat edits independent of the VIN search, stale VIN/configuration responses rejected |
| Default live federation | Existing configured runtime database and actual Motorium HTTP: 199 warehouse make/model candidates + 3 supplier records, 202 distinct rows, PARTIAL after seven of eight category queries; 73.7 seconds. Supplier MPNs PN0477W, RN1734V, RN33001V; all provisional, no verified car fitment claimed |
| Actual API + actual sources | POST `/api/v1/vehicles/search` returned 202; GET status/pages returned 200, using ASGI transport with actual database/provider reads. Narrow brake-pad query completed all bounded reads, 200 distinct rows across four pages: 199 warehouse candidates + one supplier record; code bridge performed two supplier-code queries and found one warehouse candidate |
| Warehouse preservation | Before/after count + ordered source-product-ID/row-hash digest matched during actual API acceptance. All serialized warehouse stock values remained null; no source quantities promoted to warehouse stock |
| VIN privacy | Actual and synthetic API snapshots exclude request VIN; jobs retain only HMAC input fingerprints, sanitized records and UUID identifiers. No raw captures/credentials in repository |
| Missing evidence | Actual fitment and cross-reference sources had no applicable records; actual maintenance result INSUFFICIENT_DATA, engine code unknown. No fabricated approvals, oil recommendation or confirmed parts seeded |

The first live default run preceded the optimization that removed redundant searches
for already-listed local primary codes. The final API acceptance exercises the optimized
bridge. The initial default run's source truncation remains recorded; it is not recast as
complete coverage. vPIC results can come from its existing sanitized cache; these checks
do not assert every request contacted vPIC live. Provider results may change.

## Behavior covered by synthetic tests

Independent failures and retained partial categories/pages; verified-negative exclusions;
exact manufacturer/code provenance grouping; punctuation/normalization/OEM collisions;
conflicting repeated supplier identity; fallback when vPIC is unavailable; no arbitrary
inventory on unknown vehicle; supplier-code inventory discovery without model labels;
manufacturer- and vehicle-restricted forward cross-reference evidence without inverse
traversal/substitution; configuration cannot replace decoded make/model/year; stable
snapshot paging, expiry, capacity, API validation/rate limits and late-worker immutability.

The warehouse text candidates and supplier assertions remain distinct from confirmed
vehicle compatibility. `COMPLETE` means bounded source reads finished; `coverage_complete`
remains false. Five-minute in-memory jobs require one application process and disappear
on restart. Operator enrichment jobs remain durable and separately authenticated.

## Unavailable checks and actual audit findings

- Real browser/layout/mobile E2E: BLOCKED. Chrome DevTools reported missing X server;
  the Node DOM checks are not a real browser pass. Existing screenshot artifacts predate
  this unified view. TCP/browser-to-provider E2E is not claimed from ASGI acceptance.
- Disposable PostgreSQL security/migration and loopback tests: 13 skipped; no disposable
  test admin URL/environment. Actual warehouse read acceptance above is separate.
- Full repository lint retains the previously recorded unrelated explorer formatting
  failures; only affected automotive files/tests are reported clean here.
- Ordinary pip-audit was blocked by DNS/cache restrictions. Running the same installed
  audit CLI through the available Python command path completed the advisory lookup and
  reported **four existing vulnerable pins** (not a clean dependency audit):

| Package | Installed | Audit ID | Reported fix |
|---|---|---|---|
| lxml | 5.4.0 | PYSEC-2026-87 | 6.1.0 |
| pytest | 8.3.5 | PYSEC-2026-1845 | 9.0.3 |
| python-dotenv | 1.2.1 | PYSEC-2026-2270 | 1.2.2 |
| requests | 2.32.5 | PYSEC-2026-2275 | 2.33.0 |

These are pip-audit's reported package findings, not an independent exploitability
assessment. Dependency pins/lock were not upgraded in this source-federation change.
A compatible locked upgrade and regression run remain security follow-up work.

## Self-review

| Axis | Score | Evidence / improvement |
|---|---|---|
| Accuracy | 4/5 | Actual database + supplier + API acceptance; verified fitment still absent and browser behavior unexercised |
| Completeness | 3/5 | All connected sources wired; exhaustive factory/supplier coverage needs additional authorized catalogs, and browser acceptance remains blocked |
| Clarity | 4/5 | Georgian source progress and uncertainty labels; long candidate lists still require paging |
| Actionability | 4/5 | Restart, enter VIN, one submit; dependencies still need audited updates |
| Conciseness | 4/5 | Main flow is one search; protocol and limits moved to documentation |

Overall 3.8/5. Highest-impact remaining improvements: obtain actual authorized
configuration-specific fitment/EPC data, upgrade audited package pins compatibly, and
exercise the completed interface in a real desktop/mobile browser. The coverage gap
cannot be fixed by generating more confident text. The user's likely remaining concern
is exact compatible parts, rather than candidate discovery; the UI states that distinction.
