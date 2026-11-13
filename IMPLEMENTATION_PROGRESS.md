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

### Follow-up — Fast, accurate vehicle-part lookup (2026-11-09)
- Cause of the empty/`SOME_SOURCES_UNAVAILABLE` replies: broad questions waited on eight sequential
  Motorium calls plus every chooser (73.7 s previously), any non-OK source made the whole answer
  partial, Motorium withheld codes when the assistant omitted a structured part, and exact code hits
  were dropped by the product-name category filter.
- Chat answers a known vehicle plus category (or a VIN with no category) directly from the read job
  before any model call; brakes/oil show candidates together with their clarification.
- Sources are scoped to the requested category, Motorium categories run in a small pool, VIN is
  optional for make/model/year jobs and oil choosers, JS Asakashi filter codes are matched in the
  warehouse, and the engine-oil category excludes sensors, coolers, pumps and gearbox oils.
- Real failures are named per source and localized in the chat UI. Verified live (read-only): front
  brake pads for the public Honda fixture VIN returned 12 catalog candidates in 6.8 s (previously
  one 75 s federation); Motorium reported the known fixture identity conflict by name.
- Tests: 274 total, 263 passed, 11 skipped (PostgreSQL/loopback); Ruff, Bandit and both Node DOM
  suites pass. Restart the server to load the change.

### Follow-up — VIN to requirement to code to stock (2026-11-09)
- Stock mapping confirmed from the source screenshot and the stored columns: `column_10` is
  the store, `column_12` (TUZ 11) arrives in 1-2 hours, `column_11` (TUZ 6) is unused.
  Chat offers only items in the store or TUZ 11 and states the availability.
- Chat offers are derived from the car's requirement (VIN codes, chooser oil grade, engine
  family, reviewed fitment), never from make/model names. New `oil_change` bundle.
- Choosers are matched by trim, fuel and body, not by displacement (vPIC said 3.0 L where
  Shell lists the same engine as 3.5 L). Verified read-only for a 2015 E 400 Coupe: engine
  `M 276.850`, 0W-40 / 5W-40, 6.5 L; in-store Shell Helix Ultra 5W-40 and the MANN M276 oil
  filter were offered.
- Tests: 309 total, 298 passed, 11 skipped (PostgreSQL/loopback); Ruff, Bandit, Node UI tests pass.

### Follow-up — Name fallback and optional catalog API (2026-11-09)
- When no code can be derived for a named part, chat offers in-stock name matches with the stated
  year range checked and a "verify compatibility" label; never for oil, never when a code-derived
  offer exists. Make aliases (MB, VW) and flexible model spelling added to name candidates, which
  are now ordered stocked-first.
