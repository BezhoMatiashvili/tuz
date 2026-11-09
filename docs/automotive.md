# Automotive intelligence MVP

The separate `automotive` package reads the existing
`warehouse.warehouse_products` catalog by text `source_product_id`. It never writes
warehouse records and does not expose the explorer's SQL interface. The catalog,
synchronizer, explorer, and migrations 0001–0005 are preserved.

## Local setup

Use the project's pinned Python 3.12, matching CI and the recorded application tests.
The `.python-version` file selects it automatically. If an existing virtual environment
uses another version, run `uv sync --python 3.12 --extra dev --locked` to recreate it.
Python 3.14 causes the locked `lxml==5.4.0` development dependency to build from source;
Python 3.12 has prebuilt wheels and avoids the missing libxml2/libxslt header error.
Install `uv sync --extra dev --locked`. PostgreSQL and internet access to approved
providers are separate prerequisites. The UI runs at `http://127.0.0.1:8767`:

```bash
uv run python -m automotive --setup-local
uv run python -m automotive
```

`--setup-local` is for an **existing, migrated local warehouse**. It reads the local
admin `DATABASE_URL` from `.env`, accepts only loopback hosts, applies only migration
0006 after confirming earlier migrations are already complete, and creates independent
runtime/enrichment logins. It generates missing VIN/operator secrets and saves them
privately in `.env` (0600), without printing passwords. SQL role creation uses SCRAM
verifiers rather than plaintext passwords. It never copies, imports or updates warehouse
products. Existing automotive database URLs are preserved rather than overwritten.
If older warehouse migrations are pending, complete the existing migration procedure
first; this helper stops instead of changing the warehouse tables.

After setup, restart the application so it reads the new credentials. Stop an existing
server with Ctrl+C before starting it again. The setup command does not create an
Anthropic key: add your own `ANTHROPIC_API_KEY` locally to enable chat.

The existing `compose.yaml` continues to operate the existing services; the automotive
application runs separately. Copy the new settings from `.env.example` into local
configuration; do not put credentials in documentation, browser storage, or source.
Required for VIN operation: internet access to vPIC and an independent random
`AUTOMOTIVE_VIN_HMAC_KEY` of at least 32 bytes. `AUTOMOTIVE_DATABASE_URL` enables the
private cache and inventory features. Generate secrets with a local secret
manager or Python's `secrets.token_urlsafe(32)`. Keep the VIN hash key stable across
restarts; rotating it makes old cache entries unreachable until expiry/cleanup.

