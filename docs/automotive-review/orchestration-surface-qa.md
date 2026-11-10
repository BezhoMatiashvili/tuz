# AI orchestration: conversation and browser verification

The AI-led path is selected by `AUTOMOTIVE_CHAT_ORCHESTRATION=true`, the default
when configuration is loaded from the environment. Set the flag to `false` to
restore legacy routing. Direct `Settings()` construction retains the legacy default
for existing integrations and tests. `AUTOMOTIVE_CACHE_PATH` defaults to
`data/automotive-cache.sqlite3`; the catalog/research cache does not hold stock.

The conversation store supplies a 30-second turn deadline, carries separately stated
vehicle details, source-supported candidates, selected configuration, and the unresolved
question to the next turn. The model receives the original redacted text rather than
depending on the deterministic vehicle parser. Corrections can supersede running turns.
Old workers and lower vehicle revisions cannot replace the current result. Form changes
clear dependent results across make, model, year, engine, displacement, fuel, trim, body,
drive, transmission, and market. Deadline expiry retains successful offers or source
findings and finalizes the polling response.

API response additions are optional: `vehicle_revision`, `resolution_candidates`,
`selected_configuration`, `clarification_choices`, `claims`, and `citations`.
Internal stated-detail evidence and pending dialogue remain server-side. Vehicle request
configuration accepts numeric `displacement_l` greater than zero and at most 20 litres,
and accepts both `fuel` and `fuel_type`.

Validated findings appear in a collapsible sources section. Links accept HTTPS only,
reject credential-bearing or executable schemes, and open with `noopener noreferrer`.
All names, source text, candidate labels, and supplier identifiers render as text.
Supplier-only findings remain separate from warehouse offers. Trim and displacement
are preserved in the vehicle form and subsequent requests.

Verification performed:

- `python -m unittest tests.test_automotive_conversations -v`: 23 tests passed,
  including new state persistence, superseding corrections, revision rejection,
  deadline, displacement validation, and response serialization checks.
- `ruff check automotive/config.py automotive/conversations.py automotive/api.py
  tests/test_automotive_conversations.py`: passed.
- `node tests/automotive_chat_ui_test.cjs` and
  `node tests/automotive_ui_test.cjs`: passed. The added checks cover citation scheme
  filtering, text-only statements/excerpts, backend-labelled oil assertions, unconfirmed
  vehicle applicability, source-only progressive findings, and vehicle-variant labels.
- `python -m unittest tests.test_ai_orchestration.StoredAIConversationTests -v`: four
  additional real-store/orchestrator regressions passed. They cover scattered Georgian
  details, a short fuel reply, a bare engine-code reply, and vehicle switching while
  keeping supplier-selected engines separate from customer statements. The year/size
  and bare-engine cases initially reproduced erroneous direct part-code routing.
- A real headless Chrome session with **synthetic intercepted API responses** checked
  1440×1000 desktop and 390×844 mobile layouts, progressive polling, recognized vehicle
  details, displacement preservation, supplier separation, and safe source links.
  No horizontal overflow or JavaScript page errors were observed. This verifies the
  interface, not live provider availability or technical facts.

Screenshots: [desktop](orchestration-desktop.png), [mobile](orchestration-mobile.png).