- Added `automotive/catalog_api.py`: key-gated VIN to OE/part-number client (RapidAPI "Auto Parts
  Catalog" defaults), a source in the VIN federation, and `--check-catalog-api`. Activated with the
  user's key and verified live on one VIN (2015 E 400 Coupe, exact vehicle found): part groups are
  taken from the vehicle's own category tree and numbers match only with the same brand. Coverage
  across other cars is not measured yet.
- Tests: 335 total, 324 passed, 11 skipped; Ruff, Bandit and Node UI tests pass.

### Follow-up — Faster answers and stricter accuracy (2026-11-09)
- Found by measurement: the code index was empty (0 of 65,805 products), so each code search
  scanned every row (~1 s), and the answer waited for the slowest source (Motorium 5.5 s, Fuchs up
  to 10 s). Migration `0007` adds a stock-independent `content_hash`; the index was built locally
  (49 s, 173,021 codes) and a lookup is now ~60 ms. The worker has `--auto` and a systemd timer.
- Pipeline tiers, shared 24 h caches, parallel catalog calls, prewarm, faster polling and live
  cards. Live read-only timings for the E 400 VIN: oil change 4.6 s (was 10-18 s), front brake
  pads 1.3 s, air filter 2.3 s, cabin filter 0.9 s (warm).
- Hard rules (active-only, axle with `წნ`/`უკ`, part-type conflict, vehicle consistency, code
  safety, 24 h freshness, oil quantity) and the staff-editable `automotive/rules.toml`.
- `--evaluate` batch tool; first run on two VINs showed the axle rule removing rear-axle pads for a
  front request and Motorium's 5 s fallback as the main remaining delay on non-European cars.
- Tests: 386 total, 375 passed, 11 skipped (PostgreSQL/loopback).

### Follow-up — Natural, accurate, fast chat: audit and iterations (2026-11-09)
- Harness first: `automotive/conversation_eval.py`, 46 bilingual multi-turn scenarios in
  `tests/fixtures/conversations/`, `--evaluate-chat mock|live [--judge]`. Hard checks: every number,
  code, brand and name in a reply backed by cards/car/requirements/customer words
  (`automotive/grounding.py`), language, expected act/category/axle/vehicle, no search on small
  talk. Soft checks: short, one question, at most one caveat, not repeated, names the car, says
  how many. Live runs used two private VINs (2003 Honda Accord, 2015 Mercedes E 400), never printed.
- Changes: deterministic `automotive/dialogue.py` (small talk incl. Latin-typed Georgian, typed
  cars, short answers, offers, references, sticky language, code vs year/grade/quantity/model);
  backend-written replies `automotive/replies.py`; direct code/grade/reference turns without the
  model; 8 new categories; messenger UI (bubbles, typing indicator, chips, Enter sends, compact
  cards, 5 then more, no unknown price, fixed language `<option>`); optional fact-checked
  rewording (`AUTOMOTIVE_CHAT_REPHRASE`, off by default — measured 1.2-3 s, rarely better).
- Bugs found and fixed on the way: "thanks" re-ran the search; typed cars were never understood;
  years counted as part codes (forcing model calls); the store passed `[VIN]` so VIN-only messages
  always went to the model; the source coordinator refused all searches after 20 in five minutes
  and the chat then said the car's codes were unknown; the HTTP model dropped the new reply fields;
  VINs with an attached Georgian suffix escaped redaction (all VIN patterns now use ASCII
  boundaries); a failed code lookup said "not in our catalog".
- Found, not fixed (owner decision): the catalog API key is on RapidAPI BASIC (100 requests/month)
  and returned `RATE_LIMITED` (quota exhausted, about 30 days to reset); evaluation runs used part
  of it because its cache is in memory only. Motorium reports a vehicle mismatch for both test
  VINs after 3-7 s. Most live VIN lookups therefore fall back to name matching and take ~5 s.
  No `rules.toml` entries were added: no wrong-part evidence was found, only missing source data.

| Round | Mode | Scenarios passing | Hard checks | Naturalness | Judge (1-5) | Small talk | Code | Lookup (median) |
|---|---|---|---|---|---|---|---|---|
| 0 baseline | mock | 5/46 | 75.8% | 75.0% | — | — | — | — |
| 0 baseline | live | 10/46 | 79.6% | 65.3% | 1.45 | 1,048 ms | 4,508 ms | 4,572 ms |
| 1 | live | 46/46* | 100% | 94.1% | 3.64 | 20 ms | 1,350 ms | 1,778 ms** |
| 2 | live | 45/46 | 99.7% | 95.2% | 3.83 | 20 ms | 132 ms | 5,206 ms |
| 3 (pre-review) | live | 41/46 | 98.6% | 96.7% | 3.82 | 20 ms | 122 ms | 4,996 ms |
| 4 (post-review) | live | 46/46 | 100% | 96.7% | 3.86 | 20 ms | 142 ms | 5,030 ms |
| 5 (repeat) | live | 46/46 | 100% | 96.7% | — | 20 ms | 122 ms | 5,134 ms |
| final | mock | 46/46 | 100% | 100% | — | — | — | — |

\* Live mode relaxes catalog-dependent expectations. \** Round 1 lookups were flattered by the
search-capacity bug (instant failures). Round 3 failures were the checker catching a new note
("during the day" read as a delivery promise); reworded. Routing model calls fell from 65 per run
to 0; the remaining soft misses are long name-matched replies.
- Independent code and security reviews: all HIGH/MEDIUM findings fixed with regression tests
  (rephrase trusting customer-typed codes, failed lookups reported as missing, vanished reference
  parts called sold out, make + part number read as a model, grade questions captured by earlier
  context, Georgian-suffix VIN redaction, checker bypasses, eviction grace period).
- Verification: 448 Python tests (11 skipped: PostgreSQL/loopback), Ruff on `automotive` and the
  touched tests, Bandit (warehouse_sync + automotive) clean, both Node UI suites, live browser
  check with screenshots in `docs/automotive-review/chat-texting-qa.md`. Repo-wide Ruff still
  reports the 39 pre-existing `db_ui` errors.

### Follow-up — Garage 1886 public site, staff login and dashboard (2026-11-10)

- The application now also serves the garage's website: a bilingual landing page at `/` and
  `/en` (services, story, gallery, contact), a scroll-driven 3D engine that comes apart and
  reassembles, a public part search by car, VIN, name, brand or code, the parts assistant as a
  chat widget, an unlinked sign-in at `/login`, and a dashboard at `/admin` with `employee` and
  `admin` roles. Every text, service and gallery photo is edited from the dashboard. Routes,
  roles and the security model: [docs/garage-site.md](docs/garage-site.md).
- Behaviour change: the staff workspace moved from `/` to `/staff`, and it and `/api/v1/*` now
  need a staff session (`AUTOMOTIVE_STAFF_AUTH`, on by default, can be switched off only while
  every allowed host is loopback). `/api/v1/operator/*` answers direct loopback callers only.
- Public data is deliberately narrow: a search result is name, vehicle text, brand and price for
  items available now; chat cards carry an availability label instead of counts. Search and the
  dashboard's inventory figures read an in-memory snapshot of the catalogue, so visitors do not
  query PostgreSQL. Prices come from source column `column_06`; the currency is assumed GEL and
  is a dashboard setting.
- Database: migration `0012_garage_site` (private schema `garage`, row-level security, new
  capability role `garage_site`); `automotive_runtime` has no access to it. One new dependency,
  `jinja2`. New files only under `automotive/site*.py`, `automotive/templates/`,
  `automotive/static/site/` and `automotive/static/admin/`; `api.py`, `config.py`, `__main__.py`
  and `static/app.js` changed by a few lines each.
- Two independent security reviews, the second of the fixes made after the first. Confirmed
  findings were fixed with regression tests: assets served only under their exact names (a second
  spelling of a path used to reach the authoring harness), no database query a visitor can cause
  without limit, sign-in and session look-ups that cannot take the worker threads, no account-wide
  or address-wide sign-in lock, a public origin refused without a trusted proxy, unpublished
  packaged photos no longer served as assets. What was left as it is, is listed under "Security
  model" in [docs/garage-site.md](docs/garage-site.md).
- Verification: 1047 Python tests (52 skipped: PostgreSQL/loopback), the database-gated site tests
  against a disposable database, Ruff on `automotive`, the site tests and `scripts`, Bandit clean,
  three Node suites, and a live review recorded with screenshots in
  [docs/automotive-review/garage-site-qa.md](docs/automotive-review/garage-site-qa.md): headless
  Chrome on the machine's graphics card (Lighthouse mobile 99 on both languages, twelve phone and
  tablet profiles, real touch input, the chat sheet under a simulated keyboard), the system's
  WebKit, which is the engine Safari is built on, and Firefox, both at phone sizes. `pip-audit`
  flags four packages that were already in the lock (`lxml`, `pytest`, `python-dotenv`,
  `requests`); the one dependency added here is clean.
- The chat widget shows the staff assistant's answers unchanged. Checked live it answered
  part-number questions with the part and its price, but answered make-and-model questions with
  "could not confirm an offer"; the widget points to the part search when an answer lists nothing.
- Not done: no deployment, no penetration test, no pass on a physical phone or in Apple's own
  Safari, no native-speaker review of the Georgian interface strings. Contact details, opening
  hours, history milestones and service prices are empty until the owner enters them in the
  dashboard.

### Follow-up — Parts assistant: model-driven engine, answer buttons, new chat screens (2026-11-10)

- Asked for: a chatbot that finds the car from plain text and the exact parts it needs, asks
  the right question when it has to and offers the answers as buttons, and is pleasant to use;
  and, from the logs, the reasons it had not found cars and parts, fixed without deterministic
  rules.
- Log analysis. Both chat logs (498 turns) were read and every failed request was checked
  against the live catalogue (65,812 rows, 8,745 in stock). On the engine that was live, 12 of
  74 requests for a car or a part ended with parts (staff chat 10 of 50, public widget 2 of 24),
  at a median of 10 to 11 s, although the parts were usually there: "Toyota Camry 2015, front
  brake pads" got "could not confirm an offer" while 8 matching rows were in stock. The causes,
  each traced to code: a rule dropped every row found through the catalogue's own text once a
  car was known (18 turns); the search found only rows that spell out the make and model, while
  the catalogue writes `MB W212`, `MMC L200`, `VAG GOLF 7` (27 Mitsubishi rows seen of 3,078);
  parts were recognised by 20 fixed patterns that reach 30% of the catalogue; the model had to
  quote the customer word for word, so "Camry" gave no make; rules in front of the model
  swallowed answers such as "yes", "ორივე" and `W212`, and most questions had no buttons; the
  design depended on part numbers the free sources rarely give for a typed car; matches that
  were out of stock were hidden without a word (9 turns). The common cause is a hand-typed
  catalogue and rules that cover only the spellings their author thought of; replacement rules
  tried on the real rows picked wrong parts too.
