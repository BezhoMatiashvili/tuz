# Automotive intelligence implementation

Source: the supplied Automotive intelligence MVP with Anthropic plan, 2026-11-08.
No production migration, deployment, source write, purchase or external message was performed.

Statuses below distinguish implementation, synthetic/mocked verification, and blocked live
acceptance. VERIFIED applies only to the listed evidence; it does not imply production readiness.
All agents shared this checkout with explicit, nonoverlapping file ownership. Lead integrated
shared dependencies/chat/configuration/docs and independently reviewed safety-sensitive behavior.
The checkout has no Git metadata, so Git diff/history-based verification was unavailable.

| Requirement / acceptance | Owner | State | Files and acceptance evidence / remaining gate |
|---|---|---|---|
| Preserve synchronizer, explorer, catalog and migrations 0001–0005 | lead | VERIFIED regression; catalog BLOCKED | Existing 25 tests still pass; no edits in warehouse_sync/, db_ui/ or old migration files. Actual catalog connection fails. |
| Separate FastAPI app, synchronous PostgreSQL, Uvicorn, vanilla UI | API/lead | IMPLEMENTED; mocked API VERIFIED | automotive/{api,__main__,repository}.py, pyproject.toml, uv.lock; 13 ASGI tests |
| Private 0006 schema: identifiers, vehicles, VIN cache, evidence, fitments, directional refs, specs, approvals, jobs | database | IMPLEMENTED; real DB BLOCKED | migrations/0006_automotive.{up,down}.sql; 3 disposable PostgreSQL tests skip without admin URL |
| Separate runtime/enrichment/migration capabilities; RLS, public revokes, unchanged sync role | database | IMPLEMENTED; real DB BLOCKED | ops/sql/provision_automotive_roles.sql; role/RLS/owner-review integration test prepared |
| Original codes/manufacturer, combined values/parentheses, AP104/2, collision candidates | database | VERIFIED synthetic | automotive/codes.py, repository.py; test_automotive_database.py parser and ambiguity tests |
| No co-occurrence interchange; directional evidence and supersession | evidence/database | VERIFIED synthetic | compatibility tests; xref never inverted; restricted/superseded claims cannot authorize unconditional interchange |
| Current source fingerprints/parser versions prevent stale search; incremental index/dedupe | database | VERIFIED logic; DB BLOCKED | repository.py search fallback/index_batch; current-catalog staleness integration test prepared |
| Durable jobs before 202; checkpoint/retry/recovery and worker | database/API | VERIFIED mocked; DB BLOCKED | worker.py + API tests + worker tests; leases/atomic checkpoint test awaits PostgreSQL |
| vPIC VIN format/checksum/warnings; no invented engine details | evidence | VERIFIED mocked; live BLOCKED | vin.py; fixed official POST, streamed cap, explicit 200, warnings and market-context checksum tests |
| Keyed VIN hash cache, sanitized responses excluding echo/suggested VIN | evidence | VERIFIED mocked; cache DB BLOCKED | HMAC over provider/VIN/year, allowlisted fields, embedded VIN redaction; 30-day logical TTL |
| Operator-only bounded CSV/JSON import; owned/authorized evidence | evidence/API | VERIFIED mocked | importer.py; 1,000 records; separate authority/reuse/restrictions/review; rejects credential-bearing URLs |
| Imported VERIFIED label cannot verify | evidence/database/API | VERIFIED mocked; RLS BLOCKED | imports forced pending; protected payload fields stripped; enrichment cannot UPDATE evidence |
| VERIFIED/PROVISIONAL/CONFLICTED/UNKNOWN and explicit negative compatibility | evidence/API | VERIFIED synthetic | conflict/negative/metadata-only evidence tests; response preserves compatible and candidates |
| Oil requires exact applicable verified specs and current actual-product approvals | evidence | VERIFIED synthetic; real feeds BLOCKED | recommendations.py; engine/year scope, approval requirements, product fingerprint/viscosity gates |
| Missing specs/approval mismatch/stale approval return insufficient data | evidence | VERIFIED synthetic | missing engine/spec/approval/fingerprint, changed catalog identity, viscosity and conflict tests |
| Capacity/filter references unknown without independent support | evidence | IMPLEMENTED conservative limit | always null/empty in MVP; independent capacity/filter extraction is not implemented |
| Typed /api/v1 requests/responses, parameterized queries, slash code queries | API/database | VERIFIED mocked | API tests for slash, strict fields, negative results and output allowlist; docs list actual endpoints |
| Official Anthropic SDK, model/workspace/secrets configuration | lead | VERIFIED SDK/mock; live BLOCKED | chat.py/config.py; SDK wire serialization mock test; no configured key |
| Strict/local-authorized automotive reads only; no SQL/network/fs/enrichment writes | lead | VERIFIED mocked | unlisted tool/extra property/partial identifier rejection tests; fixed backend dispatch |
| Four model requests/eight tools/1,024 tokens, explicit auth/refusal/timeout/limit failures | lead | VERIFIED mocked | budget/error tests; initial VIN lookup counted; SDK automatic retries disabled |
| Per-turn usage accounting; metered inference only | lead | VERIFIED mocked | response + sanitized log counts tokens/calls/cache tokens; no monetary estimate/durable billing ledger |
| Final backend references; Georgian/English templates; no model factual prose | lead | VERIFIED mocked | unknown result references/free prose rejected; template + referenced records only; stale turns isolated |
| Georgian standalone VIN/config/chat/code/inventory/fitment/oil/evidence interface | API | IMPLEMENTED; static preview VERIFIED; interaction E2E BLOCKED | automotive/static/*; desktop/mobile screenshots and QA report in docs/automotive-review/ |
| Localhost, host/origin, rate/body limits, no VIN/secret errors, text rendering | API/lead | VERIFIED mocked/source review | security API tests, CSP, textContent; no browser secrets/storage; API access logging disabled |
| Nine original sample codes / combined-code examples / original VIN / every supplied Georgian query | lead/operator | BLOCKED exact original acceptance | Original plan includes only AP104/2; nine identifiers, actual sample VIN and full queries absent. Synthetic Georgian/code/VIN cases are labeled fixtures. |
| Actual catalog, price/stock/currency mapping and authorized technical feeds | operator | BLOCKED | Catalog OperationalError; mappings unknown; no fabricated successful inventory/evidence seed |
| Provider candidates' licensing/coverage and operation docs | lead | IMPLEMENTED | docs/automotive.md covers all named sources (warehouse/vPIC/authorized evidence/Anthropic); other candidate names absent |
| Regressions, new subsystem tests, lint, Bandit, dependency audit, browser QA | lead | mixed; see commands below | New code lint/security pass; full lint has unchanged legacy errors; audit/live browser checks blocked |

## Engineering cycles

### 1 — Baseline and boundaries
- Read AGENTS.md, warehouse schema/migration runner, existing configuration/runbook and selected
  ECC FastAPI, security-review, team-agent-orchestration, and agent-self-evaluation skills.
- Baseline `.venv/bin/python -m unittest discover -s tests -v`: 33 total, **25 passed, 8 skipped**.
- Assigned database, evidence and API workstreams to the three requested worker slots. No overlapping
  writes; lead owns shared integration. All workstreams completed and were reviewed by lead.

### 2 — Vertical slice and evidence
- Implemented exact current-catalog search, AP104/2-preserving parser, fixed-host vPIC decode,
  standalone Georgian UI and typed API. Added private migration/roles without modifying old files.
- Added authorized import, pending review, deterministic conflict evaluation, oil eligibility,
  durable indexing jobs, leases and resumable checkpoints.
- Focused worker evidence: database initially 9 pass/2 skip; evidence initially 18 pass; injected
  API initially 10 pass. These were intermediate counts; final counts below supersede them.
- Actual catalog read-only connection attempt: **BLOCKED OperationalError**; no data read or changed.
- Live fixed-endpoint vPIC POST attempted with a public synthetic fixture: **BLOCKED network**.

### 3 — Claude integration and independent acceptance review
- Added official SDK server-side orchestration, strict schemas/local authorization, bounded calls,
  explicit error states, usage accounting, current-turn references and deterministic templates.
- Cross-review found/fixed raw VINs in serialized context, partial identifier authorization,
  initial VIN budget accounting and unhandled psycopg errors. Tests now cover these attacks.
- Review found/fixed incomplete configuration scope, missing boolean fitment claims, hidden negative
  compatibility/collision candidates, stale product approvals and ignored viscosity requirements.
- Changed approval identity must match the current catalog fingerprint; absent fingerprint cannot
  authorize an actual inventory recommendation. No technical success records seeded.
- Worker errors now redact claim/failure-marker connection details, missing runtime URLs cannot use ambient libpq credentials, and expired fifth-attempt leases become failed/retry_exhausted (real SQL verification still blocked).
- API CLI custom ports now update allowed origins, strict types reject bool years, and UI invalidates
  stale pending answers after vehicle/code/question edits.
- Actual SDK tested with httpx.MockTransport: strict tool JSON reaches the wire and returned backend
  references ground the result. This is **not** a live Anthropic test.

### 4 — Verification, docs and environment limits
- Full new-code review, release/setup docs, env template, CI Bandit scope extended to automotive.
- Documented import/review operation, role provisioning, index recovery, privacy/licensing boundaries,
  conservative technical limitations and all blocked external gates.
- Desktop 1440px and mobile 375px screenshots inspected: Georgian text renders and initial layout
  fits. Saved screenshot/QA artifacts are under docs/automotive-review/. Browser MCP cannot start
  without X; automated headless interaction launches hit sandbox socket/FD restrictions. Actual
  browser-to-API/provider/database E2E is **BLOCKED**, not passed.
- Network/cache restrictions prevented ordinary dependency installation. Existing cached wheel
  metadata/packages were used in a separate Python 3.12 environment `/tmp/tuz-test-env`; no fake
  packages or local `/tmp` sources are in the repository lock. Existing locked package versions and
  artifact entries were preserved; new lock entries use official PyPI artifact URLs from cached
  registry metadata. Some new artifacts lack cached hashes; a fresh network-enabled locked install
  and dependency audit remain integration gates. Do not equate dry-run/lock validation with install.
- The existing mandatory development pin lxml==5.4.0 also cannot build here without libxml2/libxslt
  headers. Tests use a separate recovered runtime environment; its baseline transitive/dev versions
  are not all identical to the complete dev lock (see environment note below).

## Verification commands and outcomes

| Command/check | Result |
|---|---|
| `.venv/bin/python -m unittest discover -s tests -v` before changes | PASS: 25 passed, 8 skipped |
| `/tmp/tuz-test-env/bin/python -m unittest discover -s tests -v` integrated | PASS: 103 total, 92 passed, 11 skipped after final hardening |
| `/tmp/tuz-test-env/bin/ruff check automotive tests/test_automotive_*.py` | PASS |
| `/tmp/tuz-test-env/bin/bandit -q -r warehouse_sync automotive` | PASS (new backend included) |
| `node --check automotive/static/app.js` | PASS |
| `UV_CACHE_DIR=/tmp/tuz-uv-cache uv lock --check --offline` | PASS: 80 resolved packages, lock consistent |
| `UV_CACHE_DIR=/tmp/tuz-uv-cache uv sync --extra dev --locked --offline --dry-run` | PASS planning only; would install 70 packages; not actual installation |
| `/tmp/tuz-test-env/bin/ruff check .` | FAIL: 38 pre-existing errors confined to unchanged db_ui/__main__.py and tests/test_db_ui.py |
| `pip-audit --disable-pip --no-deps -r requirements.txt --timeout 5 --progress-spinner off` | BLOCKED: PyPI DNS resolution; no clean vulnerability claim |
| Real PostgreSQL migrations/RLS/current data/recovery/freshness integration | BLOCKED: no disposable admin URL/local PostgreSQL binaries |
| Real catalog schema/data/price/stock mapping | BLOCKED: configured connection OperationalError; automotive runtime URL not configured |
| Live Anthropic and model/workspace access | BLOCKED: no ANTHROPIC_API_KEY configured |
| Live vPIC | BLOCKED: request/network unavailable |
| Desktop/mobile initial static rendering | PASS scoped visual review; screenshots retained |
| Automated browser interactions / real E2E | BLOCKED: missing X and sandbox socket/FD restrictions |

Recovered test environment: Python 3.12.12, FastAPI 0.155.0, Anthropic SDK 0.72.0,
HTTPX 0.28.1, Pydantic 2.14.1, Uvicorn 0.54.0; pure psycopg 3.3.2 uses system libpq.
Ruff 0.17.1 and Bandit 1.9.4 were available offline (dev pins are older). Baseline requests
2.32.5 and python-dotenv 1.2.1 match the project. Full locked fresh install remains blocked.

## Remaining acceptance ownership

- Operator: provide a disposable PostgreSQL admin URL and separate runtime/enrichment logins;
  run real migration/RLS/recovery checks, then separately verify the actual catalog.
- Operator/data owner: confirm currency/price/stock fields and obtain licensed fitment, exact
  maintenance and current-product approval evidence; review it through the owner capability.
- Operator: configure Anthropic key/model/workspace and permit live vPIC; check actual provider
  behavior/cost accounting. No credentials should be sent through chat or committed.
- Request owner: recover original nine codes, sample VIN, Georgian queries and unnamed provider
  candidates for exact original acceptance. AP104/2 and synthetic adversarial fixtures are covered.
- Environment owner: enable dependency network/audit and a browser/server environment; rerun
  locked installation, complete E2E and produce visual regression evidence.

No blanket completion or production-readiness claim is made while these gates are blocked.

## Final self-evaluation (ECC rubric)

| Axis | Score | Evidence and specific improvement |
|---|---:|---|
| Accuracy | 4 | Mocked checks and static previews are explicitly scoped; real SQL/provider behavior still needs verification. |
| Completeness | 3 | Implementation exists, but the three new PostgreSQL integration checks, live providers, original acceptance inputs and browser E2E remain blocked; rerun them in the required environment before release. |
| Clarity | 4 | Setup, endpoints and evidence rules are documented; actual supplier-specific scope examples still depend on licensed source material. |
| Actionability | 4 | Commands, independent roles and blocked-gate owners are concrete; usable credentials/data must be supplied outside this repository. |
| Conciseness | 4 | Final handoff is short; this deliberately detailed requirement matrix retains audit evidence. |

Overall: 3.8/5. Highest-impact improvements: execute disposable PostgreSQL acceptance; then validate
actual licensed evidence and catalog mapping; then run live-provider/browser E2E and dependency audit.
Would the user agree? The implementation is reviewable, while completion of unavailable external
acceptance is explicitly withheld. The completeness gap needs external prerequisites, not an
unverified claim of success.

### Follow-up — Python 3.14 / lxml development install failure
- User reproduced `uv sync --extra dev --locked` attempting to compile lxml 5.4.0 and
  failing on missing libxml2/libxslt headers. Inspected `.venv`: Python 3.14.6; the
  installed Python 3.12.12 and locked CPython 3.12 Linux wheels provide a smaller fix.
- Added `.python-version` pin 3.12, matching CI. Updated README and automotive setup
  instructions; dependency pins and lock contents are unchanged.
- Verification: `UV_CACHE_DIR=/tmp/tuz-uv-cache uv sync --extra dev --locked --dry-run`
  selects installed Python 3.12.12, plans replacing the 3.14 project environment and
  resolves all 80 packages. This is a dry-run, not a completed download/install.
- Next user command: `uv sync --python 3.12 --extra dev --locked`; uv recreates `.venv`
  as needed. A completed network-enabled install remains to be verified.

### Follow-up — Missing VIN cache key at startup
- User reports locked install succeeds and 103 tests finish with 95 passed / 8 skipped;
  the remaining skips require PostgreSQL integration configuration.
- Inspected local `.env` presence flags only; automotive VIN key, runtime/enrichment
  URLs, operator token and Anthropic key were absent. Existing catalog URL points to
  loopback and matches the local Compose port; its credentials were never printed.
- Generated only the missing AUTOMOTIVE_VIN_HMAC_KEY directly into gitignored `.env`
  using secrets.token_urlsafe(48); preserved other values and set .env mode 0600.
- Verified real Settings.from_env() + create_app(settings=...) initialize successfully
  using the installed project environment. No server/database/provider health claim.
- Read-only database connection failed OperationalError in this sandbox. Docker listing
  was available, but container-exec/port inspection were denied; no DB role grants or
  migrations were performed. Automotive database credentials and Anthropic key remain
  unconfigured and must be supplied locally before their respective features operate.

### Follow-up — VIN service 503 with missing automotive database (2026-11-09)
- Confirmed inventory/enrichment URLs and Anthropic key remain absent; the generated
  VIN key is present. The VIN service previously read its PostgreSQL cache before
  contacting vPIC, so absent credentials prevented any external decode.
- Changed VIN caching to best effort: read/write outages preserve sanitized live
  results and add an explicit uncached warning. Provider failures remain errors;
  cache failures never fabricate a successful decode or expose connection details.
- Added safe, distinct API error codes for missing inventory configuration and vPIC
  outages; GET /api/v1/status reports configuration flags only, with no secret values
  or provider-health assertion. Georgian UI shows missing inventory/chat setup.
- Added `uv run python -m automotive --setup-local` for an existing migrated loopback
  warehouse. It refuses remote/service/hostaddr targets, pending old migrations, and
  existing automotive URLs; applies only 0006, creates independent restricted logins,
  uses SCRAM verifiers in role SQL, and writes credentials/secrets to private .env.
  Warehouse products are never modified by this helper. Production setup is unchanged.
- Added regression checks for cache read/write failures, real VIN service + API with
  no database (mocked provider), explicit API status/errors, isolated local credentials,
  preserved existing values, pending migration refusal and safe setup errors.
- Added optional real-PostgreSQL setup acceptance covering independent logins, read-only
  runtime privileges and unchanged catalog fingerprints; not exercised here.
- Verification in installed Python 3.12 environment: `.venv/bin/python -m unittest
  discover -s tests -v`: **116 total, 104 passed, 12 skipped**. Four automotive PostgreSQL
  tests, five pre-existing PostgreSQL checks and three loopback checks skipped.
- `.venv/bin/ruff check automotive tests/test_automotive_*.py`: PASS.
- `.venv/bin/bandit -q -r warehouse_sync automotive`: PASS.
- `node --check automotive/static/app.js`: PASS. Actual Settings/create_app initialization PASS.
- Live synthetic vPIC POST still BLOCKED ConnectionError. Attempted the local setup
  command: BLOCKED PostgreSQL connection; no database changes or new .env credentials.
  Docker administration is denied in this environment. Neither actual DB setup nor
  live VIN/browser/provider E2E is claimed verified.
- Operator next action: stop the previous server and restart to load the VIN fix; run
  the local setup command from the user's own terminal for inventory. Configure the
  Anthropic key privately in .env and restart for chat. Do not paste credentials in chat.

Follow-up self-check: implementation and mocked verification cover the reproduced failure;
completeness remains limited by the unavailable live provider/database. The highest-impact
remaining check is a real local setup + VIN request in the user's terminal environment.

### Follow-up — Show inventory for a decoded vehicle (2026-11-09)
- User screenshot demonstrates VIN decoding on their host; the prior UI exposed only
  per-code fitment lookup and did not provide a vehicle-to-inventory view.
- Added POST /api/v1/inventory/vehicle with typed bounded input/output and a Georgian
  vehicle-parts panel that loads after VIN identification or configuration submission.
  Product make/model descriptions and existing evidence links remain visible.
- New deterministic VehicleInventoryService lists current inventory with verified matches
  prioritized, preserving pending/conflicting evidence and explicit negative exclusions.
  OE/shared-code co-occurrence and normalization collisions never verify interchangeability;
  product-linked fingerprints, manufacturer identity, model year and all restrictions apply.
- Literal make/model catalog mentions are labeled UNKNOWN candidates. No car-year/engine
  inference or stock quantity is derived from those labels. Empty results do not mean
  out-of-stock. No fitment records or successful inventory examples were seeded.
- Preserved decoded body/drive configuration and supported those restrictions in API/UI
  and current-context chat reads. Added vehicle_inventory to the existing strict read-tool
  allowlist; no model-selected vehicle override or new write capability.
- Added service/API/chat coverage plus one optional real PostgreSQL regex/exclusion test.
  Full suite: **133 total, 120 passed, 13 skipped**. New-code Ruff, Bandit for both backends,
  and node JS syntax PASS. Real catalog read BLOCKED OperationalError; no actual parts
  or warehouse quantities are claimed verified from this environment.
- Browser QA uses ECC browser-qa: headful browser lacks X; one headless preview launch
  failed sandbox crashpad/socket restrictions. New visual/interaction acceptance remains
  BLOCKED; report: docs/automotive-review/vehicle-inventory-qa.md.
- Customer action: restart the existing server, refresh, identify the VIN again, then use
  the new parts panel. Reliable compatibility still depends on owned/authorized fitment
  evidence; text candidates are an aid to finding existing catalog records only.

### Follow-up — Motorium VIN → part codes (2026-11-09)
- User explicitly requested the reverse-api-engineer plugin/skill for motorium.ge and
  a runnable VIN-to-part-code API. Read that installed SKILL.md and ECC connector skill,
  plus the site's public terms; inspected existing warehouse/vPIC provider conventions.
- reverse-api-engineer 0.14.0 installed; list had no prior Motorium run; dry-run passed
  (auto/Claude/headless). Initial capture failed on read-only global history. Isolated
  per-process history/config under /tmp without changing global settings, started run
  68f6f286ca71; it produced only run_started, no HAR/client/usage result, and was stopped
  with Ctrl+C (exit 130). No capture remains running. SDK cost is unreported/unknown;
  a successful plugin-generated-client claim is explicitly withheld.
- Independently inspected the public JS and replayed its actual protocol: POST
  /api/ai-assistant.php with message/history/context, plus GET product.php?id=...
  JSON-LD for structured MPN/SKU/OEM-labeled values. No account, purchase, cart change,
  human message or access-control bypass. Raw capture/cookies/VIN requests stay private
  under /tmp/tuz-motorium-rae, not repository source or documentation.
- Live public Honda fixture showed decoded Accord vs catalog S2000 mismatch. A context-only
  follow-up also returned generic products without VIN identity. New client withholds codes
  for both situations and repeats VIN in each fresh, anonymous lookup. No arbitrary caller
  history/context/URL is accepted. Strict format, numeric detail IDs, streamed caps,
  timeouts, no redirect/retry, immediate stop on 429/access requirement, sanitized output.
- Implemented automotive/motorium.py, typed POST /api/v1/vehicles/part-codes, status flag,
  disable setting MOTORIUM_ENABLED, and Georgian Motorium code-search panel. Structured
  supplier SKU, MPN and OEM-labeled fields remain separate; model prose is discarded.
  Detail identity mismatches conflict; unavailable OEM details remain unknown. No stock,
  price, fitment verification, oil eligibility or imported approvals derive from the bot.
- UI reuses current VIN, accepts part description and links actual source records; a button
  searches own catalog by MPN/manufacturer. Supplier quantities/prices are not our stock.
- Live acceptance: real Python client and real local API handler (ASGI transport with actual
  outbound HTTP, not mocked Motorium) both returned PROVISIONAL with SATO PN0477W and
  source OEM field D1322-8434 from product 29845. VIN omitted from recorded outputs.
  HTTP status 200; typed API schema validation passed. This is not browser/TCP E2E or
  independent manufacturer fitment validation. Source catalog is not a full vehicle EPC.
- Final regression: 146 total, 133 passed, 13 skipped. New Motorium tests cover observed
  schemas, context/frame mismatches, generic fallback, prose rejection, separate code
  types, details failure, input/body limits, rate/access failures and typed endpoint wiring.
  Ruff (automotive + automotive tests), Bandit (warehouse_sync + automotive), JS syntax,
  and offline lock consistency all PASS. No new dependencies/migrations required.
- Protocol/setup/live-check/plugin-limit documentation: docs/integrations/motorium.md.
  Existing catalog and synchronizer remain unchanged by this connector.

Motorium self-evaluation: accuracy 4 (supplier labels remain provisional); completeness 4
(working protocol/client/API/UI, automated plugin capture incomplete and OEM coverage limited);
clarity 4 (code types and limitations documented); actionability 5 (live client + API verified,
restart and UI instructions available); conciseness 4 (protocol notes retained separately).
Highest-impact remaining check: browser interaction after restarting the user's server, plus
independent supplier/manufacturer validation before treating candidates as confirmed fitment.

### Follow-up — One VIN search across connected sources (2026-11-09, implementation cycle)
- Ownership: lead integration; no new delegation. Applied ECC api-connector-builder and
  fastapi-patterns to the existing vPIC/Motorium/repository service boundaries.
- Implemented automotive/vin_search.py: bounded concurrent read federation for vPIC,
  warehouse make/model discovery, applicable fitment records, maintenance/actual-product
  approvals, Motorium category queries, warehouse identifier matches and directional
  cross-reference evidence. A failed provider preserves successful independent sources.
- Added POST /api/v1/vehicles/search (202) and GET /api/v1/vehicles/search/{id} with
  typed records, per-source progress/errors, snapshot pagination and cache expiry.
  These are ephemeral reads, distinct from durable operator enrichment jobs. Identical
  HMAC-bound searches reuse a five-minute snapshot; raw VIN is absent from stored jobs.
- UI: the main VIN submission now starts federation and polls source progress, renders
  combined provenance, verified/candidate counts, paginated records and maintenance
  evidence. Previous single-source controls remain under advanced details. VIN and
  configuration edits invalidate older responses. Supplier stock is never our stock.
- Conservative merge: unique exact main-code/manufacturer only, retaining punctuation;
  no OEM alias/co-occurrence union. Negative local fitments cannot reappear through
  supplier/code candidates. Conflicting supplier identities remain visible separately.
- Bounds: 75 seconds per running search, two concurrent search workers, 20 cached jobs,
  eight default Motorium category queries with at most six details each, shared ten
  provider lookups/minute, 2,000 local discovery rows, 24 warehouse code lookups,
  50 records per UI page. Truncation remains PARTIAL; coverage_complete is always false.
- First focused/regression cycle: 165 tests total, 152 passed, 13 skipped (PostgreSQL
  and loopback acceptance unavailable). New backend Ruff and JS syntax pass. Added
  synthetic tests for partial failures, fallback, identity conflicts, privacy, limits,
  pagination, all-page local discovery, source code bridge and no reverse interchange.
- Remaining acceptance: final API/UI flow checks, live bounded federation, security
  scan and docs. Browser MCP still BLOCKED: missing X server. Actual local catalog
  and disposable PostgreSQL verification are not claimed from synthetic test results.

### Follow-up — Unified source acceptance cycle (2026-11-09)
- Optimized source bridge to search supplier MPN/OEM values only; local products already
  listed do not cause redundant code scans. All local primary-code/manufacturer identities
  remain available for forward evidence discovery. No automatic cross-reference expansion.
- Default live run: real configured warehouse + Motorium, 199 local name/model candidates,
  three distinct supplier records, 202 combined records; PARTIAL after seven of eight
  category queries, elapsed 73.7 seconds. No confirmed fitment/engine/oil claims.
- Final actual API acceptance: ASGI handlers + actual database + actual Motorium, narrow
  brake-pad query: POST 202, GET 200, 200 unique rows paged completely (199 local + one
  supplier), two supplier-code searches. Warehouse row count and ordered ID/hash digest
  unchanged before/after; all stock values null. Raw VIN absent from JSON. This upgrades
  actual catalog read acceptance from previously BLOCKED, but does not assert browser E2E.
- Full Python regressions: 167 total, 154 passed, 13 skipped. Automotive Ruff, Bandit
  for both backends, JS syntax and new Node synthetic-DOM interaction contract PASS.
  DOM checks verify single-submit progress/pagination, source text safety and stale
  VIN/configuration invalidation; they do not verify layout or real browser behavior.
- Added interrupted-page preservation, repeated supplier identity conflict quarantine,
  late-worker snapshot immutability and explicit remaining query totals/truncation reason.
- Dependency advisory lookup now exercised via available Python CLI path: FAIL, four
  existing vulnerable pins reported (lxml, pytest, python-dotenv, requests). No dependency
  upgrade made in this source-federation change. Exact IDs/fix versions and reproduction
  commands are in docs/automotive-review/unified-vin-qa.md.
- Remaining BLOCKED/unavailable: real browser (missing X), disposable PostgreSQL/loopback
  integration tests (13 skipped), full licensed EPC/fitment coverage. Existing full-repo
  explorer lint failures are unrelated and unchanged. No fabricated evidence added.
- Updated docs/automotive.md, docs/integrations/motorium.md and independent self-review
  record. User operation: restart existing server and refresh; enter VIN and select
  'ყველა წყაროში ძიება'. Leave part description blank for bounded multi-category search.

### Follow-up — Conversational parts assistant completion (2026-11-09)
- Resumed after the Codex session hit its usage limit. Implementation (chat workspace,
  `/api/v1/chat/conversations` endpoints, `automotive/conversational.py`, bounded in-memory
  store) was already in place; review findings (stale identity fields, VIN sanitization,
  follow-up capitalization/axle memory, `progress` snapshot key) verified fixed.
- Checks: 219 Python tests pass (11 skipped: PostgreSQL/loopback), both Node DOM suites,
  `node --check`, Ruff on `automotive/` and new tests, and Bandit on `automotive` pass.
  Repo-wide Ruff still reports pre-existing E501 issues in `db_ui/` and `tests/test_db_ui.py`.
- Live read-only probe (real warehouse DB + Anthropic + Motorium): `AP104/2` returns 1 card;
  VIN + "brakes" asks for pads/discs and axle; front pads return 12 and "rear ones" returns 5
  catalog candidates, all with unknown stock. Motorium reported CONFLICTED for the probe VIN,
  so the supplier-code-to-warehouse bridge is NOT confirmed live; results are PARTIAL.