`ANTHROPIC_API_KEY` enables chat. `ANTHROPIC_MODEL` defaults to `claude-haiku-5-5`;
actual model/key access must be checked in the target workspace. Set
`ANTHROPIC_WORKSPACE_ID` for an unscoped key; omit it for a key already scoped to the
intended workspace, as described in the official
[authentication documentation](https://platform.claude.com/docs/en/manage-claude/authentication).
The official Python SDK runs only on the server. No catalog subscription is purchased.

`AUTOMOTIVE_OPERATOR_TOKEN` (at least 32 characters) and
`AUTOMOTIVE_ENRICHMENT_DATABASE_URL` enable operator-only imports and indexing.
Missing operator configuration disables those operations. These are independent from
source cookies, the warehouse writer URL and migration credentials. The application
binds to loopback, validates host and origin, disables access logs and proxy headers,
and applies per-process rate limits (60 API requests/minute, 10 provider requests/minute
per client). It is a local application; internet-facing deployment requires the existing
[production runbook](production-runbook.md) and a separate access-control design.

## Migration and database capabilities

No migration is applied on application startup. Back up and test on a disposable
database first. As cluster administrator, execute
[`provision_automotive_roles.sql`](../ops/sql/provision_automotive_roles.sql); it creates
NOLOGIN capability roles. Create separate LOGIN users with credentials distributed
through your secret manager and grant each exactly one capability. Never grant either
LOGIN schema ownership, `BYPASSRLS`, superuser, the sync-writer role, or membership in
the migration-owner role. Use the migration-owner credential with the existing runner:

```bash
uv run python -m warehouse_sync --migrate
```

Migration `0006_automotive` adds a private schema with RLS on every table and revokes
public access. Runtime can read catalog/evidence and write only the VIN cache.
Enrichment can index and persist jobs/imports; imported evidence is constrained to
pending review and enrichment cannot update evidence to approve itself. The owner
reviews through the separate administration channel. If capability roles are created
after migration, rerun the provisioning script to apply grants. Keep `automotive` out
of Data API exposed schemas. Existing sync-writer privileges are unchanged.

The paired down script removes **only automotive data**. It is destructive to imports,
indexes, jobs and cached identifications. Existing production rollback policy prefers
a new forward migration or tested backup restore; do not apply the down script casually.

## API surface

All endpoints are under `/api/v1`; typed input rejects extra context fields. Part
identifiers are query parameters so `AP104/2` is preserved. No endpoint accepts SQL,
provider URLs, filesystem paths or model-selected write commands.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Process availability (does not assert database/provider health) |
| GET | `/status` | Configuration flags only; contains no secrets or live-provider health claim |
| GET | `/inventory/search?code=...&manufacturer=...&limit=30` | Current inventory code candidates |
| POST | `/inventory/vehicle` | `{vehicle: {...}, limit?: 30}`; vehicle-related catalog rows and evidence-based matches |
| GET | `/inventory/products/{source_product_id}` | Current inventory product and fingerprint |
| POST | `/vehicles/part-codes` | VIN + optional part description through public Motorium assistant/detail metadata; see [protocol notes](integrations/motorium.md) |
| POST | `/vehicles/search` | `{vin, model_year?, part_query?, configuration?}`; starts a bounded read federation, returns 202 and snapshot |
| GET | `/vehicles/search/{search_id}?offset=0&limit=50` | Source progress, combined parts, maintenance evidence and pagination; expires five minutes after completion |
| POST | `/vehicles/identify` | `{vin, model_year?}` through vPIC; no raw VIN returned |
| GET | `/parts/fitments?code=...&make=...&model=...&model_year=...&engine_code=...&market=...` | Applicable fitment evidence |
| GET | `/parts/cross-references?code=...` | Outgoing directional code evidence |
| POST | `/recommendations/oil` | `{vehicle: {...}}`; deterministic eligibility |
| GET | `/evidence/{evidence_id}` | Source, permission, restrictions and review metadata |
| POST | `/chat` | `{message, vehicle?, language: "ka" or "en"}` |
| POST | `/chat/conversations` | Create an ephemeral conversation; returns 201 and an opaque `conversation_id` |
| POST | `/chat/conversations/{id}/messages` | `{message, vin?, vehicle?, language}`; returns 202 with a `turn_id` |
| GET | `/chat/conversations/{id}/turns/{turn_id}?offset=0&limit=20` | Progress, answer, authoritative catalog cards, separate supplier-only codes and warnings |
| DELETE | `/chat/conversations/{id}` | Clear context and invalidate pending replies; returns 204 |
| POST | `/operator/evidence/import` | Bearer token; `{format: "csv" or "json", content: "..."}` |
| POST | `/operator/enrichment/jobs` | Bearer token; `{kind: "index"}`; persisted before 202 |
| GET | `/operator/enrichment/jobs/{job_id}` | Bearer token; durable status/checkpoint |

## Finding inventory for the selected car

Enter a VIN and press **ყველა წყაროში ძიება**. This starts one read job across
all **currently connected** sources: vPIC identification, warehouse discovery,
applicable fitment evidence, maintenance specifications/product approvals, Motorium
part-code candidates, code matching against our warehouse, and directional
cross-reference evidence. The **VIN-ით ნაპოვნი ნაწილები · ყველა ჩართული წყარო** panel
shows each source's progress, failures, counts and combined paginated results.
VIN is transmitted to vPIC and Motorium; it is absent from job responses and URLs.

Leave the optional part description blank for eight bounded category queries
(general parts, oil/air/cabin filters, brake pads/discs, shock absorbers and suspension),
or enter a specific part to make one query. This is a discovery workflow, **not an
exhaustive factory EPC or every internet supplier**. Unconnected paid/manufacturer
catalogs remain external dependencies. Every snapshot has `coverage_complete: false`.
`COMPLETE` means the bounded reads finished; it never means every part was discovered.
A failed/truncated source produces `PARTIAL` while preserving other sources' records.

The job runs for at most 75 seconds, with a separate source deadline reserving time for
local code matching. The app runs two read jobs concurrently and retains at most 20
jobs in process memory. Identical VIN/query/year/configuration inputs reuse a keyed
HMAC snapshot for five minutes after completion. No raw VIN is stored in that snapshot.
These ephemeral jobs disappear on restart and are **distinct from durable operator
enrichment jobs**. Poll every two seconds; GET pages accept up to 100 records, UI pages
use 50. Finished pages are stable. An expired/unknown job returns `SEARCH_EXPIRED` (404);
capacity returns `SEARCH_CAPACITY` (429). Use one server worker for this local workflow;
multiple Uvicorn workers do not share jobs or rate budgets.

Local discovery reads successive pages, up to 2,000 rows. Supplier queries have at most
six product detail reads each and share a ten-lookup/minute budget with direct Motorium
lookups. Up to 24 distinct supplier MPN/OEM queries are searched in the warehouse;
code collisions remain candidates. SQL statements in federation use bounded timeouts.
Rows read before a later page/provider failure remain visible. Source query totals,
`PARTIAL` and warnings expose the limits; these do not imply out-of-stock.

Only an unambiguous exact main code plus manufacturer can group warehouse and supplier
provenance. Punctuation and slashes are retained for this merge; OEM field co-occurrence
never merges distinct products. Supplier disagreements remain conflicted. A supplier
assertion never upgrades local fitment verification. Stock/price mappings remain unknown.
Supplier MPN/OEM matches can find warehouse records even without a make/model description;
those rows are `CODE_CANDIDATE`, `UNKNOWN`, with compatibility unresolved. Applicable
outgoing cross-reference evidence is displayed separately; no inverse traversal or
automatic substitution is performed. Verified oil eligibility is shown in the same
search only when its existing independent specification/approval checks pass.

Changing VIN or configuration clears old results and rejects late responses. Submitting
configuration details reruns the search; a make/model/year that contradicts decoded VIN
identity is ignored with a warning. Added engine details are user context, not decoder
verification. Decoded body/drive fields remain available. Restart the server and refresh
after updating code. The previous warehouse-only and Motorium-only controls remain
under **ცალკეული წყაროს შემოწმება** for focused checks. Manual configuration without a VIN
can still use the warehouse-only view.

The list contains current warehouse catalog products, with a separate fitment status:

- **VERIFIED**: reviewed, applicable fitment evidence identifies the actual current
  product through its fingerprint, or unambiguous main part code and manufacturer,
  and includes the vehicle's model year/range. Unresolved engine/body/drive restrictions
  never match silently. Positive evidence for an OE code merely mentioned in another
  product does not verify interchangeability.
- **PROVISIONAL/CONFLICTED**: source assertions need review, disagree, or their link to
  the actual warehouse item/year is insufficient. Evidence remains visible.
- **UNKNOWN** catalog candidates: the product name/model literally mentions the car's
  make and model, or supplier code matching found an inventory association. This does not establish its generation, year, engine or fitment.

Explicit negative actual-product fitment is excluded and cannot reappear as a name
candidate. Stale fingerprint-bound associations cannot confirm the changed product.
No fitment evidence or mappings are fabricated. An empty list means no supported
match was found, not that the warehouse has zero stock. The standalone `/inventory/vehicle` response is bounded
to 30 rows (maximum 100); counts describe returned rows and `has_more` signals more
records. Confirmed matches sort first. Quantity, price and currency remain unknown
until the source mappings are independently established.

If your catalog has no make/model labels or applicable fitment records, search a known
part number using the existing code search and import owned/authorized fitment evidence
for a reliable vehicle-wide list. Chat can also use the allowlisted `vehicle_inventory`
read tool; it always uses the current selected configuration and preserves uncertainty.

Requests are bounded at the API boundary. Errors do not echo VINs, credentials,
provider exception details or invalid input. External content is rendered with
`textContent`, never as HTML. Evidence source URLs are citations, never fetched by the server. The Motorium connector
fetches only its fixed, validated numeric product-detail URL pattern.

## Identifiers, inventory and indexing

Original values, source field, manufacturer and combined raw string remain visible.
Normalization ignores case/display punctuation but retains `/`. Commas, semicolons,
pipes, parentheses and spaced slashes separate combined codes; compact identifiers
such as `AP104/2` remain intact. Numeric spaced OE groups remain a single identifier.
This is a conservative parser, not a provider catalog or a universal interchange rule.
Normalization collisions return candidates and warnings; co-occurrence creates no
compatibility or interchange claim.

Search joins indexed matches only when both source fingerprint and parser version
match the current warehouse record. Changed/unindexed rows are parsed directly using
a server cursor. An obsolete association cannot return an old code after a catalog
change. Large catalogs should be indexed before interactive use because the fallback
scan may be expensive. Price, currency and stock remain null, with the catalog
observation timestamp. `is_active` is never treated as stock.

Queue an index job using the authenticated API, then run:

```bash
uv run python -m automotive.worker --once
# or continuously, polling every 5 seconds:
uv run python -m automotive.worker
```

The worker uses only the enrichment URL. Each batch atomically commits parsed
associations, fingerprints and its checkpoint. Leases/`SKIP LOCKED` coordinate workers;
expired leases resume from the committed checkpoint. Identical index associations are
deduplicated. Failed jobs retry up to five claims, retain checkpoints and expose safe
error codes. Expired fifth-attempt leases become failed with `retry_exhausted`; after five failed
claims an operator must investigate before requeuing.
A completed job has succeeded status; newly changed or inserted inventory is covered
by direct parsing until the next index job.

## Authorized evidence import and independent review

Only import evidence you own or have explicit permission to reuse. JSON accepts an
array of records; CSV uses header columns with JSON strings in `payload`,
`applicability`, and `restrictions`. The importer is bounded to 1,000 records and
2 MB, with a smaller HTTP body cap of 1.1 MB. Fields are:

| Field | Meaning |
|---|---|
| `kind` | `fitment`, `cross_reference`, `service_specification`, `product_approval` |
| `source_name`, `source_url` | Source identification; HTTP(S) URLs only, no credentials |
| `authority` | Assertion authority, separately from reuse permission |
| `reuse_permission` | `owned` or `authorized`; a label is the operator's attestation |
| `applicability` | Exact vehicle constraints or model-year bounds |
| `restrictions` | Additional applicability limits; unresolved keys block applicability |
| `payload` | Kind-specific technical claim |

Fitment payload uses `part_code` and an explicit boolean `compatible`.
Cross-reference payload uses `from_code`, `to_code`, `relationship`, and explicit
`compatible` if interchange is claimed. The direction is never automatically inverted.
Superseded/revoked records remain visible and cannot authorize recommendations.
`superseded_by` and `revoked` are preserved in payloads. For constrained cross-references,
no unconditional interchange is asserted.

Engine-oil specifications use `category: "engine_oil"`, `required_approvals` (an array)
or `approval`/`specification`, and configuration-specific applicability binding
make/model/engine code and model year (or year range). If a viscosity requirement is
provided, it must match verified product approval evidence. `viscosity`/`viscosities`
accept an explicit SAE multigrade string such as `5W-30` or a list of alternatives;
unsupported prose, conflicting fields or unsuitable asserted grades decline eligibility. Other unresolved restrictions must be reviewed rather than guessed.
Product approval payloads use `product_id`, `approval`, and `source_fingerprint` copied
from the **current** inventory response. A changed product invalidates its old approval.

Every import enters `pending`, regardless of supplied `VERIFIED`, `review_status` or
`reviewed_by` labels. Source authority, reuse permission, applicability and review are
separate. A trusted owner must inspect the original source and licence, resolve scope,
and update the evidence metadata through the administration channel. For a specific
reviewed evidence ID, the conceptual operation is a parameterized UPDATE setting
`review_status='approved'`, the reviewer's identifier, and `reviewed_at=now()`; perform
it only with the separate owner credential after the actual review. There is no
customer or enrichment endpoint that can approve evidence. No successful technical
evidence is seeded by this application.

Compatibility statuses are `VERIFIED`, `PROVISIONAL`, `CONFLICTED`, and `UNKNOWN`.
A verified negative claim remains negative; status describes the evidence decision,
not a promise that the part fits. Disagreements remain visible. Oil eligibility requires
an applicable verified specification and current fingerprint-bound verified approvals
for the actual inventory product. Missing evidence returns `INSUFFICIENT_DATA` with
clarification; conflicting requirements return `CONFLICTED`. Capacity and filter
references remain unknown in this MVP, even when other oil evidence exists.

## VIN privacy and provider limits

vPIC receives VIN and optional year through a fixed HTTPS POST batch endpoint.
17-character format excludes I/O/Q. The check-digit calculation is reported with its
market context; a mismatch is a warning, not a universal rejection. Provider error
codes/warnings are retained. EngineModel is descriptive and never becomes a verified
engine code; missing details and maintenance requirements remain unknown.

Cache keys use keyed SHA-256 HMAC over provider version, VIN and optional year. Cached
payloads allowlist decoded fields, redact embedded VINs and omit echoed/suggested VINs.
Responses expire for use after 30 days. Expired rows remain private database data until
operator cleanup; expired responses are not returned. No raw VIN is stored. HTTP
redirects are rejected, responses have a streamed byte cap and requests have timeouts.
If the cache is missing or unavailable, live VIN decoding still runs and returns the
sanitized result with a warning that it was not cached. Cache failures never expose
database details. A provider failure returns the separate `VIN_PROVIDER_UNAVAILABLE`
code; an inventory request without credentials returns `DATABASE_NOT_CONFIGURED`.
The interface shows missing inventory/chat configuration explicitly.

## Claude grounding, privacy and cost bounds

Chat is the main workspace. Ask in Georgian or English, provide a VIN or exact part
code, and continue with details such as “rear ones.” The composer detects the language
or accepts an explicit language choice. Ctrl/Cmd+Enter sends; **New chat** clears the
conversation and selected vehicle. Direct VIN, inventory, evidence and oil controls
remain available in an expandable section.

Conversation IDs stay only in page memory. Refreshing creates a new conversation;
the abandoned conversation expires automatically. Each process holds at most 20
conversations, expires them after 30 idle minutes, and keeps 12 recent exchanges.
One turn runs per conversation; a changed vehicle invalidates earlier turns and code
associations. No chat history is written to the database or browser storage. Raw VIN
is held transiently in process memory for provider follow-ups, and is excluded from
model requests, history, logs, response payloads and URLs. Restarting loses conversations;
run one application process for local use. These capability IDs do not replace staff
authentication for public deployment.

Message submission returns immediately. The browser polls every two seconds; a turn
has a 180-second deadline, and successful source results survive other source failures.
The response includes `status` (`RUNNING`, `COMPLETE`, `PARTIAL`, `FAILED`), `answer`,
`vehicle`, `progress`, `warnings`, `results` summaries, `cards`, `supplier_only`,
`total`, `offset`, and `next_offset`. Expired IDs return 404; an active turn returns
409 on another submission; bounded capacity returns 429. No migration is needed.

Product name/category discovery uses parameterized queries with Georgian/English
synonyms, “breaks” for brakes, and front/rear distinctions. Category filters apply
before warehouse pagination, including VIN searches. Engine oil is distinct from oil
filters. Missing brake subtype/axle or engine/oil evidence produces a clarification.
Unverified oil candidates carry no suitability claim. Catalog cards say **Found in
our catalog; stock unconfirmed.** Supplier-only identifiers are separately labeled
**No warehouse match.** Prices and stock remain unknown.

Strict schemas are sent as described in Anthropic's
[strict tool-use documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use).
Local validation additionally checks allowed tool names, extra properties, current
question identifiers and current-turn result references. Tools are automotive reads
only: inventory, fitments, outgoing cross-references, current vehicle, and deterministic
oil eligibility. The legacy endpoint uses `present_results`; conversations use
`finish_response` with validated current-turn references and a conversational act.
The backend renders localized replies and technical facts, keeping model-generated
claims out of prices, stock, codes and compatibility decisions. Every tool use receives
an immediate matching `tool_result`, including `is_error` for failed reads, following
Anthropic's [client-tool lifecycle](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
Full VINs in chat are decoded by the backend and redacted before model requests. VINs
and configured secrets are also redacted in context and serialized read results.
Catalog/provider text is untrusted data, with no authority to grant tools or change policy.

Each turn uses current vehicle context, at most four model requests, at most eight tool
executions (including an initial VIN lookup), and 1,024 output tokens per model request.
Automatic SDK retries are disabled. Usage counts input/output/cache tokens and call
counts; structured usage logs contain no prompt, VIN, credentials or provider errors.
These counts support billing reconciliation; they are not a monetary estimate or a
durable billing ledger. Inference is metered. Model refusals, unavailable credentials,
authentication failures, timeouts, rate limits and exhausted budgets are explicit statuses.

The model selects conversational acts and known backend result IDs. Georgian/English
templates and validated backend records supply
technical facts, prices, stock, evidence and uncertainty. Invalid/unreferenced output
returns an ungrounded-response status. The UI clears dependent results when the vehicle
or code changes and suppresses stale in-flight responses. No conversation history or
raw VIN is kept in browser storage.

## Data sources, permission and coverage

The original sources and the subsequently requested public Motorium connector are
listed below. Motorium is an observed web protocol, not an official supported API.

| Source | Current use and coverage | Permission/licensing boundary |
|---|---|---|
| Existing warehouse catalog | Code associations, manufacturer, product identity and observation time; no confirmed price/stock mapping | Operator-selected existing catalog; original access/reuse controls still apply |
| [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/) | Initial vehicle decoding from manufacturer VIN submissions; no certified fitment, oil specification or product approval | Public service with automated rate control; follow published terms and validate regional coverage; no paid subscription |
| Owned/authorized supplier evidence | Operator-imported fitments, directional references, maintenance specs and actual product approvals | Operator must possess explicit reuse rights; provenance labels alone are not proof of licence or truth |
| [Motorium](https://motorium.ge/) | Public assistant and product JSON-LD return supplier MPN/OEM-labeled candidates for VIN + part query | Public read access observed; reuse rights are not independently recorded. No automatic evidence approval or paid subscription. See [protocol and limitations](integrations/motorium.md). |
| Anthropic API | Language/tool orchestration; supplies no technical evidence | Metered API under the operator's Anthropic account/terms; send only sanitized minimum context |

Other named supplier candidates, the original nine codes, original sample VIN, and full
Georgian acceptance queries were not included in the supplied plan text. Their
licensing/coverage and exact acceptance checks remain blocked until those inputs exist.

## Verification and remaining integration gates

See [`IMPLEMENTATION_PROGRESS.md`](../IMPLEMENTATION_PROGRESS.md) for commands and actual
results. Synthetic/mocked acceptance tests are intentionally separate from real database,
live-provider and browser checks. To run with a disposable database, set
`WAREHOUSE_TEST_ADMIN_DATABASE_URL`; the automotive tests create/drop a unique temporary
database, requiring disposable admin privileges. Do not point this variable at production.

```bash
uv run python -m unittest discover -s tests -v
uv run ruff check automotive tests/test_automotive_*.py
uv run bandit -q -r warehouse_sync automotive
uv run pip-audit
```

The implementation session could not connect to the actual catalog, had no Anthropic
key/disposable database URL, and could not reach live vPIC. Database migration/RLS/job
recovery and actual catalog/live inference/browser API E2E remain release gates.
A successful mocked test does not verify a provider or production deployment.

## Motorium VIN part codes

Under the advanced **ცალკეული წყაროს შემოწმება** controls, the
**VIN-ით ნაწილის კოდები · Motorium** panel uses the same VIN field and an optional
part description. It needs neither our Anthropic key nor local inventory access. Each
result includes actual catalog MPN, supplier SKU, OEM-labeled detail values and source
link. The button on a code result searches our own catalog by MPN/manufacturer; supplier
stock/price is never used as our own. VIN/catalog identity conflicts block code results.
The public supplier catalog is not a complete OEM EPC and results remain provisional.
See [the connector protocol, live checks and plugin execution record](integrations/motorium.md).

## Unified-search verification

Run `uv run python -m unittest discover -s tests -v`,
`uv run ruff check automotive tests/test_automotive_*.py`,
`uv run bandit -q -r warehouse_sync automotive`, and
`node tests/automotive_ui_test.cjs`. The Node test uses a synthetic DOM and mocked
responses; it does not assert browser layout or real network behavior. Current
acceptance and environment limits are recorded in
[the federation QA report](automotive-review/unified-vin-qa.md).