- What was built. A new engine in which the model searches the catalogue with words it chooses,
  judges which rows fit and asks with buttons, while code only fetches data and keeps facts
  honest (cards come from database rows, a quoted row text must exist, staff vetoes hold, the
  VIN never reaches the model): `automotive/assistant_catalog.py` (an index over the snapshot
  the site already holds, and a catalogue guide computed from it),
  `automotive/assistant_tools.py` (read tools), `automotive/assistant.py` (the turn and its
  checks), `automotive/assistant_prompt.py` (instructions), `automotive/chat_contract.py` (one
  set of shapes for both chats),
  `automotive/assistant_eval.py` (test conversations, catalogue-wide check, spending ledger) and
  `automotive/chat_review.py` (a daily review of logged misses, with
  `ops/systemd/automotive-chat-review.service` and `.timer`). Small edits wire it in:
  `config.py`, `chat.py`, `conversations.py`, `api.py`, `site.py`, `site_chat.py`. Both chat
  screens were rebuilt around the question and its answers: the public widget
  (`templates/site/partials/chat.html`, `static/site/js/chat.js`, `static/site/css/site.css`,
  `site_strings.py`) and the staff chat (`static/app.js`, `static/index.html`,
  `static/style.css`). Tests: `tests/test_assistant_catalog.py`, `tests/test_assistant.py`,
  `tests/test_assistant_eval.py`, `tests/test_chat_contract.py`, `tests/test_chat_review.py`,
  `tests/site_chat_widget_test.mjs`, the fixtures `tests/fixtures/assistant/conversations.json`
  and `tests/fixtures/chat_ui/turns.json`, and the browser walk
  `tests/browser/assistant_chat_walk.mjs`. The engine, its settings and how to run the checks:
  [docs/automotive.md](docs/automotive.md), "Assistant engine (model-driven)".
- Behaviour change: `AUTOMOTIVE_CHAT_ENGINE=assistant` is the default for both chats. The older
  engines stay selectable (`orchestrated`, `scripted`) for going back, and their tests still
  run. A greeting or a part-number look-up now takes 3 to 5 s instead of answering at once,
  because nothing answers before the model. Web research is no longer part of a live turn.
- Measured before and after. The two samples are not the same: the first row counts requests in
  the logs, the others the 72 test conversations (the logged failures and cases with rows that
  must and must not be offered; 3 need a private VIN and were skipped), run on the real model
  and catalogue.

| | Ended right | Wrong rows offered | Questions without buttons | Median seconds | Cost per turn |
|---|---|---|---|---|---|
| Engine that was live (the logs) | 12 of 74 requests answered with parts | not measured | most | 10 to 11 | not measured |
| New engine, Haiku 5.5 | 52 of 69 conversations | 12 | 0 of 26 | 7.6 | $0.0017 |
| New engine, Sonnet 5.5, two runs | 63 of 69 and 63 of 69 | 0 and 2 | 0 of 26 | 6.7 and 7.6 (7.1 and 8.1 on turns that look for a part) | $0.025 |
| New engine, Sonnet 5.5, thinking off | 61 of 69 | 7 | 0 of 29 | 9.0 | $0.027 |

- Model decision: Sonnet 5.5 at low effort with thinking left at the model's default
  (`AUTOMOTIVE_ASSISTANT_MODEL`, `AUTOMOTIVE_ASSISTANT_EFFORT`, `AUTOMOTIVE_ASSISTANT_THINKING`).
  The rule set beforehand was that Haiku stays only if it finds the same parts with no more
  wrong offers. After the prompt was tuned for both, Haiku passed 52 conversations against 63
  and offered 12 wrong rows against 0 and 2, and it was not faster. Switching Sonnet's thinking
  off cost 7 wrong rows and made turns slower. The price is about 2.5 cents a turn, about 15
  times Haiku's.
- Screens against the live engine (the last step). Both screens were walked in headless Chrome
  against the real engine, model and catalogue: the public widget at 390x844 and 1440x900, the
  English page, and the staff chat, 28 turns in the final walk, median 5.9 s, nothing failed,
  and every check in the page passed (answer buttons under every question, the tapped answer
  kept marked, the car line, name, price, availability and confidence on 45 cards, group
  headings, no sideways overflow, no raw value, no console error). Eight things broke where the
  engine and the screens meet and were fixed, seven of them with a test that fails without the
  fix: both screens opened the conversation
  only with the first message, so the first reply also paid for the engine's preparation (11.2 s
  for the first message after a start, 7.4 s after the fix); the engine's warning about old
  stock figures reached neither screen; the progress line stood still; a staff card said its
  reason twice, showed the raw word `assistant` and did not show which cars the row names; the
  oil selector's figures were not shown to staff. Record and screenshots:
  [docs/automotive-review/assistant-chat-qa.md](docs/automotive-review/assistant-chat-qa.md).
- Screens after that walk, and a final walk. Both screens gained a fourth confidence label
  (`spec`: "შერჩეულია სპეციფიკაციით", "Chosen by specification") and say a group's confidence
  and reason once under its heading when every part of the group shares them; the staff chat
  gives the conversation more room and names the conversation's car in its side panel. The
  final walk ran on the engine as it is now: 35 live turns, median 7.9 s, nothing failed, 68
  cards in 30 groups checked in the page. Five added checks passed: oils arrive as `spec` with
  their reason in one line; "Show more" pressed live keeps the groups and their line; "not
  sure" brings the variants as groups marked "needs checking"; a made-up VIN is masked in the
  bubble and answered with the car and a question; a car typed in a message outranks the staff
  page's form. That walk added $1.29 to the ledger. The record is the same note.
- Catalogue-wide check (`--evaluate-assistant catalogue`). 150 stocked rows were sampled by
  counted strata of make spelling and part name; a second model (Haiku 5.5) wrote what a
  customer who needs each row would type (59 Georgian, 36 Georgian in Latin letters, 30
  Russian, 22 English, some with typos) and answered the engine's questions from the hidden
  row. Found means the engine showed that row, or one sharing two or more numbers, within
  three turns.

