# Motorium VIN / part-code connector

Observed 2026-11-09 against public https://motorium.ge/. No login, cart, purchase,
contact submission, access bypass, paid subscription or warehouse mutation was used.

## Run it

Restart the automotive server after updating source:

```bash
uv run python -m automotive
```

In the new **VIN-ით ნაწილის კოდები · Motorium** panel, reuse the VIN field above
and enter a part description, e.g. `სამუხრუჭე ხუნდი`. Click the lookup button. A result
shows manufacturer MPN, the supplier's own SKU, and values explicitly labeled OEM in
the product metadata. The source link and source fitment description are shown.
**MPN-ით ძიება ჩემს საწყობში** searches the local catalog using that MPN/manufacturer;
Motorium quantities/prices are never represented as this warehouse's own stock/prices.

Local API:

```text
POST /api/v1/vehicles/part-codes
Content-Type: application/json

{"vin":"<replace with a 17-character VIN>","part_query":"სამუხრუჭე ხუნდი","limit":6}
```

`part_query` is optional, but source results are limited and a specific part often is
needed. `limit` is 1–6. This is not a complete manufacturer EPC / all-parts-by-VIN API.
It wraps the public supplier assistant and its current catalog coverage.

Direct Python client (VIN is not put in shell arguments/history):

```python
from getpass import getpass
from automotive.motorium import MotoriumClient

result = MotoriumClient().lookup(getpass("VIN: "), "სამუხრუჭე ხუნდი", limit=2)
for item in result["items"]:
    print(item["manufacturer"], item["mpn"], item["oem_codes"], item["source_url"])
```

No local Anthropic key or Motorium API key is needed for this observed anonymous
protocol. `MOTORIUM_ENABLED=false` disables the application endpoint. Existing server
configuration still applies to the rest of the app. New lookup requests use the existing
provider rate limit. There is no automatic retry, and access/rate-limit failures are
reported rather than bypassed.

## Reverse-engineered protocol

The public frontend [`/js/ai-assistant.js?v=20261106a`](https://motorium.ge/js/ai-assistant.js?v=20261106a)
uses:

- `POST https://motorium.ge/api/ai-assistant.php`
- JSON: `message`, `history` (last 8 messages), `context` (`vehicle`, `part`).
- Headers: JSON content type and `X-Requested-With: XMLHttpRequest`; origin/referer
  identify the same public site. Session cookies are held only in a fresh requests
  Session for each lookup, never hardcoded or persisted by this connector.
- Response: `reply`, `vehicle`, `products`, `context`, `ai_enabled`.
- Product record: `id`, `sap`, `mpn`, `name`, `brand`, `price`, `quantity`.
- Product details: `GET /product.php?id=<numeric id>`; `application/ld+json` Product
  metadata has `mpn`, `sku`, brand and `additionalProperty` entries named `OEM` and
  `Compatible vehicles`.

Our wrapper sends the VIN explicitly on **every** fresh lookup with empty history and
null context. It does not accept arbitrary history, URLs, cookies or provider context
from callers. Captured context-only follow-up requests lost VIN identity and returned
unrelated generic products, so they are deliberately not reused.

The assistant's prose is discarded. In the live Audi lookup it stated that OEM was
absent, but the actual product metadata contained an OEM-labeled value. The connector
therefore reads structured catalog MPNs and detail metadata, not numbers in model text.
Supplier SKU is kept separate from MPN and the source's OEM field. An OEM label on a
retailer page is not independent proof that a value is a manufacturer-issued OEM code;
industry/reference codes may use that field too.

## Validation and evidence boundaries

- VIN format validated; VINs never appear in returned JSON, source URLs or logs.
- VIN-decoded vehicle make/model/year must match the assistant's selected catalog
  context. A mismatched echoed frame/VIN also blocks results.
- An observed public test decoded Honda Accord but selected Honda S2000 in catalog
  context. The wrapper returns `CONFLICTED`, no part codes, and a clarification for
  this case. Missing VIN context cannot turn generic products into VIN results.
- Part-specific queries require a structured selected-part context; otherwise the
  wrapper asks for clarification.
- Product detail MPN/SKU/brand must agree before merging its OEM field. Disagreement
  is `CONFLICTED`; unavailable details leave OEM unknown while preserving catalog MPN.
- Supplier catalog results stay `PROVISIONAL`. They are not independently reviewed
  vehicle fitment, OEM approval, oil eligibility or actual warehouse availability.
  No evidence is automatically imported or approved. Public access is not a recorded
  redistribution licence: reuse permission still needs operator verification before
  a separately licensed feed is persisted or republished at scale.
- Fixed HTTPS origin/routes, numeric IDs, 2 MB streamed caps, timeouts, no redirects,
  no arbitrary network tools. API inputs and response records are typed and bounded.

## Plugin execution and live acceptance

The explicitly requested `reverse-api-engineer` skill was read and CLI
0.14.0 used: version/list/preflight passed; no earlier Motorium capture existed.
Its default agent provider was `auto` (fresh Playwright), Python output, Claude SDK.
A normal capture failed because global history storage was read-only. A process-local
state-path override under `/tmp` avoided global settings changes and started run
`68f6f286ca71`, but it emitted no capture/client/usage result and was interrupted.
There is no successful plugin HAR/generated-client claim. Model cost was not reported
and is **unknown**, not asserted zero. No capture is left running.

The checked-in connector was implemented from public frontend source and confirmed
real HTTP exchanges. Private raw requests/responses and cookies are kept only in the
restricted `/tmp/tuz-motorium-rae` work directory and are not committed, linked here,
or required at runtime. Do not copy them into tickets, documentation or source.

Live checks:

- Public test fixture lookup: HTTP 200; mismatched Honda identity/context reproduced.
- Audi brake-pad lookup: HTTP 200; structured MPN `PN0477W`, supplier SKU `30001573`.
- Product [29845](https://motorium.ge/product.php?id=29845): JSON-LD MPN matches;
  source's OEM field contains `D1322-8434`; fitment text names multiple Audi variants.
- Actual Python client returned `PROVISIONAL` and those codes; response validates
  against the typed local API schema. Actual VIN intentionally omitted from records.
- The actual local POST handler (ASGI transport with real outbound HTTP) also returned
  HTTP 200 and the same provisional codes. This is not a browser/TCP E2E test.
- Full regression: 146 tests, 133 passed, 13 skipped; new-code Ruff, backend Bandit,
  JS syntax and lock consistency pass.
- Unit tests use anonymized fixtures, covering mismatches, generic fallback, model-prose
  rejection, code separation, details failures, limits, redaction, rate/access failures.

Respect the site's [terms](https://motorium.ge/terms.php), rate limits and changing
coverage. Motorium's own terms require verifying codes and compatibility before order;
this connector does not provide a fitment guarantee or an official provider contract.

### Unified VIN discovery

The main VIN form now also uses this connector through `/api/v1/vehicles/search`.
A blank part description runs up to eight predefined category lookups; specifying
a part runs one. Every category still repeats VIN with empty history/context and
passes the same identity gates. One shared client caps assistant lookups at ten
per minute across federation/direct endpoints; no retries on access/rate errors.
The overall read job is limited to 75 seconds and reserves time for local matching,
so unvisited categories and provider failures remain partial. Repeated searches
reuse five-minute in-memory HMAC snapshots. Results are not persisted as verified
evidence and do not expand Motorium's observed reuse rights or source coverage.
See [unified-search operation](../automotive.md#finding-inventory-for-the-selected-car).
