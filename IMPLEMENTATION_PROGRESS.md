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