| | Found after 1 turn | after 2 | after 3 |
|---|---|---|---|
| Before the fixes, 147 rows | 40% | 63% | 71% |
| After the first fixes, the same rows and requests | 46% | 80% | 86% |
| Fresh rows after the fixes (39, another seed) | 46% | 74% | 90% |
| Fresh rows after the part-name readings (19, another seed) | 42% | 89% | 89% |
| Requests that name a model: before, after, fresh | | | 78%, 92%, 97% |

  What the misses led to, none of it a rule about cars or parts: after "I don't know" the
  engine shows the variants as groups marked "needs checking" and stops asking (26 of the
  42 first misses were still asking after three turns); a search also returns rows filed
  under a word one letter away and rows in stock that carry the numbers of an out-of-stock
  row; the guide lists the brands' number series beside each part name; a model (Opus 5.5)
  read what each of the guide's 175 part names means from the rows that carry it
  (`--read-part-names`, `automotive/assistant_names.py`, $1.15), which took the stabiliser
  rows from 71% to 82% found and the rear-arm bushings from 40% to 80%; and a number of
  three or more digits also finds the code that carries it behind letters (`150` finds
  `J150`), which found the stocked Prado rear pad that "прадо 150" had missed.
- Wrong offers. A stronger model (Opus 5.5, no tools) judged the offered rows against the
  request and a wide set of rows. It called 15 of 121 rows wrong before the fixes and 12 of
  73 after the first ones; of those twelve, five were the very row the request was written
  from (the judge disagreeing with the catalogue's own text, as on a row filed
  `TOYOTA/LEXUS CELICA CX5 2011-2016`), and about four were real mistakes, all shown as
  "needs checking", three of them a stabiliser bushing offered for a link. In the last
  sample, after the part-name readings, 4 of 24 judged rows were wrong, all four shown as
  "needs checking", and none of the rows presented as listed or confirmed. The judged rows
  are in `data/assistant_eval/*-judged.csv` for a parts person; the judge works from memory
  and is not a parts catalogue. The four rows of the last sample: a tie-rod end the
  catalogue files under `MMC PAJERO IO` whose numbers are a Hyundai's, an air filter whose
  row states no year, and two bushings whose "rear" in the name is the rear bushing of a
  front arm. A list made by counting alone,
  `data/assistant_eval/catalogue-rows-to-check-2026-11-10.csv`, names 64 catalogue rows
  (15 in stock) whose numbers are otherwise filed under another make or whose make word is
  misspelt (`Subura Legacy`, `ZUSUKI VITARA`), for the owner to look through; it cannot
  catch a row like the Pajero one, whose other make appears nowhere in the catalogue.
- Engine after the first walk: row handles are refused in anything the customer reads;
  the `spec` level with a required reason; the fixed part of the prompt is kept by the
  provider for an hour (`AUTOMOTIVE_ASSISTANT_CACHE_TTL`); the staff page's form reaches
  the model as `staff_form` and a car typed in a message outranks it; `car_label` is only
  the customer's own car; the five VIN conversations run on stand-in VINs.
- Test conversations at the end. The last complete run of the 72 passed 66, with 4 wrong
  rows, all spark plugs offered as "needs checking" on a number the model recalled. Two of
  its six failures (the Prado rear pads, and the C-Class arm bushing of earlier runs) are
  fixed by the search change and pass when run alone (5 of 5 with three neighbours). The
  run after that change was cut off: the Anthropic account ran out of credit at
  conversation 39 of 72; 38 of the 39 that ran passed, with no wrong row, at a median of
  6.5 s a turn (7.5 s for turns that look for a part, 90th percentile 10.5 s, 3.8 s for
  quick turns). The 10.0 s median reported by the run before it was evaluations running
  side by side: its model requests and tools took 6.9 s at the median.
- Cost of testing: $57.17 in the ledger (`data/assistant_eval/cost_ledger-2026-11-10.jsonl`,
  computed from token counts) of the $60 that was allowed. The provider's console has the
  exact figure. The account's credit ran out during the last run: the API answers "Your
  credit balance is too low", so no chat engine can answer until credit is added. The
  engine now tells this state (`PROVIDER_BILLING`), a key that does not work and a model
  without access apart from a passing fault: the customer is told the assistant is not
  available and is pointed to the part search and the phone instead of "try again", the
  staff chat names the cause under the reply, the log gets
  `assistant_provider_account status=...` at error level, and an evaluation run stops at
  the first such turn without charging for it. Checked against the account as it is: one
  turn in each chat came back `PROVIDER_BILLING` with that reply.
- Verification on 2026-11-10, after the last change: `python -m unittest discover -s tests`
  1353 tests pass, 53 skipped (database and loopback gated);
  `node tests/site_chat_widget_test.mjs` 41 pass; `node tests/automotive_chat_ui_test.cjs`
  and `node tests/automotive_ui_test.cjs` pass; `python3 -I scripts/vendor_site_assets.py
  check` passes; `bandit -q -r warehouse_sync automotive` reports nothing; `ruff check
  automotive tests` reports five long lines in `tests/test_db_ui.py`, which this work did
  not touch, and nothing else. `pip-audit` was not run; no dependency changed.
- Not done, and known weak spots: no deployment, and the server on port 8767 was left
  running the code it started with. No pass on a physical phone with its keyboard, in
  Safari or Firefox, or with a screen reader; no native-speaker review of the Georgian
  wording of the engine's replies and of the new interface lines; no review of the judged
  rows by a parts person. No complete run of the 72 conversations on the final code (see
  above). The engine still shows instead of asking in one kind of case: for the CR-V 2010
  water pump it asked "2.0 or 2.4" in 1 of 6 runs and otherwise showed a pump as "needs
  checking"; the instruction is in the prompt and the model does not follow it reliably at
  low effort. When a row names only the chassis, the heading of a variant is the model's
  reading of the part number and can be wrong: the BMW F10 V8 air-filter pair was once
  headed "6 cylinder" (as "needs checking"). The part-name readings live in `data/`, which
  is not in the repository: each machine that serves the chat has to run
  `--read-part-names` once. Seen in the data and left for the owner: Mobil 1 0W-40 is
  priced 55 GEL as 1 L and as 4 L, and Shell Helix Ultra 5W-40 37 GEL as 4 L and 38 GEL as
  1 L.


### Follow-up — Parts assistant: alternatives, web research, a free test bench and a randomised exam (2026-11-11)

