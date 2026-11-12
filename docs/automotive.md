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
admin `DATABASE_URL` from `.env`, accepts only loopback hosts, applies only the
automotive migrations (through 0008) after confirming earlier migrations are already
complete, and creates independent runtime, enrichment and knowledge-validator logins.
It generates missing VIN/operator secrets and saves them
privately in `.env` (0600), without printing passwords. SQL role creation uses SCRAM
verifiers rather than plaintext passwords. It never copies, imports or updates warehouse
products. Existing automotive database URLs are preserved rather than overwritten: when
runtime and enrichment URLs already exist, a rerun only adds the missing
`AUTOMOTIVE_VALIDATOR_DATABASE_URL`.
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
actual model/key access must be checked in the target workspace. The default chat engine
has a model setting of its own, `AUTOMOTIVE_ASSISTANT_MODEL` (`claude-sonnet-5-5`); see
"Assistant engine (model-driven)" below. Set
`ANTHROPIC_WORKSPACE_ID` for an unscoped key; omit it for a key already scoped to the
intended workspace, as described in the official
[authentication documentation](https://platform.claude.com/docs/en/manage-claude/authentication).
The official Python SDK runs only on the server. No catalog subscription is purchased.

Optionally set `OPENROUTER_API_KEY` in the server's `.env` to keep chat working when
the direct Anthropic account cannot pay: its credit balance is too low (400), a spend
limit set on the organization or a workspace is reached (400, "You have reached your
specified ... API usage limits"), the usage tier's monthly spend cap is reached (429 with
`error.details.error_code` `enforced_spend_limit_reached`), or there is a billing problem
(402). Every chat client path (the shared assistant client, the per-turn clients of the
older engines and their `with_options` copies) then sends the same request through
OpenRouter's Anthropic Messages endpoint, with the same prompts, history, tools, cache
controls, thinking and effort. The model stays the same: `claude-sonnet-5-5` maps to
`anthropic/claude-sonnet-5.5`, and `claude-haiku-5-5` to `anthropic/claude-haiku-5.5`, at
the same per-token prices. Unknown model IDs never fall back to a different model.
Routing is restricted to Anthropic's own endpoint, without OpenRouter fallbacks. See
[OpenRouter's Anthropic integration](https://openrouter.ai/docs/cookbook/coding-agents/claude-code-integration)
and [provider routing](https://openrouter.ai/docs/guides/routing/provider-selection).
OpenRouter's `require_parameters` is deliberately not sent: measured on 2026-11-11, its own
parameter check answers the engine's opening request (a forced `tool_choice`, which Claude
Sonnet 5.5 refuses) with a 404 that names nothing to drop, while without it Anthropic
answers with its own 400 naming `tool_choice`, so the turn asks again unforced exactly as
on the direct API. The same probes showed OpenRouter passing `output_config.effort`,
`thinking`, strict tools and 1-hour cache lifetimes through to Anthropic, with cache
writes and reads billed as on the direct API. The client absorbs three differences:
OpenRouter reads `max_tokens: 0` (the engine's cache warm-up) as unset and writes a whole
reply, so the backup is asked for one token instead; it sends nothing, headers included,
until the model's first event, which took 1.3 to 7.3 seconds with the chat's cached prompt
where the direct API answers within about 2, so a backup request may stay silent up to 15
seconds (never beyond the request's own time; a 30-second turn still has time to ask once
more) where a turn allows the direct API 10; and
it refuses `thinking: disabled` in its own words ("Reasoning is mandatory"), which the
engine reads as Anthropic's refusal and answers with `between_tools`. OpenRouter also
returns thinking summaries where Anthropic omits them by default; the engine never shows
thinking text and passes the blocks back unchanged. OpenRouter uses
its own account balance. Ordinary request errors, authorization failures, rate limits and
outages keep their existing handling. The shared assistant client (and every copy of it)
skips an unfunded direct account and asks it again after 1 minute; while it keeps
refusing, each wait doubles up to 10 minutes, so a top-up is in use within 10 minutes and
needs no restart. The older engines build a client for each turn, so each of their turns
asks the direct account once first. A rejected stream can
switch before it opens; a stream that has already started is never replayed. Thinking
blocks and prompt caches belong to the account that made them: when a turn moves to the
other account, the API drops that turn's earlier thinking and the request succeeds, and
the first request there reads the fixed prompt (about 33,000 tokens) afresh. While the
backup is in use, conversations pass through OpenRouter as well as Anthropic. The switch
is logged as `chat_billing_fallback` (a warning the first time, then at info level for
each recheck) and the return as `chat_billing_restored`, with the status code and model
only. Restart the application after first adding or changing either key. Both keys
remain server-side and are redacted from conversation payloads, stored conversations and
logs; the direct client never sends a bearer token from the environment. Other code that
must reach OpenRouter uses `automotive.model_client.openrouter_client(key)`, which applies
the same credential isolation. When neither account can pay, a turn ends as
`PROVIDER_BILLING` (no "try again", and the `assistant_provider_account` error in the
log), as it does when no backup is configured.

`AUTOMOTIVE_OPERATOR_TOKEN` (at least 32 characters) and
`AUTOMOTIVE_ENRICHMENT_DATABASE_URL` enable operator-only imports and indexing.
Missing operator configuration disables those operations. These are independent from
source cookies, the warehouse writer URL and migration credentials. The application
binds to loopback, validates host and origin, disables access logs and proxy headers,
and applies per-process rate limits (60 API requests/minute, 10 provider requests/minute
per client). It is a local application; internet-facing deployment requires the existing
[production runbook](production-runbook.md) and a separate access-control design.

That access-control design now exists: the same process also serves the garage's public
site, a staff login and a dashboard, and this workspace moved from `/` to `/staff` behind
that login together with `/api/v1/*`. See [garage-site.md](garage-site.md) for the routes,
roles, security model and what is still required before it is reachable from the internet.

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

Migration `0008_automotive_knowledge` adds the private `automotive_knowledge` schema
described in [Learned knowledge base](#learned-knowledge-base). It creates no grants for
the enrichment role. Runtime may read active accepted facts and enqueue candidates only
through `automotive_knowledge.enqueue`; the separate `automotive_validator` capability
claims jobs and publishes results. Rerunning the provisioning script reapplies exactly
these grants.

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
part-code candidates, Fuchs, Shell and Pakelo oil choices, JS Asakashi filters,
code matching against our warehouse, and directional
cross-reference evidence. The **VIN-ით ნაპოვნი ნაწილები · ყველა ჩართული წყარო** panel
shows each source's progress, failures, counts and combined paginated results.
VIN is transmitted to vPIC, Motorium, JS Asakashi and, only if configured, the catalog API. Fuchs, Shell and Pakelo receive the decoded make, model and year. Raw VIN is absent from job responses and URLs.

Leave the optional part description blank for eight bounded category queries
(general parts, oil/air/cabin filters, brake pads/discs, shock absorbers and suspension),
or enter a specific part to make one query. This is a discovery workflow, **not an
exhaustive factory EPC or every internet supplier**. Unconnected paid/manufacturer
catalogs remain external dependencies. Every snapshot has `coverage_complete: false`.
`COMPLETE` means the bounded reads finished; it never means every part was discovered.
A failed/truncated source produces `PARTIAL` while preserving other sources' records.

A named part runs only the sources that can answer it: engine oil uses the Fuchs, Shell
and Pakelo choosers, oil/air/cabin filters use JS Asakashi (its filter codes are searched
in the warehouse), and other categories run Motorium plus the local catalog. Unrequested
sources report `SKIPPED` with `NOT_REQUESTED` and do not make a job `PARTIAL`; a long
supplier result list is not a failure either. A job can start from make, model and year
(no VIN): vPIC, Motorium and JS Asakashi report `VIN_REQUIRED`, while the local catalog
and oil choosers still run. Broad category reads (blank part description) run up to four
Motorium lookups in parallel inside the same ten-per-minute budget.

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
records. Confirmed matches sort first. Price and currency remain unknown
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
scan may be expensive. Price and currency remain null, with the catalog
observation timestamp. `is_active` is never treated as stock. Stock comes from the
synchronized source columns (below).

Queue an index job using the authenticated API, or let the worker queue one itself with
`--auto` (see "Speed" below), then run:

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

Chat is the main workspace and reads like a messenger. Ask in Georgian or English (Latin-typed
Georgian such as `gamarjoba` is understood), give a VIN, a part code or the car in words
(“Honda Accord 2015”, “2015 წლის ჰონდა აკორდი”, “Prius 2012”), and continue with details such
as “rear ones”, “2015”, “yes” or “is the Brembo one in stock?”. With *Automatic* language the
server follows the customer's letters and keeps the conversation's language for messages that
are only a code or a VIN. Enter sends and Shift+Enter starts a new line; tappable chips answer
the bot's question (front/rear, pads/discs, common parts after a VIN); **New chat** clears the
conversation and selected vehicle. Direct VIN, inventory, evidence and oil controls remain
available in an expandable section.

### How replies are written

*Applies only when `AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`; the default
engine writes its own replies, see "Assistant engine (model-driven)".*

Replies are built by the backend (`automotive/replies.py`) from the verified answer only: the
car, how many parts were found, how many are in the store and how many can come from the TUZ 11
warehouse in 1-2 hours, at most one question, and at most one short note (said once per
conversation). Phrasing rotates so the customer does not read the same sentence twice. Card
details (match source, matched codes, observation date, evidence) sit behind a *Details* toggle;
an unknown price is not shown.

Answered without any model call (`automotive/dialogue.py`, deterministic):

- small talk (greetings, thanks, ok, bye, “are you a bot?”, help) — it never re-runs the search;
- a car typed in words, a bare year or engine code answering the bot's question, and “yes”/“no”
  to its offer (“Need the rear ones too?”);
- a message that is only part codes plus filler (“do you have AP104/2?”), and oil grades
  (“0W-20 oil?”); years, grades, quantities and car models are never treated as part codes;
- “the second one”, “the Bosch one” about the cards just shown (each refreshed for stock), or an
  honest “none of the ones I showed are Brembo” when that brand was not among them;
- a known car plus a part category, through the existing lookup pipeline.

The model is used only for open questions (cross-references, “does it fit…”) through the same
read tools and act/result-ID contract as before. A failed source search now says so (“Sorry, I
couldn't finish that just now”) instead of claiming the car's codes are unknown.

**Optional rewording.** `AUTOMOTIVE_CHAT_REPHRASE=true` lets the model reword result replies
within `AUTOMOTIVE_CHAT_REPHRASE_SECONDS` (default 1.2 s, thinking disabled).
`automotive/grounding.py` rejects any rewording that adds a number, code, brand, mid-sentence
name, price, delivery, guarantee, contact or link claim, an extra question, the wrong language,
or drops a count or code from the backend sentence; the backend sentence is then shown. It is
**off by default**: measured live, the call took 1.2-3 s per result reply (Georgian slower),
usually changed digits to words, and occasionally produced ungrammatical Georgian, for little
gain over the backend sentences.

Conversation IDs stay only in page memory. Refreshing creates a new conversation;
the abandoned conversation expires automatically. Each process holds at most 20
conversations, expires them after 30 idle minutes, and keeps 12 recent exchanges.
One turn runs per conversation; a changed vehicle invalidates earlier turns and code
associations. No chat history is written to the database or browser storage. Raw VIN
is held transiently in process memory for provider follow-ups, and is excluded from
model requests, history, logs, response payloads and URLs. Restarting loses live conversations
(the local chat log below survives);
run one application process for local use. These capability IDs do not replace staff
authentication for public deployment.

### Local chat log

Every conversational turn is recorded to a private SQLite file
(`data/chat_logs/chat_log.sqlite3`, directory mode 0700, file mode 0600, git-ignored) so
answers can be reviewed and improved. Set `AUTOMOTIVE_CHAT_LOG_PATH` to another file, or
to an empty value to disable logging; `AUTOMOTIVE_CHAT_LOG_RETENTION_DAYS` deletes older
turns at startup (0 keeps everything). Records are written on a background thread, so a
logging fault never delays or fails a chat turn.

- `sessions`: hashed session ID, start, end and end reason (`deleted`, `expired`, `shutdown`).
- `turns`: redacted question and answer, status (including `CANCELLED` and timeouts),
  language, category, duration, vehicle make/model/year/engine, warnings, a summary of up to
  20 offered parts (code, match source, fitment status), token usage, and rejection
  diagnostics. Raw provider records and full catalog rows are not stored.

What is never stored: raw VINs (they are masked as `[VIN]`, with only a keyed hash from
`AUTOMOTIVE_VIN_HMAC_KEY` to group turns by vehicle), configured secrets, and
conversation IDs (only a SHA-256 hash). Customer wording is still retained, so treat the
file as confidential and do not copy it off the machine without review.

Export for analysis with `uv run python -m automotive.chat_log --export chat_log.jsonl`
(`-` writes to stdout), or query the file directly with `sqlite3`, for example the most
common failed or unanswered questions:

```sql
SELECT question, status, COUNT(*) FROM turns
WHERE status != 'COMPLETE' GROUP BY question, status ORDER BY 3 DESC LIMIT 20;
```

#### Daily review: `python -m automotive.chat_review`

`uv run python -m automotive.chat_review` asks again every logged request that ended with
no parts and no question. It opens both logs read-only (`--log staff|site|both`, the last
`--since-hours 24`), takes the newest conversations up to `--limit 40` such turns, and
replays each conversation from its first message in a fresh conversation of the assistant
engine, with a larger budget (8 model requests and 90 seconds a turn) and no chat log, so
a replay never shows up among the chats. A turn is a **miss** when its replay ends with at
least one part that is in the store or at the warehouse: the catalogue had it and the chat
showed nothing. The judgement is the engine's own; the command holds no matching rules and
leaves no message out for being short or a greeting.

- Messages are replayed as logged, `[VIN]` included. Staff turns are also given the car the
  log recorded beside them (the form's car and what the VIN decoded to); the public chat
  has no form. A turn that had a VIN and whose replay finds nothing in stock is reported as
  **needs VIN**: not a miss, and not proof that nothing was missed.
- "No question" comes from the assistant engine's own record of the turn. Turns of the
  older engines have none: there an answer that ends with a question mark counts as a
  question, and the report says how many turns were decided that way.
- Reports go to `data/chat_logs/reviews/chat-review-YYYY-MM-DD.md` and `.json` (`--out`;
  directory 0700, files 0600, git-ignored): a summary table and a section for each miss
  with the customer's words, both answers, the car and up to five of the parts found. They
  hold customer wording, so treat them like the log. The command itself prints three lines
  of counts.
- Cost: only the replays call the model; they read free sources (no paid catalogue) and
  write nothing to PostgreSQL. `--budget-usd 1.0` ends a run once the replays' token usage
  has cost that much (counted at $0.10/$0.50 per million input/output tokens for Haiku 5.5,
  $2/$10 for Sonnet 5.5), and the report says how many turns were left unchecked. One
  replayed message is assumed, not yet measured, to cost about $0.007 on Haiku 5.5 and
  $0.14 on Sonnet 5.5. `--dry-run` lists what would be replayed with that estimate and
  calls no model; `--model ID` replays with another model.
- `ops/systemd/automotive-chat-review.service` and `.timer` run it daily at 22:30
  (Asia/Tbilisi), after the evening sync.

*The three paragraphs that follow describe the older engines and apply only when
`AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`.*

A message with a known vehicle (decoded VIN or make, model and year) and a part category,
or a VIN with no category, is answered directly from the same read job without a model
round trip: warehouse cards appear as the sources finish, brake questions show pad/disc
candidates together with the front/rear question, and oil shows catalog grades the vehicle
choosers listed (unverified) with the engine-code note. Messages containing a part code,
engine details or other free text still use the model-selected read tools. The reply names
each source that actually failed (`SOURCE_FAILED:<source>`); exact supplier-code hits in the
warehouse remain even when the catalog name lacks the category word.

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
our catalog; stock unconfirmed.** only when the stock cells are missing; otherwise the
card states the real availability (below). Supplier-only identifiers are separately labeled
**No warehouse match.** Prices remain unknown.

### Stock and the "suitable for this car" rule

Stock is read from `warehouse.warehouse_products.source_columns`: `column_10` (the source
column titled "1") is the quantity in the store; `column_12` ("TUZ 11") is held in
inventory and reaches the store in one to two hours. `column_11` ("TUZ 6") has no agreed
meaning and is never used. Products carry `stock: {store, tuz11}` (numbers, or null for a
cell that is not a plain number). Counts are as fresh as the last sync, which runs at 09:00
and 21:00 Tbilisi time; a card older than 13 hours adds `STOCK_MAY_BE_STALE`.

*The rest of this section is how the older engines choose what to offer and applies only
when `AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`; the assistant engine reads
the same stock columns, shows a matching part that is out of stock (marked so) and judges
fit as its own section describes.*

In chat, an item is offered only when it is **in the store** (`IN_STORE`) or **in TUZ 11**
(`WITHIN_1_2_HOURS`). Items with zero in both are not offered; if suitable items exist but
none is stocked the reply says so. An exact code the customer typed is still answered, with
"Not available now". Missing stock cells show "stock unconfirmed", never "in stock".

With a selected car, an item must also be **derived from the car's requirement**, not from
its name: an exact code from a VIN source (Motorium, JS Asakashi), an oil grade the vehicle
choosers recommend, the engine family the chooser gave (oil filters and sump plugs only), or
reviewed fitment evidence. A product that merely names the make and model is not offered in
chat (the direct panel still lists such candidates). Each card shows why it was offered
(`matched_via`), and fitment stays unknown unless evidence verifies it.

**Name fallback.** When no code could be derived for the car and a part category was asked
(not oil), the bot offers in-stock items whose catalog name contains the make and model
(common abbreviations such as MB and VW, flexible model spelling). A year range stated right
after the model (`08-`, `98-03`, `2010 >`) must contain the car's year, otherwise the item is
dropped; a name with no year is kept and says so. Every such card is labelled "Matched by the
car's name only; please verify compatibility" and the reply carries `NAME_MATCH_ONLY`. If any
code-derived offer exists the fallback is not used. Chassis-only names (for example `MB W212`)
are not matched by name; a catalog API (below) is the fix for those.

**Optional catalog API (`CATALOG_API_KEY`).** A TecDoc-style service (default: RapidAPI
"Auto Parts Catalog", about $39-69 a month) turns the VIN into OE and part numbers per part
group (oil filter, air and cabin filter, brake pads and discs, shock absorbers). The numbers
are searched in the warehouse like any other source code and cards show
"Catalog API OE: ...". It is off when the key is empty. The VIN is sent to this provider when it
is on. After adding the key in `.env` run `uv run python -m automotive --check-catalog-api`
(it asks for a VIN privately and prints only counts and sample codes). The vehicle's own
part-group ids come from its category tree (`products-groups-variant-1`), not from fixed ids,
and part numbers are trusted only together with their brand (`MANN` matches `MANN-FILTER`),
because a bare number can collide with another brand's. Verified live with one VIN (a 2015
E 400 Coupe): the exact catalog vehicle was found and oil filter, air filter, cabin filter,
brake pad and brake disc part numbers were matched in the warehouse; coverage on other cars
still has to be measured. Settings: `CATALOG_API_KEY`, `CATALOG_API_BASE_URL`,
`CATALOG_API_HOST`, `CATALOG_API_KEY_HEADER`.

Flow for an oil change (`ზეთის შეცვლა`, "oil change"): vPIC gives the trim, fuel and body;
Shell/Pakelo/Fuchs are matched by trim, fuel and body (not by displacement, which decoders
disagree on) and give the SAE grade, product line, capacity and engine code. Engine oils of
that grade are found in the catalog (same brand and line first, then the same grade from any
brand; diesel-only lines are excluded for petrol cars and the reverse). Oil filters and
sump plugs are found by exact engine family (`M 276.850` becomes `M276`, which matches
`M276` but never `M272/273`) and by Motorium codes. The reply lists what the car needs
(grade, capacity, engine) and which part has no stocked match. If the source cannot pin the
engine, the bot asks for it instead of listing oils.

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

The model selects conversational acts and known backend result IDs. Backend reply
sentences and validated backend records supply
technical facts, prices, stock, evidence and uncertainty. Invalid/unreferenced output
returns an ungrounded-response status. The UI clears dependent results when the vehicle
or code changes and suppresses stale in-flight responses. No conversation history or
raw VIN is kept in browser storage.

## Speed, accuracy rules and evaluation

### Why answers are fast

*Describes the older engines (`AUTOMOTIVE_CHAT_ENGINE=scripted` or `orchestrated`) and the
direct VIN search; the assistant engine's speed is given under "Cost and speed" below.*

- **Indexed code search.** `automotive.part_numbers` holds every product's codes, keyed by a
  stock-independent hash (`automotive.content_hash`, migration `0007`), so stock changes no
  longer make products look stale. A code lookup is about 60 ms instead of 1 s.
  Build it once with `uv run python -m automotive.worker --auto --once` (about a minute for
  65,000 products); `ops/systemd/automotive-index.timer` repeats it hourly, so it catches up within an hour of each sync. The
  worker needs only `AUTOMOTIVE_ENRICHMENT_DATABASE_URL`. Rows never indexed or parsed by an
  older parser are still found by a direct scan, so a missing index slows answers but never
  loses a match.
- **Fast tier first.** vPIC, the catalog API and Shell start together; the warehouse code
  search starts as soon as they finish. Motorium and JS Asakashi (code sources) run only when
  the catalog API is off, failed, empty or contradicted the decoded car, and Pakelo and Fuchs
  only when Shell cannot resolve the engine. Fallbacks get a short deadline (Motorium 7 s,
  JS Asakashi 5 s, Pakelo and Fuchs 4 s). Sources not needed show `NOT_NEEDED` and never make an
  answer partial.
- **Caches.** Catalog article lists (by catalog vehicle and part group), vehicle part groups,
  the VIN to catalog-vehicle lookup (keyed by an HMAC of the VIN) and chooser answers (by make,
  model, year, trim, fuel, body) are kept for 24 hours and shared by every customer with that
  car. Stock is read fresh on every answer. When a VIN first appears the car is resolved in the
  background (`prewarm`), so the part question that follows is mostly answered from cache.
- **Parallel and streaming.** Part groups are fetched four at a time, the page polls every
  0.15 s at first (small talk and code answers finish in milliseconds), a typing indicator shows
  until the first cards, and cards appear in a live bubble as sources finish.
- **No model on the common paths.** Small talk, typed cars, codes, grades, references and
  car-plus-part questions never call the model. The system prompt and tool list carry
  `cache_control`, which takes effect once they reach the provider's minimum cacheable size.
- **Search capacity.** The source coordinator keeps at most 20 search snapshots; finished ones
  older than 30 seconds make room oldest-first, so only 20 searches running at the same moment
  are refused. (Before, 20 searches within five minutes made every further search fail at once,
  and the chat then wrongly said the car's part codes were unknown.)
- **Paid catalog quota.** The VIN-to-part-number catalog API (RapidAPI) answers `RATE_LIMITED`
  once its plan quota is used; the BASIC plan allows 100 requests a month. Its cache lives in
  process memory, so every restart and every `--evaluate`/`--evaluate-chat live` run asks again.
- Each source reports `elapsed_ms` in the progress data.

### Hard rules (always on)

*These rules are code of the older engines and apply in full only when
`AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`; the assistant engine enforces
rules 1 and 6 in code and leaves the judgement of fit to the model.*

1. Only active products are offered (`is_active`).
2. Front/rear: items named for the other axle are dropped (the catalogue abbreviations `წნ` and
   `უკ` count); an item with no axle word stays, labelled "axle not stated", and ranks last.
3. A code match that is another kind of part (a disc for a pad request, a filter for engine oil,
   or any word in `not_the_part`) is dropped.
4. If the catalog API's vehicle contradicts the decoded car, its codes are ignored
   (`VEHICLE_MISMATCH`). Several catalog vehicles for one VIN already means "do not guess".
5. A bare number is trusted only with its brand. OEM-labelled codes shorter than 6 characters,
   or all-digit codes shorter than 8, are never searched.
6. Stock older than `hide_after_hours` (25) is not stated as a fact: the customer is asked to
   confirm. Older than `stale_hours` (13) adds a confirm-in-store warning.
7. Oil quantity: the reply shows whether the stocked pack sizes add up to the engine's litres.
8. A typed code is answered as typed; only `is_active` and the stock label apply to it.

### Staff rules: `automotive/rules.toml`

*The assistant engine reads `[stock]`, `[[brand_alias]]` and `[[never_offer]]` only; the
other sections take effect when `AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`.*

Edit the file and restart the app. It is validated at startup; a mistake stops the app and
names the section. Sections: `[stock]` (minimum quantities, stale hours), `[[brand_alias]]`
(brands that are one brand), `[[not_the_part]]` (words that remove an item from a category),
`[[oil_approval]]` (approvals a car needs; oils stating one rank first, others are labelled
"approval not stated", `strict = true` hides them), `[[never_offer]]` (a product code never
offered, optionally for one make and model) and `[[staff_confirmed]]` (a fitment staff
verified, offered first as "Confirmed by our staff", still subject to stock and `is_active`).
The oil-approval, never-offer and staff-confirmed sections ship as commented templates: staff
fill them in; nothing is invented. A staff-confirmed fitment is not the evidence-reviewed
`VERIFIED` status.

### Measuring: `--evaluate`

*Its result columns describe the older engines' lookup, so run it with
`AUTOMOTIVE_CHAT_ENGINE` set to `scripted` or `orchestrated`; the assistant engine is
measured with `--evaluate-assistant`.*

```bash
uv run python -m automotive --evaluate vins.csv --misses-out misses.csv
```

`vins.csv` has one VIN per line (first column; a header and `#` comments are fine) and is
git-ignored. For each VIN and request (default: oil change, front brake pads, air filter, cabin
filter; override with `--categories "a,b"`) it prints the car, seconds, in-store and TUZ 11 offers,
the result (`OFFERS`, `NONE_IN_STOCK`, `NO_CODES`, `NO_VEHICLE`), the rules that rejected items
and slow or failed sources, then totals per request. VINs appear as `VIN#n`. The misses CSV has
no VIN (make, model, year, trim, request, reason) so staff can add rules where the bot found
nothing. The command writes nothing to the database.

### Measuring conversations: `--evaluate-chat`

*Its scripts and checks were written for the older engines' backend-written replies and
apply when `AUTOMOTIVE_CHAT_ENGINE` is `scripted` or `orchestrated`; the assistant engine
is measured with `--evaluate-assistant`.*

```bash
uv run python -m automotive --evaluate-chat mock                       # offline, CI
uv run python -m automotive --evaluate-chat live --vins vins.csv --judge --report-out r.json
```

Runs the bilingual multi-turn scripts in `tests/fixtures/conversations/` through the same
conversation store as the web chat. Hard checks (every number, code, brand and name in a reply is
backed by the cards, car, requirements or customer's words; reply language; expected behaviour
such as “thanks does not search”, the category, axle, vehicle and quick replies) must pass. Soft
checks score natural feel (length, one question, at most one caveat, no repeated reply, names the
car, says how many). Timings are reported per kind of turn, with routing model calls and rewording
attempts counted separately. `--judge` (live only) asks the model to rate each transcript from 1 to
5. Live mode skips expectations tied to the synthetic catalog; `{VIN1}` is the first VIN in the
file and VINs are never printed. `tests/test_conversation_eval.py` runs the mock suite as a test.

## Assistant engine (model-driven)

Since 2026-11-10 both chats, the staff chat at `/staff` and the widget on the public site,
are answered by the assistant engine (`AUTOMOTIVE_CHAT_ENGINE=assistant`, the default). The
two older engines stay selectable; the sections that describe them say so.

### Why the engine changed

Both chat logs (498 turns) were read and every failed request was checked against the
catalogue. On the engine that was live, 12 of 74 requests for a car or a part ended with
parts (staff chat 10 of 50, public widget 2 of 24), although the parts were usually in
stock. The causes, each traced to code:

1. A rule dropped every row found through the catalogue's own text once a car was known.
   18 turns ended in "could not confirm an offer" this way.
2. The search found only rows that contain the make and model as words. The catalogue
   writes `MB W212`, `BMW F10`, `MMC L200`, `VAG GOLF 7`: 27 Mitsubishi rows were seen
   where 3,078 exist, 8 E-Class rows where about 886 exist.
3. Parts were recognised by 20 fixed patterns, which reach 30% of the catalogue.
4. The model had to quote the customer word for word for every fact, so "Camry" gave no
   make and the bot asked for it.
5. Rules in front of the model swallowed answers ("yes", "ორივე", `W212`) and deleted
   questions, and most questions had no buttons.
6. The design depended on part numbers that the free sources rarely give for a typed car.
7. Matches that were out of stock were hidden without a word (9 turns).

The common cause: the catalogue is typed by hand, and a hand-written rule covers only the
spellings its author thought of. Replacement rules tried on the real rows picked wrong
parts too (`>2009` means "from 2009" on some rows and "up to 2009" on others), so this
engine takes rules out of the path instead of adding more.

### How a turn works

The model works like a counter clerk who can read the whole catalogue. No list or pattern
in the code decides what the customer means, which rows fit or what to ask.

1. **Before the model.** The store (`automotive/conversations.py`) keeps the VIN checks,
   redaction and turn bookkeeping and reads nothing out of the message. A VIN is decoded
   by the backend and never sent to the model, which sees `[VIN]` and the decoded facts.
   The turn (`automotive/assistant.py`) attaches data only: exact hits for part numbers
   typed in the message, fresh copies of the rows shown last turn, the model's own notes,
   and the question it asked last time with its options and which one was tapped.
   The staff page sends its vehicle form with every message. It reaches the model apart
   from the conversation, as `staff_form`: the car fields a clerk filled in, and the
   facts decoded from the VIN in the form's VIN field. The form may be left over from
   the customer before, so it is the car only while no message names one: a make, model,
   year or engine stated in a message outranks it, and then the form's VIN is not this
   car's either.
2. **What the model knows.** The instructions (`automotive/assistant_prompt.py`) and a
   catalogue guide computed from the live catalogue (`automotive/assistant_catalog.py`):
   how makes are written and how often, part names and abbreviations, year notations, each
   with a real example. Beside each part name the guide says what the part is in plain
   English (`სტაბ კრონშტეინი` is the stabiliser link, `სტაბ მილისა` the bar's bushing): a
   model read that from the rows that carry the name (`--read-part-names`, below), because
   the shop's names are not what their literal translations say. Nothing in the guide is
   written by hand; it is built once per catalogue snapshot and cached with the provider.
3. **Searching.** The model calls read tools, several at once, with words it chooses
   (a Camry is a Toyota, a 2015 E-Class is a `W212`, `კალოტკა` means brake pad). A search
   that finds nothing answers with how the catalogue does write that car and part and
   with near spellings, so the model corrects itself.
4. **Judging and answering.** The model reads the rows, decides which fit this car, and
   ends the turn with one `finish` call: the reply, the rows it chose (each with a
   confidence, the words of the row it rests on, a reason and a group), a question with
   2 to 8 answer buttons when the answer changes which rows are right, next-step buttons,
   the car as it understood it, and notes for the next turn.
5. **Code keeps facts honest.** Cards are built from database rows by id with stock read
   again: name, price and stock are never model text. A row the model names must be one
   it was shown and still be in the catalogue, the quoted words must be in the row,
   `confirmed` needs a number from a car-specific source of this conversation, the text
   states no price, a reply that asks carries its buttons, and a `[[never_offer]]` entry
   of `rules.toml` is a veto. A failed check goes back to the model with the reason, at
   most twice; after that the valid part of its answer is used.
6. **No dead end.** Every reply holds parts, or a question with buttons, or next-step
   buttons. A model or network failure gives an apology with a retry button. A matching
   part that is out of stock is shown, marked so.

The reply follows the customer's language (Georgian, English, Russian; Georgian typed in
Latin letters is answered in Georgian script). Labels of the page stay Georgian or English.
Web research is not part of a live turn any more.

The engine prepares itself when a conversation opens (it builds the catalogue index and
has the provider read the fixed part of the prompt), so both screens open the conversation
when the customer starts to write, not when the message is sent.

### Tools

| Tool | What it reads |
|---|---|
| `search_catalog` | The shop's catalogue by make, vehicle and part terms and exclusions the model chooses. Up to 30 rows, in-store first, with totals, the other vehicle texts and part names that matched, near spellings, rows filed under a word one letter away, and rows in stock that carry the numbers of an out-of-stock row. Matching is string mechanics only: a Georgian word matches as the start of a word, a Latin word a whole word however it is spaced (`CR-V`, `CRV`), and a number of three or more digits also the code that carries it behind letters (`150` finds `J150`, as customers say "Prado 150"). |
| `lookup_codes` | Part numbers, exactly: the brand's own, the shop's and cross numbers, what the crawled parts catalogue says a number is and which cars it lists. |
| `equivalents` | Rows that share reference numbers with a row (one shared number is flagged as a hint only). |
| `supplier_parts` | Outside catalogues for this car and part group, through the existing source federation, for at most 6 seconds. |
| `oil_guide` | The Shell, Pakelo and Fuchs oil selectors: grades, capacity and engine code. |
| `car_numbers` | The crawled manufacturer catalogues (MANN-FILTER, NGK and the others in `parts_catalog`): which part numbers they list for this car in one part group, and the shop rows that carry those numbers, whatever car a row's own text names. The model gives the car's words and codes; it then picks the crawled vehicle that is the customer's car (`vehicle_ids`), and only that vehicle's numbers count for `confirmed`. Numbers shorter than five letters and digits are not matched to rows. |
| `research` | A separate, time-boxed web-search request (`assistant_research.py`): the numbers that fit this car for a part, whether given numbers fit it, or a fact about the car. It runs beside the catalogue search, waits at most `AUTOMOTIVE_RESEARCH_SECONDS` (8 by default), keeps running in the background to fill its cache when it takes longer, and returns findings with the sites that show them and the shop rows that carry the numbers found. Its text comes from web pages, so in what the model sees links (also without `http` or `www`), e-mail addresses and phone-shaped numbers are removed unless the token is a number the shop carries or the request asked about, pages appear by their site only, and a finding whose number is not written like a part number is dropped: a page cannot plant a contact for the bot to pass on; the cache keeps the result as it came. Never a VIN or anything about the customer in a query; 7zap, PartSouq, RealOEM and RockAuto are blocked. Rows found this way are `check`, never `confirmed`. |
| `shop_info` | The shop's own facts from the site content (phone, address, hours, services), in Georgian and English. |
| `finish` | Ends the turn (see step 4). |

All tools are read-only. Rows reach the model under per-conversation handles (`r1`, `r2`),
and the shop's own stock numbers are given only in the staff chat.

### Confidence levels

Every part shown carries one of four levels; both screens show it as a fixed label with
the model's reason under it. When every part of a group rests on the same level and the
same reason, the label and the reason stand once under the group's heading and not on
each card; "Show more" keeps that right as parts are added. A level a screen has no label
for is shown without one.

| Level | Meaning | Label |
|---|---|---|
| `confirmed` | A part number that a tool returned for this car in particular (`supplier_parts`, `oil_guide`, or `car_numbers` for the crawled vehicle the model chose) is on the row; the cars `lookup_codes` lists for a number are not enough. | Matches by part number |
| `listed` | The row's own text names this model with a year range or generation that includes the car. | Listed for this model and year |
| `check` | The fit rests on the model's name alone or on indirect evidence; the reason says what to check. | Needs checking |
| `spec` | The row's text ties the product to no car (an engine oil, a fluid, a universal item); it was chosen because its specification is what the car needs, and the reason names that specification. | Chosen by specification |

One more judgement, `depends`, is internal: rows that fit only if the customer answers the
open question one way are held back until that answer.

### Alternatives

One pad, filter or link often fits several models while the shop's row names only one of
them. A row is offered for the customer's car when one of its numbers is listed for that car
by a tool that answers for that car (`car_numbers` for the chosen crawled vehicle,
`supplier_parts`, a `research` finding that names the car), or when it shares two or more
numbers with a row that fits. It is shown in a group of its own, with a reason saying that the
catalogue lists it under another car and that the part number matches (an E63 row carrying the
E400's number is offered to the E400 owner). When the row that names the customer's car is
out of stock, a search also lists the in-stock rows that are the same part by their numbers
(`in_stock_under_other_cars`); a search narrowed to stock (`in_stock_only`) still finds them
and shows the out-of-stock rows they stand for (`out_of_stock_rows_with_a_twin_in_stock`).
The model judges every such row; no table of interchangeable models exists anywhere in the
code.

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `AUTOMOTIVE_CHAT_ENGINE` | `assistant` | `assistant`, `orchestrated` or `scripted`. Empty selects by `AUTOMOTIVE_CHAT_ORCHESTRATION`, as before this engine existed. |
| `AUTOMOTIVE_ASSISTANT_MODEL` | `claude-sonnet-5-5` | The model of this engine. Empty uses `ANTHROPIC_MODEL`. |
| `AUTOMOTIVE_ASSISTANT_EFFORT` | `low` | `low`, `medium`, `high`, or empty for the API default. |
| `AUTOMOTIVE_ASSISTANT_THINKING` | empty | Empty keeps the model's own default; `off` asks for no thinking before an answer. |
| `AUTOMOTIVE_ASSISTANT_TURN_SECONDS` | 30 | Seconds one turn may take (10 to 120). |
| `AUTOMOTIVE_ASSISTANT_MAX_REQUESTS` | 4 | Model requests in one turn (1 to 8). |
| `AUTOMOTIVE_ASSISTANT_MAX_TOOL_CALLS` | 10 | Read-tool calls in one turn (1 to 20). |
| `AUTOMOTIVE_ASSISTANT_CACHE_TTL` | `1h` | How long the provider keeps the fixed part of the prompt ready: `1h` or `5m`. With an hour the first message after a quiet spell does not wait for the prompt to be read again; writing it costs twice the input price instead of 1.25 times. |
| `AUTOMOTIVE_ASSISTANT_NAMES_PATH` | `data/assistant/part_names.json` | The file with what the catalogue's part names mean, printed in the guide. Empty, or a file that does not exist: the guide lists the names without meanings. |
| `AUTOMOTIVE_VEHICLE_READINGS_PATH` | `data/assistant/vehicle_readings.json` | What the catalogue's vehicle texts mean (makes, models, generations and years), read once by a model and carried beside search results as `vehicle_readings`; the row's own text decides when they disagree. Made with `uv run python -m automotive.assistant_vehicles` (Claude subscription, nothing billed; it continues where it stopped, `--all` reads everything again, `--everything` also texts no row in stock carries). Empty, or a file that does not exist: results carry the texts alone. |
| `AUTOMOTIVE_NAME_READINGS_PATH` | `data/assistant/name_readings.json` | The same for the catalogue's part names, carried as `name_readings`: what each part is and every position its marks state (for a bush or mount, which arm or beam and which of its bushes). Made with `uv run python -m automotive.assistant_vehicles --names`. Empty, or a file that does not exist: none. |

The part-name file is made by a model, not by hand, and `data/` is not in the repository:
run `uv run python -m automotive --read-part-names` once on every machine that serves the
chat (175 names cost $1.15 on Opus 5.5 on 2026-11-10), and again when the catalogue gains
new part names; it reads only the names that have no reading yet, `--all` reads every name
anew. Each reading is at most 12 words and comes from the name's most written forms and
example rows (vehicle text, brand, number, cross numbers). The file is plain JSON, so a
parts person can correct a reading in it; the engine picks the change up by itself.

When the provider's account refuses every request, the chat cannot answer and asking again
does not help. The engine tells the three causes apart from a passing fault: no credit
(`PROVIDER_BILLING`, the provider's "Your credit balance is too low"), a key that does not
work (`AUTHENTICATION_ERROR`) and no access to the model (`ACCESS_DENIED`). The customer
reads that the assistant is not available right now and is pointed to the part search and
the phone, with no "try again" from the engine; the staff chat adds the cause under the
reply ("the Anthropic account is out of credit"); and the server log gets one line per
such turn at error level, `assistant_provider_account status=PROVIDER_BILLING`, which is
the line to watch for. This happened on 2026-11-10 when testing used up the account's
credit. An evaluation run stops at the first such turn, charges nothing for it and marks
the rest "not run".

To go back to an older engine set `AUTOMOTIVE_CHAT_ENGINE=orchestrated` (AI-led routing,
below) or `AUTOMOTIVE_CHAT_ENGINE=scripted` (backend-written replies) in `.env` and
restart. Their code and tests are untouched, and both screens still draw their turns:
`automotive/chat_contract.py` reads the question, the next steps and a confidence level
out of what an older engine sends.

### Cost and speed

Measured on 2026-11-10 with `--evaluate-assistant conversations`: 72 test conversations
(87 turns) on the real model and catalogue, 3 of them skipped for want of a private VIN.

| | Haiku 5.5 | Sonnet 5.5, run 1 | Sonnet 5.5, run 2 | Sonnet 5.5, thinking off |
|---|---|---|---|---|
| Conversations passed | 52 of 69 | 63 of 69 | 63 of 69 | 61 of 69 |
| Expected rows found | 78 of 91 | 78 of 88 | 81 of 88 | 71 of 84 |
| Wrong rows offered | 12 | 0 | 2 | 7 |
| Questions without buttons | 0 of 26 | 0 of 26 | 0 of 26 | 0 of 29 |
| Median seconds, all turns | 7.6 | 6.7 | 7.6 | 9.0 |
| Median seconds, turns that look for a part | 8.7 | 7.1 | 8.1 | 9.3 |
| 90th percentile seconds, all turns | 11.6 | 9.2 | 11.4 | 12.7 |
| Cost per turn | $0.0017 | $0.025 | $0.026 | $0.027 |

Sonnet 5.5 at low effort with thinking left on is the default: it finds as many of the
expected rows as Haiku and offers almost no wrong ones, and it is no slower. It costs
about 15 times more per turn, about 2.5 cents. Greetings and part-number look-ups take 3
to 5 seconds, because nothing answers before the model. In the browser walks of the same
day the median was 5.9 s at $0.023 a turn (28 turns on both screens) and, in the final
walk on the engine as it is now, 7.9 s at $0.029 a turn (35 turns); see
[the walk's note](automotive-review/assistant-chat-qa.md).

The engine changed after the model was chosen (the catalogue-wide check, the part-name
readings, the one-hour prompt cache, the wider number match). The last complete run of the
72 conversations passed 66 with 4 wrong rows, all of them spark plugs offered as "needs
checking". On the final code a run reached 39 conversations before the API account ran out
of credit: 38 passed, no wrong row, 7.5 s at the median for a turn that looks for a part
(10.5 s at the 90th percentile) and 3.8 s for a quick turn, about 3 cents a turn.

The catalogue-wide check of the same day (`--evaluate-assistant catalogue`): a second
model asked for 147 sampled stocked parts in customers' words, in four ways of writing.

| | Found after 1 turn | after 2 | after 3 |
|---|---|---|---|
| Before the fixes it led to | 40% | 63% | 71% |
| After them, the same requests | 46% | 80% | 86% |
| Fresh requests after them (39, then 19 after the part-name readings) | 46%, 42% | 74%, 89% | 90%, 89% |

Requests that name a model were found in 97% of the fresh cases; requests that name only
a make ("a bushing for a Honda") are the weak ones. A stronger model that reviewed the
offers called 4 of 24 rows wrong in the last sample, all of them shown as "needs
checking" and none of those presented as listed or confirmed. Known weak spots: when
variants differ by engine and only one is in stock the engine often shows it as "needs
checking" where it should ask; and when a row names only the chassis, the heading the
engine gives a variant comes from its reading of the part number and can be wrong. The
full account is in `IMPLEMENTATION_PROGRESS.md`; reports and the judged rows for a parts
person are in `data/assistant_eval/`.

### Research settings

| Variable | Default | Meaning |
|---|---|---|
| `AUTOMOTIVE_RESEARCH` | `staff` | `off`, `staff` (the staff chat only) or `all` (the public widget too). Keep the widget off until the provider path has been checked once (below). |
| `AUTOMOTIVE_RESEARCH_MODEL` | `claude-haiku-5-5` | The model of the research request. |
| `AUTOMOTIVE_RESEARCH_SECONDS` | `8` | How long a turn waits for research (1-30). |
| `AUTOMOTIVE_RESEARCH_CACHE_PATH` | `data/assistant/research_cache.sqlite3` | Answers kept 30 days, empty answers a day. |

Research goes through the chat's own client, so the OpenRouter billing fallback applies;
should the provider refuse Anthropic's search tool there, it is asked once in OpenRouter's own
search shape. A researched turn costs about 1-3 cents (search fee plus tokens); a cache hit
costs nothing. To check the provider path once before turning research on for the public
widget, run `uv run python -m automotive --check-research`: one billed request (about 1-3
cents) with a fixed question that holds no customer data (front brake pads of a 2015 Camry),
no cache. It prints the status, seconds, searches and the numbers found with their sources'
sites, and the `assistant_research ... backend=` log line says whether the direct client or
OpenRouter's own search shape answered. `status=answered` (or `no_findings`) means the path
works; anything else keeps research where it is.

### The subscription bench and the randomised exam

Both run on the Claude subscription of the machine's logged-in Claude Code, so nothing is
billed to the API (`automotive/assistant_bench.py`). The bench's `SubscriptionClient` stands
where the provider's client stands, so the production turn runs unchanged: each turn is a
headless `claude -p` session (Sonnet 5.5, effort low) that receives the engine's own system
prompt, tools and turn context, and whose tool calls are held at a local MCP endpoint until
the engine has run the real tool. The child processes get a whitelisted environment (no API
key, no hooks, plugins, memory or project instructions). Measured on 2026-11-11 against the
last paid run of the 72 test conversations: 67 passed on the bench, 66 in production, with
the same number of model requests per turn (1.93 and 1.89); speed is not measured on the bench
(its sessions start per turn) but estimated as requests x 3.4 s plus tool time.

```bash
uv run python -m automotive --evaluate-assistant conversations --runtime subscription
uv run python -m automotive.assistant_exam build --name dev-101 --size 170 --seed 101
uv run python -m automotive.assistant_exam run --name dev-101 --parallel 4 [--research on]
uv run python -m automotive.assistant_exam report --name dev-101
uv run python -m automotive.assistant_exam compare --name round-2 --against dev-101
uv run python -m automotive.assistant_exam regrade --name dev-101   # crawled truth, no model
```

The exam (`automotive/assistant_exam.py`) draws new cases on every build (the seed is kept, so
a dev set can be reused): text requests for a stocked row, the same with a VIN that the real
decoder decodes to the car, typed part numbers in varied forms, alternatives (a crawled
catalogue lists one of a row's numbers for a car its text does not name, or the in-stock twin
of an out-of-stock row), numbers the shop does not carry, and questions about the shop. The
right answers come from data: a case passes when a stocked row of its truth shows within three
turns (for a number the shop lacks, when nothing is shown as fitting). The
truth of a crawled alternative is every row carrying a number the crawled catalogue lists for
that vehicle and part (one car often takes a spin-on filter or an element); runs built before
that rule are brought up to it with `regrade`, which asks no model.
Rows shown outside the truth go to a dispute judge (Opus on the subscription); the share it
calls wrong is the second headline number. Since 2026-11-12, by the owner's decision, a case
also passes when a stocked row the judge says fits shows within three turns; every report
prints the share from the data truth alone beside it. Requests that name only a make are
reported apart.
Runs log to `data/assistant_eval/exam/<name>/exam.log`, show a progress bar and continue
where they stopped; once the subscription's usage limit is reached they take no new case (stop
the run and start the same command again later). A model request of the exam's own that fails
(the writer, customer or judge) is logged in `errors.jsonl` and never scored against the bot.

Measured on 2026-11-11 over two held-out draws on that day's engine (741 conversations),
from the data truth alone: 95.5% right outcome (93.8-96.8%) and 2.9% of shown rows wrong (1.7% and
4.2% in the two draws), short of the 98% and 1% aimed for. With Opus 5.5 as the model, two
more draws (772 conversations) gave the same 95.5% and 2.6% wrong rows, at twice the price;
paired on the same new cases it found no more parts, and fewer wrong rows were not shown.
With the vehicle-text readings beside the results (round H), a paired half draw of 187 cases
found as many parts and showed no wrong row (against 4 without them). A VIN case whose truth
row names no generation has no data truth (the VIN pins the year); such cases are now built
as text cases, and with them set aside the earlier draws score 95.0-96.6%. Typed numbers, numbers the shop lacks and shop questions
passed every time; alternatives 89-97%, text requests 94-95%, VINs 81-82%. Of the failures
that repeat on a rerun, at most 10 of 20 were the exam's or the catalogue's, and 10 more
failures were the bot's run-to-run variance; the wrong rows come mostly from variants
taken too widely (a related model, another position, a misread year range). The judge
probably undercounts wrong rows (it called 81% of the rows people marked wrong wrong). Speed
is estimated (requests x 3.4 s, the median measured on the direct API on 2026-11-10), not
measured through OpenRouter. On 2026-11-12 the three newest full draws (vehicle readings and
rounds I and J; the last two also with the part-name readings) scored 97.9%, 96.5% and 95.6%
right outcome (97.4%, 96.2% and 95.3% from the data truth alone) with 4.1%, 3.3% and 3.3% of
shown rows wrong; round M, added last, fixed 2 of 193 paired cases and broke none. The bar of
98% and 1% is not reached. The rounds, the failure sorting and the judge's measured
reliability are in `data/assistant_eval/rounds/narrative.md`; catalogue rows the tests point
at, for a parts person to check, are in `data/assistant_eval/catalogue_fix_list.md`.

### Running the checks

```bash
uv run python -m automotive --evaluate-assistant conversations   # the test conversations
uv run python -m automotive --evaluate-assistant catalogue       # does it find what is stocked?
uv run python -m automotive.chat_review                          # the last day's misses, from the logs
node tests/browser/assistant_chat_walk.mjs                       # both screens in a browser
uv run python -m automotive --read-part-names                    # what new part names mean, for the guide
```

All five call the real model and cost money. The two evaluations and the name reader append what they spend
to a ledger (`--ledger`, default `AUTOMOTIVE_EVAL_LEDGER` or
`data/assistant_eval/cost_ledger.jsonl`) and do not start, or go on, once its total reaches
`--budget` (40 USD by default).

- `conversations` runs `tests/fixtures/assistant/conversations.json` through the same
  conversation store the chats use and checks each turn against what should and must not
  be offered, whether a question has its buttons, questions about things already known,
  the reply language, seconds and requests. `--model ID` compares another model,
  `--audience staff|public` chooses the chat, `--only TAG,ID` narrows the run, `--parallel N`
  runs conversations side by side, `--report-out FILE` keeps the full report. Scenarios
  with a VIN take it from a private `vins.csv` (`--vins`); without one they run on a
  stand-in, a VIN of no car that the run itself decodes to the scenario's car, so the
  model's side of the VIN path is tested without customer data. A VIN is never printed.
  Run it with `--parallel 2` when the seconds matter: more conversations side by side
  wait for each other and the measured seconds grow.
- `catalogue` samples stocked rows (`--sample`, 150 by default; `--seed`), has a writer
  model phrase what a customer who needs that row would type and answer the engine's
  questions from the row, and counts how often the engine shows the row or one sharing
  two or more numbers within three turns. A stronger model then reviews a sample of the
  offers (`--review`, 60 by default; `--csv-out FILE` exports them for a parts person).
- `chat_review` is described under "Local chat log" above.
- The browser walk is described in its own header and in
  [the walk's note](automotive-review/assistant-chat-qa.md); `WALK_DRY=1` runs it on
  scripted replies without the model.

Without a model call: `tests/test_assistant_catalog.py`, `tests/test_assistant.py` (a
scripted model), `tests/test_assistant_eval.py`, `tests/test_chat_contract.py`,
`tests/test_chat_review.py`, `tests/test_site_chat.py`, and the two screen suites
`node tests/site_chat_widget_test.mjs` and `node tests/automotive_chat_ui_test.cjs`.

## Data sources, permission and coverage

### AI-led conversational routing

*Applies only when `AUTOMOTIVE_CHAT_ENGINE=orchestrated` (or when it is empty and
`AUTOMOTIVE_CHAT_ORCHESTRATION=true`); the default engine is described above.*

`AUTOMOTIVE_CHAT_ORCHESTRATION=true` enables the bounded AI-led conversation flow
when settings are loaded from the environment. Set it to `false` to restore the
legacy routing path. Unambiguous greetings, exact part codes, and references to
previously shown products remain immediate. Ordinary vehicle requests and answers
to pending vehicle questions reach intent extraction before a missing-detail question
is chosen, including Georgian inflections, transliteration, and corrections.

Each conversational turn has one deadline (`AUTOMOTIVE_CHAT_TURN_SECONDS`, 75 seconds by
default), shared by model requests, provider reads, and automatic web research. When the
catalog sources link no product for a requested part, the backend searches the web for
that part's OE and aftermarket numbers and looks up exact matches in the warehouse; those
cards say the number came from a web source and fitment must be verified. This research
alone takes 20-40 seconds. Motorium is disabled by default (`MOTORIUM_ENABLED=false`). Limits are six model requests, ten client
tool executions, and two native Anthropic web searches, including continuations.
Standalone search retains its separate budget. Successful partial findings remain
available if a source fails or time expires. Vehicle revisions invalidate dependent
results and prevent superseded turns from publishing old configurations.

`AUTOMOTIVE_CACHE_PATH` defaults to `data/automotive-cache.sqlite3`, a private,
versioned SQLite cache for catalog, chooser, and cited research results. Successful
entries expire after 24 hours and empty entries after 60 seconds. Stock, credentials,
and raw VINs are excluded; warehouse offers refresh synchronized inventory records.

Customer-stated details, source-supported candidates, and selected configuration
remain separate through follow-ups. The assistant asks one targeted question when
variants need distinguishing. Sources and excerpts appear as source assertions,
with vehicle applicability explicitly unconfirmed. A supplier assertion or web
excerpt does not establish reviewed fitment, oil approval, interchangeability,
warehouse price, or stock. Retrieved material never becomes approved evidence
automatically. Unsupported catalog routes and unavailable AI/search produce a
capability limitation while retaining successful reads.

Deterministic regression and browser checks exercise extraction, scattered details,
short vehicle answers, corrections, vehicle switching, citations, stale revisions,
and responsive layout. Synthetic provider/model responses verify those contracts;
they do not prove live provider coverage, model interpretation quality, oil
requirements, or cached/cold lookup latency. Keep bounded live acceptance runs
separate from deterministic tests. See the
[conversation and browser QA report](automotive-review/orchestration-surface-qa.md)
for the recorded interface checks and screenshots.

### Learned knowledge base

Ordinary chat lookups can gradually build a reusable store of vehicle identity, oil
viscosity, approvals, capacity, product recommendations, part numbers and fitment.
The flow is: check stored facts, research what is missing, answer normally, validate
findings in the background, then save accepted facts. Nothing is crawled or backfilled.

- **Exact profiles.** Facts are keyed by the normalized configuration: make, model,
  year, trim, generation, engine code, displacement, cylinders, aspiration, fuel, body,
  transmission, drive and market. Unknown details are never wildcards, and literal
  qualifiers such as "3.0 twin turbo" join the profile. An E400 coupe finding never
  answers another E-Class configuration.
- **Separate claims.** Each finding is stored as its own claim with source URL, provider
  IDs, observation time, parser version and restrictions. Accepting one never accepts
  another.
- **Independent judge.** A separate worker runs `AUTOMOTIVE_JUDGE_MODEL` with Claude web
  search and fetch. It researches the frozen request first, without seeing candidates,
  then returns a typed verdict per claim. Backend checks reject citations that were not
  fetched in this attempt, quotes absent from the fetched text, sources sharing the
  candidate's dataset, broader or narrower applicability, and values or units missing
  from the cited passages. Only `accepted` claims are published.
- **Machine-accepted, not VERIFIED.** Reused facts are labelled `MACHINE_ACCEPTED` and
  keep provisional compatibility; human-reviewed evidence remains a separate path.
  Warehouse offers are still refreshed from current inventory reads.
- **Reuse.** A complete hit skips supplier reads and judge work; partial hits research
  only the missing categories. Facts stay reusable for `AUTOMOTIVE_KNOWLEDGE_FRESHNESS_DAYS`
  (180 by default). A new contrary observation suspends the affected fact until an
  operator resolves it.
- **Budgets.** At most `AUTOMOTIVE_KNOWLEDGE_DAILY_ATTEMPTS` (25) research attempts per
  UTC day, including retries; one concurrent attempt; five searches, five fetches and
  two minutes per attempt. Technical failures retry once; excess work stays queued.
  Unsuccessful candidate payloads are pruned after 30 days.

Enable it after migration 0008, a validator login and source reuse policies exist. Only
sources whose terms allow reuse should be authorized; the migration owner records them:

```sql
INSERT INTO automotive_knowledge.source_policies (source, reuse_permission, dataset, configured_by)
VALUES ('shell', 'authorized', 'oats', 'operator-name');
```

Valid sources are `vpic`, `catalog_api`, `motorium`, `shell`, `pakelo`, `fuchs` and
`jsfilter`. Findings from sources without an `owned` or `authorized` policy are never
queued. Then set `AUTOMOTIVE_KNOWLEDGE_ENABLED=true`, restart the application and run
the validator separately:

```bash
uv run python -m automotive.knowledge_worker process          # continuous
uv run python -m automotive.knowledge_worker process --once   # one attempt
uv run python -m automotive.knowledge_worker status
uv run python -m automotive.knowledge_worker revoke --fact-id ID --actor NAME --reason TEXT
```

Database or judge failures leave the ordinary chat workflow unchanged.

The original sources and the subsequently requested public Motorium connector are
listed below. Motorium is an observed web protocol, not an official supported API.

| Source | Current use and coverage | Permission/licensing boundary |
|---|---|---|
| Existing warehouse catalog | Code associations, manufacturer, product identity and observation time; no confirmed price/stock mapping | Operator-selected existing catalog; original access/reuse controls still apply |
| [NHTSA vPIC](https://vpic.nhtsa.dot.gov/api/) | Initial vehicle decoding from manufacturer VIN submissions; no certified fitment, oil specification or product approval | Public service with automated rate control; follow published terms and validate regional coverage; no paid subscription |
| Owned/authorized supplier evidence | Operator-imported fitments, directional references, maintenance specs and actual product approvals | Operator must possess explicit reuse rights; provenance labels alone are not proof of licence or truth |
| [Motorium](https://motorium.ge/) | Public assistant and product JSON-LD return supplier MPN/OEM-labeled candidates for VIN + part query | Public read access observed; reuse rights are not independently recorded. No automatic evidence approval or paid subscription. See [protocol and limitations](integrations/motorium.md). |
| [Fuchs](https://www.fuchs.com/de/en/oil-chooser/), [Shell](https://www.shell.com/motorist/find-the-right-oil.html), [Pakelo](https://pakelo.com/en/find-your-oil), [JS Filter](https://www.jsfilter.jp/catalogue/) | Provisional oil or filter rows after decoded make/model/year, or a VIN filter table when the catalogue returns one row | Public read-only lookups. Multiple engines return a clarification and no product. See [fluid connectors](integrations/fluids.md). |
| Partsouq, Megazip, Amayama, Emex | Not connected to live search | Anonymous capture was blocked or did not expose a stable catalog call. Partsouq, 7zap and RealOEM pages can be loaded from operator-recorded HAR files by the [parts catalog crawler](parts-crawler.md). |
| [Parts catalog crawler](parts-crawler.md) (RockAuto, MANN-FILTER, NGK, CatCar, vPIC, FAPI format) | `GET /parts/catalog` and `/parts/alternatives` read the crawled `parts_catalog` schema | Crawled source assertions, always `PROVISIONAL`; never reviewed evidence or a fitment guarantee |
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