- Asked for: an assistant that answers every question and finds every part, using web search
  and tools over the shop's data, at 98-99% accuracy and still fast; alternatives offered
  (a part listed for E63 that fits the customer's E400); tests that are not deterministic,
  over many cars, VINs and part numbers; rounds until that level. Decided by the owner: the
  chat runs on OpenRouter only; testing costs $0 (the owner's Claude subscription plays the
  bot, the customer and the judge); two scores, never blended (right outcome in >= 98% of
  conversations, <= 1% of shown rows wrong); part turns p50 <= 9 s, p90 <= 15 s; the MANN
  crawl's brand searches moved to the front of its queue.
- Why alternatives failed before: a row filed under another car reached the customer only by
  two shared numbers in the same result or as the in-stock twin of an out-of-stock row; one
  shared OE number linked nothing, and the engine had no source of the numbers that fit a
  car, so the E63 row could never surface for the E400.
- Built. `automotive/assistant_bench.py`: `SubscriptionClient` stands where the provider's
  client stands, so the production turn runs unchanged; each turn is a headless `claude -p`
  session with the engine's own prompt and tools, its tool calls held at a local MCP
  endpoint until the engine has run the real tool; children get a whitelisted environment
  (no API key, no hooks, plugins or memory). `automotive/assistant_exam.py`: a randomised
  exam with truth from data (text, VIN, typed numbers, alternatives, numbers the shop lacks,
  shop questions), a writer that reads the part names first, a simulated customer, a dispute
  judge, Wilson intervals, paired comparison, resume, a progress bar and a log. New read
  tools: `car_numbers` (crawled MANN/NGK/CatCar fitments for the chosen vehicle, and every
  shop row carrying those numbers; `automotive/assistant_sources.py`), `research` (a separate,
  time-boxed web-search request, cached, never a VIN in a query, blocked domains;
  `automotive/assistant_research.py`), `shop_info` (the site content); wiring in
  `automotive/assistant_extras.py`, `assistant_tools.py`, `assistant.py` (one keyword),
  `api.py` (one block), `config.py` (`AUTOMOTIVE_RESEARCH*`). Search: `exclude_terms` and
  `in_stock_only` exposed; rows that match the car words and part without the make word;
  Georgian search words also find the shop's shortenings (`გამათ.` for `გამათბობელი`).
  Prompt: an ALTERNATIVES section, when to ask for numbers, showing every variant when the
  customer does not know the year, side or generation, shop and car questions. `confirmed`
  no longer rests on a lookup that lists any car. `python -m automotive --check-research`
  sends one billed research request (about 1-3 cents, a fixed question with no customer
  data) to check the provider path before research is turned on in the public widget.
- Fidelity of the bench: the 72 scripted conversations passed 67 on the bench and 66 on the
  last paid production run, with the same requests per turn (1.93 and 1.89).
- Rounds (dev set 103, 178 cases; a no-change rerun flips about 6 of 178 cases, so a few
  points are noise): baseline engine on the first dev set 74.1% (alternatives 27%); with the
  new tools 94.6% on the corrected dev set; showing every variant 98.2% with 0.8% wrong rows;
  side exclusion in the search was reverted (the model once excluded the wrong side);
  shortened words kept (98.2%, 0.7% wrong rows).
- Rounds after E, each kept or reverted on the dev set: F (never show rows the customer turned
  down) 95.8%, 2 fixed and 6 broken, reverted; G: (a) with in_stock_only a search still
  reaches the in-stock twins of the out-of-stock rows that name the car (kept), (b) rows that
  name only the model offered as check when no in-stock row names the generation (reverted:
  its rows were the wrong rows), (c) a definition of a variant (reverted: fewer rows shown),
  (e) crawled vehicles that differ by engine are variants (kept, but its premise was an exam
  error, see below; no measured effect). The final engine is A+B+C+E+G(a, e), called G'.
- Exam corrections, applied to every run and reported beside the raw numbers: a crawled
  alternative's truth now holds every number the crawled catalogue lists for that vehicle
  and part (MANN lists HU 6024 z and W 7015 for the Jaguar and Land Rover 2.0 cars, and the
  bot's HU 6024 z had been scored as a miss in four cases); `regrade` applies this to stored
  conversations without asking any model, and it can only raise a score. The dispute judge
  was measured on the 160 rows people marked wrong in the test conversations (88 in today's
  catalogue): it calls 81% of them wrong, the rest plausible, and none of 39 right rows
  wrong, so the wrong-row rate probably undercounts (39 right rows are too few to rule out
  false alarms, and before the regrade it had called the right HU 6024 z wrong once).
- Held-out draws (new random seeds, run once, then read): fresh-1 (engine B+C+D, 195
  headline cases) 96.9%, wrong rows 2.2%; fresh-2 (B+C+E, 387) 96.1% raw, 96.6% regraded,
  wrong rows 2.4%; fresh-3 (G', 389) 95.1% raw, 95.6% regraded, wrong rows 1.7%; fresh-4 (G',
  352; 37 text and VIN cases were lost when Claude Code updated itself during the build)
  94.9% raw, 95.5% regraded (95.1% reweighted to the usual mix of kinds), wrong rows 4.2%.
  On the engine in the tree (fresh-3 and fresh-4): 708 of 741 = 95.5% right outcome
  (93.8-96.8%), 34 of 1,167 = 2.9% wrong rows (1.7% and 4.2% in the two draws; wrong rows
  come in clusters within a case, so an interval over rows would be too narrow); with fresh-2's earlier engine
  pooled in, 1,082 of 1,128 = 95.9% and 48 of 1,754 = 2.7%. Headline cases by kind in
  fresh-2 to fresh-4: typed numbers, numbers the shop lacks and shop questions 100% (fresh-1:
  31 of 32 typed numbers); alternatives 89-97%; text 94-95%; VIN 81-82%. Estimated speed of part turns: p50 6.8 s, p90 7.1-10.2 s, from requests
  x 3.4 s (the median measured on the direct API on 2026-11-10, before the prompt and tools
  grew); through OpenRouter it is not measured yet. The bar (98% and 1%) is not met.
- Why, from rerunning the 30 headline failures of fresh-2 and fresh-3 twice unchanged: 10
  pass at least once (the model's run-to-run variance); of the 20 that always fail, 11 looked
  like the exam's (4 show rows the judge says fit but outside a one-row truth, 5 VIN cases
  whose truth row names only the model for a car of a later year, 2 ask for an oil filter
  with no car at all), but Opus later passed 4 of those 11 (both CR-V bushes, the Honda Fit,
  the Murano VIN) by showing the truth row too, so at most 7 are the exam's; 3 the catalogue's (a hub named front with REAR in its text, `VWA6` written
  as one word, an ABS sensor named as a brake reservoir) and 6 the bot's (a twin filed under
  the make alone, see the next point; no new search after "I need the bearing, not the
  mount"; two variant sets shown incomplete; two search wordings). Wrong rows come from
  variants taken too widely: a related model (Pajero Sport for a Pajero, 2016 Elantra for a
  Lantra), another position of the same part, or a year range misread (`MURANO 2008` for a
  2014 car, CR-V II links for a 2002 changeover year shown as listed).
- Tried after the last exam and reverted: linking an in-stock row filed under the make alone
  to the hidden out-of-stock row for the car (the Civic case). Rerun of all 83 twin cases:
  74 passed before, 71 after; all 5 broken cases searched without in_stock_only, which the
  change does not touch, one of the 2 fixed did use it, and the Civic case still showed only
  the row that names the Civic. The engine in the tree is the one fresh-3 and fresh-4
  measured.
- Effort medium against low, the same 389 fresh-3 cases: 372 and 372 (3 fixed, 3 broken; the
  fixed are among the failures that pass on some reruns), wrong rows 9 of 603 and 10 of 592,
  and 10% slower per turn on the bench. Low stays.
- Opus 5.5 as the bot instead of Sonnet 5.5 (effort low, same engine and prompt), paired on
  166 cases of the earlier draws: of the 30 Sonnet failures it passes 17 (Sonnet: 7 on each
  of two reruns); on the 16 cases where Sonnet showed wrong rows it shows 1 wrong row of 42
  (Sonnet 34 of 74); of 118 Sonnet passes it keeps 117 (2 wrong rows of 193). It needs about
  a third more time per turn on the bench and costs twice as much per token ($4/$20 per
  million against $2/$10). The judge is also Opus, so the disputed rows of the 46 hard cases
  were judged again by Sonnet: Opus bot 6 wrong of 99 (Opus judge: 4), Sonnet bot 25 of 96
  (Opus judge: 44), so the gain does not come from the judge. Opus also passed three cases
  sorted above as the exam's, by showing the truth row too.
  On new held-out draws, though, Opus scores what Sonnet scores: fresh-5 (seed 878982) 366 of
  384 = 95.3% with 3.3% wrong rows, fresh-6 (seed 4930) 371 of 388 = 95.6% with 1.8%; pooled
  737 of 772 = 95.5% (93.8-96.7%) and 34 of 1,305 = 2.6% wrong rows, against Sonnet's 95.5%
  and 2.9% on fresh-3 and fresh-4. Its gain on the subset came from cases picked because
  Sonnet had failed them; on new cases it fails others. Not adopted (it would also need
  `AUTOMOTIVE_ASSISTANT_MODEL=claude-opus-5-5` and an Opus entry in the OpenRouter model map
  of `automotive/model_client.py`, owned by the other session). Paired on the 151 fresh-5
  text and VIN cases that Sonnet also finished before the bench stopped on a usage
  warning it misread as a limit: the same right outcome (139 and 139, 4 flips each way). Wrong rows 15 of 433 against
  Sonnet's 36 of 419, but per case 8 against 11 of 151 cases (7 only Sonnet, 4 only Opus):
  the row gap comes from two Sonnet cases with 8 wrong rows each. The subset's wrong-row gap
  and the judge swap were also on cases picked for Sonnet's errors. So fewer wrong rows with
  Opus are possible but not shown on new draws. Its request shape (thinking, tool choice) has
  never reached the API either: the bench ignores those parameters.
- Research is wired in and called by the model where the catalogue cannot settle a fit (3
  calls in each of fresh-3 and fresh-4); on the bench a first search outlasts the 8 s cut-off
  and fills the cache for the next turn. Through the production path it was checked once at
  19:21 with the owner's approval (`--check-research`): the direct key refused (billing), the
  fallback sent it to OpenRouter, and Anthropic's own search tool answered there (2 searches,
  5 numbers incl. Toyota 04465-06100), in 15.2 s: longer than the 8 s a turn waits, so a
  first search answers on the customer's next message, from the cache. Default: staff only.
- Real speed, measured at 19:22 with the owner's approval (in-process, the production client,
  no chat log): two part turns took 15.8 s (the first, with the prompt written to the cache
  and the direct key's refusal before the fallback) and 7.9 s, with requests of 3.2-8.0 s
  through OpenRouter, slower than the 3.4 s per request the estimates assume. The next six
  turns failed at once with PROVIDER_BILLING: the OpenRouter account had $0.51 of its $25
  left (read-only /api/v1/credits), too little for OpenRouter to take a chat request, and
  the direct Anthropic account is unfunded, so the live chat answers no one until one of
  them is topped up. Spent on these checks: about $0.20 of the $0.50 allowed.
- Round H, vehicle-text readings: Opus read each of the 3,742 vehicle texts that a row in
  stock carries, once ($0 on the subscription; `python -m automotive.assistant_vehicles`,
  `AUTOMOTIVE_VEHICLE_READINGS_PATH`); search results carry them beside the rows as
  `vehicle_readings`. Paired on a random half of a new draw (187 cases, half size chosen by
  the owner with the weekly window at 85%): right outcome 177 against 176 (noise), wrong rows
  4 in 2 cases against 0. Kept.
- A VIN pins the model year, so a VIN case built from a row whose text names no generation
  (by its reading) has no data truth: 5 to 12 of the 13-22 VIN cases of each draw were such.
  Set aside on every draw alike, the right outcome is 95.0-96.6% (Sonnet 96.6% and 96.0% on
  fresh-3 and fresh-4). The builder now makes such rows text cases.
- Rounds I and J and a confirming draw (20:00-20:55, with the owner's approval at 88-92% of
  the weekly window): round I (car_numbers before research for any filter and spark plugs; a
  row in stock with the same same_part letter as the out-of-stock row for the car is always
  offered) scored 190 of 194 = 97.9% on the unseen half of fresh-7 against 185 (95.4%) without
  it, 6 fixed and 1 broken. Round J (search universal products before saying they do not
  exist). The full draw fresh-8 (seed 850980, 385 headline cases) on readings + I + J: 375 =
  97.4% right outcome (95.3-98.6%; 377 = 97.9% with rows the judge says fit), the best full
  draw so far, but wrong rows 27 of 656 = 4.1%, 18 of them in three cases (a Hiace read as a
  Hilux; rear beam bushes and the large rear bush shown as variants of the bush asked for).
  The shop-question grader now takes times in any order and the address in any script (word
  stems in Latin letters). Prepared and tested, not yet run: part-name readings
  (`assistant_vehicles --names`, AUTOMOTIVE_NAME_READINGS_PATH, `name_readings`), aimed at the
  position mix-ups behind most wrong rows; they wait for the weekly window (97% at 20:55) to
  reset at 07:00.
- The owner's E400 / E63 example on real data: MANN lists air filter C 21 020 for the W212 E
  400 (M276); the shop has MANN C21020 in stock filed only under `MB W205 W213 W222`. Asked
  for an air filter for a 2015 E400 W212 (in Georgian), the engine called car_numbers and
  search_catalog in one step and offered MANN C21020 and its same-number twin JS-ASAKASHI
  A0587SET, as check, saying the shop files the part under other models.
- Morning of 2026-11-12 (the owner's scheduled continuation after the weekly reset, about 10%
  of the new window): part-name readings (Opus read the 2,040 names of rows in stock;
  `name_readings` beside results) kept after a paired half draw (fresh-9, 191 cases: right
  outcome 183 against 182, wrong-row cases 7 against 4). The confirming full draw fresh-10
  (seed 975237, 397 headline cases) on the final engine scored 382 = 96.2% right outcome
  (93.9-97.7%) and 22 of 662 = 3.3% wrong rows. With fresh-8 the two newest full draws give
  96.8% and 3.7%: the bar of 98% and 1% is not met. Next, from fresh-10's misses: the same-
  number twin of an out-of-stock row is still left out when the bot lists variants for a
  customer who does not know the year. OpenRouter stayed out of credit overnight ($25.02 used
  of $25): the live chat cannot answer until a top-up.
- After the morning (with the owner's allowance of 20% more of the weekly window, about 12%
  used): round K (twin scan over cut-off results, twins as variant rows, positions by the name
  readings) reverted, no gain; round M (an unsaid position within the part, such as an arm's
  front or rear bush, is an open fact shown as variants) kept, 2 fixed and none broken on 193
  cases; round N (a sold-out reply told once of an in-stock same-part row) reverted, 75
  against 73 of 84 twin cases with more wrong rows. The case writer now gets the part-name
  readings (two fresh-10 "failures" were cases written for a stabiliser bush whose row is a
  beam bush). Final measured level on unseen draws: about 96% right outcome and 3-4% wrong
  rows (fresh-8 97.4% / 4.1%, fresh-10 96.2% / 3.3%, fresh-11 95.3% / 3.3%); the bar is not
  reached, and the last three rounds no longer moved it beyond noise.
- Round P (14:00): the handles of in-stock same-part rows beside every out-of-stock row
  (`in_stock_same_part`) instead of a correction; on 82 paired twin cases 73 against 71 right
  and more wrong-row cases, reverted. The twin cases resist both a rule and a plainer
  presentation; some twins are catalogue cross-number errors. Stopped near the 20% of the week
  the owner allowed (the meter shows nothing below its warning level).
- Scoring, decided by the owner on 2026-11-12: a stocked row that the dispute judge says fits
  now counts as a right outcome too, and every report prints the share from the data truth
  alone beside it (`right()` and `print_summary` in `automotive/assistant_exam.py`). Re-scored
  from the stored results, no model asked: fresh-8 377 of 385 = 97.9% (97.4% from data
  alone), fresh-10 383 of 397 = 96.5% (96.2%), fresh-11 on the fresh-10 engine 370 of 387 =
  95.6% (95.3%; 4 more cases than the paired count above, finished by that arm only); wrong
  rows unchanged (4.1%, 3.3%, 3.3%). The bar is still not met.
- Catalogue fix list for a parts person (2026-11-12; git-ignored, made by
  `data/assistant_eval/catalogue_fix_list.py`, which asks no model):
  `data/assistant_eval/catalogue_fix_list.csv` and `.md` hold 93 rows the last five runs
  showed as fitting and the judge called wrong (with its reason), 608 pairs in which an
  in-stock row shares two or more numbers with a row of another make, 3 names that say front
  where the vehicle text says REAR, 1,628 vehicle texts on in-stock rows that name no
  generation or years (`TOYOTA/LEXUS` alone is on 216 rows), and 3 part names the reader
  could not tell. Each line is a candidate to check, not a verdict.
- Security reviews of the commits (automated, 2026-11-12, five rounds): the research text
  written from web pages (answer, note, quotes) reached the bot as written, so a page could
  plant a link, an address or a phone number for the bot to pass on. Final rule
  (`for_the_bot` in `automotive/assistant_research.py`, applied by the Toolbox): links (also
  a bare `site.example/path`) and e-mail addresses are cut, pages appear by their site only,
  and a finding whose number is not written like a part number, or is written like a link,
  is dropped. In the customers' chat (`strict`), a number that could be a phone number (in
  a finding's number seven digits in a row once its separators are gone; in the text seven
  digits glued to letters of any script, such as `დარეკეთ599123456ზე`, or joined by up to
  two separators of any kind) reaches the bot only as a code the shop's own rows write (exact
  codes, not stem matches) or a number the request asked about; other such findings are
  dropped, the note and the quotes are left out, and any other web text holding such a
  number (one date or a range of two apart) is left out whole.
  Cutting phone-shaped numbers out of the text instead was tried and replayed on the 46
  cached research results: it took build years and engine sizes with it (`Lancer 1.6
  (2000-2013)`, `Tiguan 2012-2016 2.0L TDI`), so staff see the numbers in the web text whole
  (for staff 0 of 150 findings and 0 of 211 vehicle lines change); in the customers' chat,
  with no shop code known, 67 of the 150 findings and 3 of 10 fact answers would be left
  out. Research stays off in the public widget by default. The bench numbers above predate
  this change (research ran in about 3 cases of each full draw).
- Cost: $0 of API spend apart from the two owner-approved checks above (about $0.20); every
  bot, customer, writer and judge request of the exams ran on the Claude subscription.
  Committed on 2026-11-12 (6457d9f, together with the other session's OpenRouter billing
  fallback, and five follow-up commits for the research text); not pushed.
  The server on port 8767 was restarted at 15:37 from a terminal (not by this
  session) and loads the engine at its first chat; it has had none since, so it will run the
  code in the tree. Checks: on a clean copy of the commits 1,509 tests pass (53 PostgreSQL
  tests skipped without the disposable database); ruff and bandit find nothing in the files
  of this work (ruff's old
  findings are in db_ui; bandit's are in automotive/meta_store.py, a new file another session
  was writing at 18:43). Claude Code updated itself at 15:11 during the runs (after the
  fidelity check; fresh-3 straddles it); the bench now waits through such an update. A judge
  request that timed out under load had stopped one run; a failed grading is now logged as a
  harness error and the case runs again on resume. The runs stopped at 18:33 and 19:06 for a
  usage limit were false alarms: Claude Code reports `allowed_warning` when 85% of the
  subscription's seven-day window is used (it resets 2026-11-12 07:00) and the bench read that
  as a refusal; only a refusal counts now. Overage is off, so at 100% every session on the
  account would stop until the reset: bench runs are kept small until then.

### Follow-up — Garage 1886 on Facebook: Messenger bot with an admin switch, comment replies, page stats, ad results (2026-11-11)

- **Asked for:** a Messenger chatbot for the Facebook page Garage, built like TNLU's on
  Meta's Graph API, that an admin can turn on and off. Also TNLU's other features: ads and
  "other stuff", except its MCP server.
- **Decided by the owner, by approving the plan:**
  - hosting on this laptop behind ngrok, with only a small receiver public;
  - comment replies, page stats and posts;
  - read-only results for Garage's own ads, with no ad editing or spending;
  - the bot ships off;
  - a waiver of the release gate's external penetration test for this laptop receiver only.
- **Setup, switches, costs, troubleshooting, rollback and the security model:**
  [docs/garage-messenger.md](docs/garage-messenger.md).

**What was built** (new files except where noted):

- **Receiver.** `automotive/meta_receiver.py` (`python -m automotive.meta_receiver`,
  `127.0.0.1:8771`) is the only public process:
  - it checks each delivery's `X-Hub-Signature-256`;
  - it appends the items to `garage.meta_inbox` with a login that can only insert, then
    sends `NOTIFY`;
  - it serves the privacy and data-deletion pages in Georgian and English
    (`automotive/templates/meta/`).
- **Worker.** `automotive/messenger_worker.py` (`python -m automotive.messenger_worker`, no
  port):
  - it answers bursts after a 5 s quiet window, capped at 45 s;
  - it reads the switches when it takes a burst and again before every Send API call;
  - it handles shop echoes, `#bot` commands from admin chats, daily caps, the late-reply
    apology, the fallback with the shop phone, and a self-pause when the AI account fails;
  - it runs the jobs: subscription self-repair, names, comments, posts, page stats, ads,
    token health and purge.
- **Engine and messages.**
  - `messenger_engine.py` runs the website's parts assistant with the public configuration
    and keeps each chat's state in the database.
  - `messenger_events.py` parses webhook items.
  - `messenger_render.py` turns a turn into Messenger messages and quick replies.
- **Meta connection.**
  - `meta_config.py` reads the settings, apart from `config.Settings`.
  - `meta_graph.py` is the Graph client, with `appsecret_proof`.
  - `meta_crypto.py` seals tokens with AES-256-GCM, using the new dependency `cryptography`.
  - `meta_db.py`, `meta_store.py` and `meta_context.py` hold the data access.
  - `meta_connect.py` connects with a pasted Graph API Explorer token, runs the
    zero-delivery probe and checks tokens.
- **Features.** `meta_comments.py` (one private reply per comment), `meta_page_stats.py`,
  `meta_ads.py` (read-only) and `meta_sync.py`.
- **Setup and dashboard.**
  - `messenger_setup.py` is the setup CLI.
  - The dashboard is `/admin/facebook` with its API `/api/admin/facebook/*`
    (`site_meta_admin.py` and `site_meta_activity.py`, mounted in `site.py`; views
    `static/admin/js/views/facebook*.js`, with the tabs Status & switches, Connect, Chats,
    Comments, Page and Ads).
- **Database.** Migration `0013_meta_messenger` adds 16 tables in schema `garage`, with
  row-level security by role membership and `garage.meta_provision()`. The new roles
  `garage_webhook` and `garage_messenger` are in `ops/sql/provision_automotive_roles.sql`.
- **Operations.** There are systemd user units `ops/systemd/garage-meta-receiver.service`,
  `garage-messenger.service` and `garage-ngrok.service`. The tunnel has `Restart=always`
  and no `BindsTo=`, to avoid TNLU's trap.
- **Small edits** to `.env.example` (the Facebook block), `docs/garage-site.md` and
  `docs/production-runbook.md`.

**How it is verified.** Every test is $0: Meta, OpenRouter and Anthropic are replaced by
fakes (a mocked session, a fake Graph server on loopback, a scripted model), and no
customer is messaged. The build's runs:

| Area | Tests | Result |
|---|---|---|
| Foundations, Graph client, connect, store, setup | 75 | OK |
| Receiver | 42 | OK |
| Events, engine, renderer | 76 | OK |
| Worker | 61 | OK |
| Comments, page stats, ads | 51 | OK |

A later run, without the database, of all 15 Meta, Messenger and dashboard test modules:
365 ran and OK, with 65 skipped (the database tests).

- The database tests ran in throwaway `postgres:16.6-alpine` containers, never the
  developer database. Each one applies the real migrations and acts under `SET ROLE`. They
  show, among other things, that the receiver's role can insert and read nothing.
- Ruff and Bandit are clean on the new modules.
- The unit files pass `systemd-analyze --user verify`. Every sandbox line was tried in
  throwaway user units on this laptop:
  - `MemoryDenyWriteExecute` is left out of the Python units because it makes every new
    thread fail with the uv-managed CPython;
  - IP firewalling is left out because it is unavailable to user units.
- **Still to come:** the lead's integration pass, the adversarial review, a full suite run,
  and every live check.

**Pending on the owner:**

1. The steps in [docs/garage-messenger.md](docs/garage-messenger.md):
   1. create the app GARAGE 1886;
   2. put the App Secret into `.env`;
   3. run `--setup`;
   4. install the units;
   5. publish the app;
   6. connect with the 8 permissions;
   7. the probe;
   8. the receive check;
   9. Auto reply off;
   10. the first test, about $0.03 per answer;
   11. go live.
2. Restarting the app on port 8767 so that it reads the new keys.
3. Confirming the shop facts on the public pages (`SHOP` in `automotive/meta_receiver.py`)
   and in the dashboard's brand settings.
4. Checking the four outbound links on the privacy page, and the deadline it promises for
   deletion requests.
5. Checking the OpenRouter balance.
6. A native speaker's review of the new Georgian texts.
