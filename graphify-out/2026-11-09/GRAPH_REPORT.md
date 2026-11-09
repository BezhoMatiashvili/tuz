# Graph Report - tuz  (2026-11-09)

## Corpus Check
- 134 files · ~106,755 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 22 file(s) not represented in the graph (top: (none) 6, .service 5, .timer 4)

## Summary
- 2330 nodes · 5588 edges · 135 communities (84 shown, 51 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 491 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `f1648b90`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Reply Composition & Grounding
- Conversational Chat Tests
- Warehouse Sync Service
- Automotive Docs & Integrations
- .respond
- VIN Federation Tests
- Analytics & Automotive Schema SQL
- Motorium Connector
- Conversation Turn Engine
- automotive/static/app.js
- re
- API Request Models
- CI Workflow
- DB Explorer CLI & Analytics
- Vehicle Inventory Service
- VIN Search Coordinator
- Sync Configuration & CLI
- Engine Matching (OATS)
- Catalog API & VIN Validation
- AnalyticsIntegrationTests
- Parts Assistant Mobile UI
- WarehouseClient
- FastAPI App Routes
- DB Explorer Frontend
- Production Deploy Workflow
- Stock Classification
- Oil Recommendations
- Docker Compose Services
- PublicSourceError
- AutomotiveApiTests
- Evidence Model
- grounding.py
- Local Setup Helper
- Dialogue Code Tests
- VIN List Evaluation
- VehicleTextTests
- Conversation Eval Harness
- parser.py
- Automotive Repository Evidence
- Catalog API Client & CLI
- automotive/repository.py
- Eval Mock Repository
- rules.py
- ConversationStore
- Parts Assistant Desktop UI
- ChatLogger
- service.py
- compose
- Name & Year Matching
- test_automotive_fluids.py
- CatalogApiTests
- Public Oil Catalogs (OATS/Shell/Pakelo)
- .submit
- Chat Texting Desktop UI
- matches_category
- .decode
- Chat Texting Mobile UI
- Automotive UI JS Tests
- Chat UI DOM Stubs
- warehouse_sync/__main__.py
- Conversation Input Identity
- automotive/repository.py
- Chat UI JS Tests
- Catalog Fallback Tests
- Code Index Plumbing Tests
- Warehouse to PostgreSQL synchroniser README
- Automotive Desktop UI
- Automotive Mobile UI
- Automotive UI DOM Stubs
- Enrichment Worker
- _sync_once
- Automotive Postgres Tests
- Automotive intelligence implementation progress
- chat.py
- Chat Log Tests
- analytics Kimball-style star schema
- category_for_query
- Repository Guidelines & Migration Policy
- Chat Log Export
- vin_search.py
- Initial Warehouse Schema
- Durable Indexing Jobs
- Dependabot Config
- _clean
- Eval Mock Model
- Analytics Star Schema Docs
- Runtime Requirements
- Production Settings Tests
- problems
- Parts Assistant Design
- Parts Assistant Chat Panel
- Settings
- Conversation Suite Tests
- Rate Limiter
- ChatLogTests
- Nine-item production release gate
- Sync E2E Source Stub
- Database Not Configured
- Project Metadata
- element
- byId
- test_automotive_vin_search.py
- conversations.py
- test_automotive_chat_log.py
- RepositoryBoundaryTests
- test_analytics_cli.py
- Engine
- PostgresPrivilegeTests
- Automotive intelligence MVP
- _MockSourceHandler
- _E2ESourceHandler
- SyncEndToEndTests

## God Nodes (most connected - your core abstractions)
1. `ConversationalChatTests` - 75 edges
2. `create_app()` - 68 edges
3. `AutomotiveRepository` - 61 edges
4. `FederationTests` - 45 edges
5. `Settings` - 37 edges
6. `ConversationTurn` - 36 edges
7. `VINSearchCoordinator` - 34 edges
8. `PublicSourceError` - 33 edges
9. `category_for_query()` - 32 edges
10. `AutomotiveApiTests` - 31 edges

## Surprising Connections (you probably didn't know these)
- `Truncated Lower Card (Below Fold)` --references--> `Automotive catalog UI page (index.html)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html
- `Collapsible Direct Search & Vehicle Details` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/conversational-parts-mobile.png → automotive/static/index.html
- `Message Composer Textarea` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/conversational-parts-mobile.png → automotive/static/index.html
- `Conversational Parts Assistant Mobile QA Screenshot` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/conversational-parts-mobile.png → automotive/static/index.html
- `Automotive UI Desktop Screenshot` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Product Result Card Anatomy** — docs_automotive_review_chat_texting_mobile_product_result_card, docs_automotive_review_chat_texting_mobile_brand_part_number_line, docs_automotive_review_chat_texting_mobile_stock_availability_line, docs_automotive_review_chat_texting_mobile_match_basis_label, docs_automotive_review_chat_texting_mobile_fitment_warning_callout, docs_automotive_review_chat_texting_mobile_details_disclosure [EXTRACTED 1.00]
- **Production release gating evidence** — docs_production_runbook_release_gate, docs_asvs_l2_evidence, docs_asvs_l2_evidence_required_attachments, docs_production_runbook_restore_drill, docs_production_runbook_github_governance [EXTRACTED 1.00]
- **Unified VIN federation across connected sources** — docs_automotive_unified_vin_search, docs_automotive_nhtsa_vpic, docs_integrations_motorium_motorium_vin_part_code_connector, docs_integrations_fluids_fuchs, docs_integrations_fluids_shell, docs_integrations_fluids_pakelo, docs_integrations_fluids_js_asakashi, docs_automotive_warehouse_products_catalog [EXTRACTED 1.00]
- **Vehicle Context to Compatibility Question Flow** — docs_automotive_review_automotive_desktop_vehicle_card, docs_automotive_review_automotive_desktop_configuration_card, docs_automotive_review_automotive_desktop_ask_catalog_card [INFERRED 0.75]
- **Data Provenance Honesty Pattern** — docs_automotive_review_automotive_mobile_hero, docs_automotive_review_automotive_mobile_source_provenance, docs_automotive_review_automotive_mobile_user_supplied_context [INFERRED 0.75]
- **Conservative evidence-grounded parts answers** — implementation_progress_directional_evidence, implementation_progress_evidence_status_model, implementation_progress_oil_eligibility_gate, implementation_progress_requirement_derived_offers, implementation_progress_reply_grounding [INFERRED 0.85]
- **Evidence-first Source Disclosure Pattern** — docs_automotive_review_automotive_desktop_evidence_disclaimers, docs_automotive_review_automotive_desktop_archived_source_badge, docs_automotive_review_automotive_desktop_part_code_search, docs_automotive_review_automotive_desktop_ask_catalog_card [INFERRED 0.85]
- **VIN Identification Then Configuration Refinement Flow** — docs_automotive_review_automotive_mobile_vin_input, docs_automotive_review_automotive_mobile_optional_year_input, docs_automotive_review_automotive_mobile_identify_button, docs_automotive_review_automotive_mobile_configuration_card [INFERRED 0.85]
- **Conversational Part Lookup Flow** — docs_automotive_review_chat_texting_desktop_quick_reply_chips, docs_automotive_review_chat_texting_desktop_vehicle_slot_filling, docs_automotive_review_chat_texting_desktop_selected_vehicle_sidebar, docs_automotive_review_chat_texting_desktop_result_summary_message, docs_automotive_review_chat_texting_desktop_part_result_card [INFERRED 0.85]
- **Fitment and Stock Trust Signals** — docs_automotive_review_chat_texting_desktop_fitment_disclaimer, docs_automotive_review_chat_texting_desktop_stock_freshness_note, docs_automotive_review_chat_texting_desktop_verify_compatibility_warning, docs_automotive_review_chat_texting_desktop_match_reason [INFERRED 0.85]
- **Chat Part Request to Result Cards Flow** — docs_automotive_review_chat_texting_mobile_user_message_bubble, docs_automotive_review_chat_texting_mobile_vehicle_context, docs_automotive_review_chat_texting_mobile_clarifying_question, docs_automotive_review_chat_texting_mobile_product_result_card [INFERRED 0.85]
- **Data Trust and Verification Signals** — docs_automotive_review_chat_texting_mobile_stock_freshness_disclaimer, docs_automotive_review_chat_texting_mobile_match_basis_label, docs_automotive_review_chat_texting_mobile_fitment_warning_callout [INFERRED 0.85]
- **Conversational Part Lookup Flow** — docs_automotive_review_conversational_parts_desktop_quick_prompts, docs_automotive_review_conversational_parts_desktop_message_composer, docs_automotive_review_conversational_parts_desktop_user_message, docs_automotive_review_conversational_parts_desktop_assistant_reply, docs_automotive_review_conversational_parts_desktop_part_candidate_card [INFERRED 0.85]
- **Unconfirmed Fitment/Stock Signalling Pattern** — docs_automotive_review_conversational_parts_desktop_fitment_unknown_badge, docs_automotive_review_conversational_parts_desktop_stock_price_status, docs_automotive_review_conversational_parts_desktop_supplier_codes_section, docs_automotive_review_conversational_parts_desktop_selected_vehicle_sidebar [INFERRED 0.85]
- **Conversational Part Lookup Flow** — docs_automotive_review_conversational_parts_mobile_message_composer, docs_automotive_review_conversational_parts_mobile_send_button, docs_automotive_review_conversational_parts_mobile_user_message, docs_automotive_review_conversational_parts_mobile_assistant_reply, docs_automotive_review_conversational_parts_mobile_part_candidate_card [INFERRED 0.85]
- **Fitment and Stock Uncertainty Disclosure Pattern** — docs_automotive_review_conversational_parts_mobile_fitment_unknown_badge, docs_automotive_review_conversational_parts_mobile_stock_unconfirmed, docs_automotive_review_conversational_parts_mobile_supplier_codes_section, docs_automotive_review_conversational_parts_mobile_selected_vehicle_card [INFERRED 0.85]
- **Evidence import, review and eligibility trust chain** — docs_automotive_evidence_import, docs_automotive_independent_review, docs_automotive_fitment_status, docs_automotive_oil_eligibility, docs_automotive_migration_0006_automotive [INFERRED 0.85]
- **Grounded chat reply pipeline** — docs_automotive_deterministic_dialogue, docs_automotive_backend_replies, docs_automotive_optional_rephrase, docs_automotive_strict_tool_use, docs_automotive_claude_grounding [INFERRED 0.85]
- **Least-privilege database role model** — agents_least_privilege_model, readme_warehouse_sync_writer_role, readme_backup_restore_operator_role, docs_production_runbook_database_roles, docs_asvs_l2_evidence_v4_access_control [INFERRED 0.85]

## Communities (135 total, 51 thin omitted)

### Community 0 - "Reply Composition & Grounding"
Cohesion: 0.14
Nodes (26): availability(), _axle(), brand_name(), car_label(), _clarify(), compose(), _compose(), engine_choice() (+18 more)

### Community 1 - "Conversational Chat Tests"
Cohesion: 0.09
Nodes (4): ConversationalChatTests, finish(), response(), tool()

### Community 2 - "Warehouse Sync Service"
Cohesion: 0.15
Nodes (7): SyncProgressTests, _html(), ReliabilityTests, _settings(), Settings, SyncCounters, sync()

### Community 3 - "Automotive Docs & Integrations"
Cohesion: 0.20
Nodes (10): ASGI transport test harness with event-loop heartbeat, Automotive API and interface QA (2026-11-08), Blocked browser checks (missing X server, sandbox crashpad), pip-audit vulnerable pins (lxml, pytest, python-dotenv, requests), Self-review scorecard (3.8/5), Unified VIN source search QA (2026-11-09), Warehouse preservation digest check, Selected-vehicle inventory matching (POST /inventory/vehicle) (+2 more)

### Community 4 - ".respond"
Cohesion: 0.26
Nodes (5): tool_definitions(), ChatTests, handler(), response(), tool()

### Community 5 - "VIN Federation Tests"
Cohesion: 0.13
Nodes (3): FluidFederationTests, FederationTests, product()

### Community 6 - "Analytics & Automotive Schema SQL"
Cohesion: 0.07
Nodes (39): warehouse.finish_sync_run(), warehouse.prune_product_versions(), warehouse.record_page_seen(), warehouse.record_product(), warehouse.warehouse_sync_seen_pages, analytics.dim_date, analytics.dim_product, analytics.fact_product_current (+31 more)

### Community 7 - "Motorium Connector"
Cohesion: 0.10
Nodes (9): _identity(), MotoriumClient, MotoriumError, ProductMetadata, _text(), details(), MotoriumTests, response() (+1 more)

### Community 8 - "Conversation Turn Engine"
Cohesion: 0.11
Nodes (4): ConversationTurn, read(), run(), read()

### Community 9 - "automotive/static/app.js"
Cohesion: 0.14
Nodes (20): chatText(), chatWarnings, clearQuickReplies(), hiddenKeys, labels, partCard(), progressLine(), renderChatCards() (+12 more)

### Community 10 - "re"
Cohesion: 0.10
Nodes (19): _bmw_at(), _class_at(), code_question(), _designation_at(), detect_language(), _display(), engine_answer(), engine_details() (+11 more)

### Community 11 - "API Request Models"
Cohesion: 0.12
Nodes (20): CatalogProduct, ConversationCard, ConversationTurn, FluidProduct, FluidReport, JobResult, MotoriumPart, MotoriumResult (+12 more)

### Community 12 - "CI Workflow"
Cohesion: 0.05
Nodes (40): permissions, runs-on, steps, GITHUB_TOKEN, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, WAREHOUSE_TEST_ADMIN_DATABASE_URL (+32 more)

### Community 13 - "DB Explorer CLI & Analytics"
Cohesion: 0.15
Nodes (8): browse(), catalog(), connect(), Handler, json_default(), main(), run_query(), ExplorerTests

### Community 14 - "Vehicle Inventory Service"
Cohesion: 0.13
Nodes (7): _vehicle_scoped(), VehicleInventoryService, _year_scoped(), evidence(), product(), VehicleInventoryTests, page()

### Community 15 - "VIN Search Coordinator"
Cohesion: 0.10
Nodes (9): _key(), same_vehicle(), VINSearchCoordinator, fail(), one(), soft(), start(), start_fluid() (+1 more)

### Community 16 - "Sync Configuration & CLI"
Cohesion: 0.24
Nodes (12): ASVS V14 configuration (fail-closed, SHA-pinned CI), _configuration_error(), _database_url_is_production_safe(), _decode_hmac_key(), _load_environment(), load_maintenance_hmac_key(), load_migration_database_url(), _positive_float() (+4 more)

### Community 17 - "Engine Matching (OATS)"
Cohesion: 0.09
Nodes (21): by_body(), by_engine(), engine_code(), engine_family(), engine_matches(), engine_option(), engine_options(), words() (+13 more)

### Community 18 - "Catalog API & VIN Validation"
Cohesion: 0.24
Nodes (6): categories_for(), _dicts(), parse_articles(), parse_group_ids(), parse_vehicles(), _text()

### Community 20 - "Parts Assistant Mobile UI"
Cohesion: 0.12
Nodes (22): Conversational Parts Assistant Mobile QA Screenshot, Assistant Reply: Catalog Candidates Need Fitment Verification, Bilingual Georgian/English UI Labels, Parts Assistant Chat Panel, Collapsible Direct Search & Vehicle Details, Fitment Unknown Badge, Footer Read-Only Data Disclaimer, Match Source (EXACT_CODE) (+14 more)

### Community 22 - "FastAPI App Routes"
Cohesion: 0.06
Nodes (3): create_app(), oil(), oils()

### Community 23 - "DB Explorer Frontend"
Cohesion: 0.08
Nodes (34): column_name(), export_result(), spreadsheet_value(), text_value(), xml_value(), acceptSuggestion(), activateTab(), api() (+26 more)

### Community 24 - "Production Deploy Workflow"
Cohesion: 0.06
Nodes (30): concurrency, cancel-in-progress, group, environment, name, runs-on, steps, DEPLOY_HOST (+22 more)

### Community 25 - "Stock Classification"
Cohesion: 0.06
Nodes (27): coverage(), engine_family_rows(), _fuel_excludes(), normalize_grade(), oil_rows(), pack_litres(), product_line(), requirements() (+19 more)

### Community 26 - "Oil Recommendations"
Cohesion: 0.16
Nodes (9): _matches_viscosity(), _positive_approval(), RecommendationService, _required(), _signature(), _specific_configuration(), _viscosities(), evidence() (+1 more)

### Community 27 - "Docker Compose Services"
Cohesion: 0.08
Nodes (28): context, dockerfile, build, depends_on, environment, ports, restart, postgres (+20 more)

### Community 28 - "PublicSourceError"
Cohesion: 0.16
Nodes (6): _ambiguous(), _Table, fluid_product(), fluid_report(), PublicSourceError, redact()

### Community 29 - "AutomotiveApiTests"
Cohesion: 0.06
Nodes (6): AutomotiveApiTests, FakeChat, FakeRepository, FakeVin, test_unified_search_uses_provider_rate_budget(), test_unified_vin_read_job_is_typed_pollable_and_private()

### Community 30 - "Evidence Model"
Cohesion: 0.12
Nodes (8): import_evidence(), applies_to(), evaluate_compatibility(), flatten_record(), _same(), verified_record(), parse_import(), EvidenceTests

### Community 31 - "grounding.py"
Cohesion: 0.10
Nodes (10): query_patterns(), displacement_answer(), _claims(), digit_tokens(), language_ok(), names_part_brand(), _norm(), numbers() (+2 more)

### Community 32 - "Local Setup Helper"
Cohesion: 0.12
Nodes (9): configure_local(), local_admin_url(), LocalSetupError, _new_login(), _require_catalog(), main(), main(), LocalSetupTests (+1 more)

### Community 33 - "Dialogue Code Tests"
Cohesion: 0.13
Nodes (5): grades(), part_codes(), CodeTests, LanguageTests, ReviewRegressionTests

### Community 34 - "VIN List Evaluation"
Cohesion: 0.12
Nodes (11): anonymous_misses(), classify(), evaluate(), read_vins(), row_for(), summarize(), write_misses(), run_evaluation() (+3 more)

### Community 36 - "Conversation Eval Harness"
Cohesion: 0.09
Nodes (19): check_turn(), Counting, evaluate(), _fill(), _hide(), judge(), _key(), legacy_language() (+11 more)

### Community 37 - "parser.py"
Cohesion: 0.14
Nodes (6): ASVS V10 malicious input (cell-text parser, bounded response), ParseProductsPageTests, parse_products_page(), _ProductRowsParser, _row_hash(), _value_at()

### Community 38 - "Automotive Repository Evidence"
Cohesion: 0.11
Nodes (3): AutomotiveRepository, sanitized(), _product()

### Community 40 - "automotive/repository.py"
Cohesion: 0.17
Nodes (5): normalize_code(), parse_codes(), PartCode, product_codes(), CodeTests

### Community 42 - "rules.py"
Cohesion: 0.07
Nodes (24): vehicle_inventory(), approvals_stated(), axle_verdict(), _check(), _key(), load_rules(), NeverOffer, _number() (+16 more)

### Community 43 - "ConversationStore"
Cohesion: 0.13
Nodes (4): ConversationStore, cancelled(), progress(), _same()

### Community 44 - "Parts Assistant Desktop UI"
Cohesion: 0.14
Nodes (15): Automotive catalog UI page (index.html), Part code search panel, Source and evidence panel, Fitment, cross-reference and oil panel, Vehicle parts (warehouse) panel, VIN and configuration forms, Conversational Parts Lookup Desktop Screenshot, Auto Catalog Header (cars, parts, fitment) with local service label (+7 more)

### Community 46 - "service.py"
Cohesion: 0.15
Nodes (8): AuthenticationError, failure_code(), SourceChangedError, SourceProtocolError, ParsedPage, _check_page(), _fetch_and_parse(), _page_signature()

### Community 47 - "compose"
Cohesion: 0.25
Nodes (3): card(), compose(), ReplyTests

### Community 48 - "Name & Year Matching"
Cohesion: 0.16
Nodes (8): _key(), _make_names(), make_pattern(), _model_core(), model_pattern(), _two_or_four(), year_fit(), YearFitTests

### Community 49 - "test_automotive_fluids.py"
Cohesion: 0.13
Nodes (10): _covers_year(), FuchsClient, _model_covers_year(), JsFilterClient, ShellOilClient, equipment(), FakeHttp, FuchsTests (+2 more)

### Community 50 - "CatalogApiTests"
Cohesion: 0.21
Nodes (3): CatalogApiTests, client(), FakeCatalogHttp

### Community 51 - "Public Oil Catalogs (OATS/Shell/Pakelo)"
Cohesion: 0.10
Nodes (7): identical(), signature(), _number(), OatsCatalog, distance(), PakeloClient, PublicHttp

### Community 53 - "Chat Texting Desktop UI"
Cohesion: 0.17
Nodes (17): Chat Texting Desktop QA Screenshot, Bilingual Georgian/English Labels, Details Disclosure, Fitment and Stock Disclaimer, Match Reason ('Chosen because'), Car-name Match Fallback, New Chat Button, Part Result Card (+9 more)

### Community 54 - "matches_category"
Cohesion: 0.22
Nodes (5): matches_category(), CategoryPaginationTests, page(), CategoryTests, product()

### Community 55 - ".decode"
Cohesion: 0.19
Nodes (5): ProviderUnavailable, _text(), vin_checksum(), VinService, VinTests

### Community 56 - "Chat Texting Mobile UI"
Cohesion: 0.22
Nodes (16): Chat Texting Mobile QA Screenshot, Assistant Message Bubble, Brand and Part Number Line, Result Count + Front/Rear Clarifying Question, Collapsible Details Disclosure, Fitment Verification Warning Callout, Georgian-Language Chat Localization, Greeting Small-Talk Exchange (+8 more)

### Community 57 - "Automotive UI JS Tests"
Cohesion: 0.15
Nodes (13): assert, calls, context, flush(), fs, html, part, responder() (+5 more)

### Community 59 - "warehouse_sync/__main__.py"
Cohesion: 0.23
Nodes (5): main(), ConfigurationError, main(), _parser(), apply_migrations()

### Community 60 - "Conversation Input Identity"
Cohesion: 0.24
Nodes (8): _code_identity(), conversation_tools(), EmptyInput, _identity(), NameInput, PartInput, ReplyInput, StrictInput

### Community 61 - "automotive/repository.py"
Cohesion: 0.11
Nodes (4): VersionIntegrityTests, sign_unsigned_observations(), verify_version_integrity_tag(), version_integrity_tag()

### Community 62 - "Chat UI JS Tests"
Cohesion: 0.15
Nodes (9): assert, calls, ctx, fixture, fs, html, responder(), timers (+1 more)

### Community 63 - "Catalog Fallback Tests"
Cohesion: 0.17
Nodes (12): ChatRequest, ConversationMessage, fitments(), IdentifyRequest, ImportRequest, JobRequest, MotoriumLookupRequest, StrictModel (+4 more)

### Community 65 - "Warehouse to PostgreSQL synchroniser README"
Cohesion: 0.16
Nodes (14): Warehouse analytics guide, Owner-only idempotent analytics refresh/backfill, OWASP ASVS Level 2 release evidence matrix, ASVS V4 access control (scoped writer functions), ASVS V6 cryptography (TLS verify-full, external HMAC key), ASVS V7 error handling (safe failure codes), ASVS V8 data protection (no raw HTML, private schema, 24-month pruning), Secure production runbook (+6 more)

### Community 66 - "Automotive Desktop UI"
Cohesion: 0.23
Nodes (13): Automotive UI Desktop Screenshot, Archived Source Badge, Ask the Catalog (Free-text Question), Configuration Refinement Card, Evidence-first Disclaimer Pattern, Georgian-language Interface, Header: Autocatalog Brand (Vehicle · Parts · Evidence), Hero: Find the Part. Check Compatibility. (+5 more)

### Community 67 - "Automotive Mobile UI"
Cohesion: 0.19
Nodes (13): Automotive UI Mobile QA Screenshot, Autocatalog Brand Header (vehicle · parts · evidence), Manufacturer, Model, Year, Engine Code Fields, Configuration Refinement Card, Truncated Field (likely Fuel), Hero: Find the Part, Check Compatibility, Identify Vehicle Button, Year Input (optional) (+5 more)

### Community 70 - "_sync_once"
Cohesion: 0.16
Nodes (5): WarehouseProduct, WarehouseRepository, _log_progress(), _sync_once(), SyncResult

### Community 72 - "Automotive intelligence implementation progress"
Cohesion: 0.16
Nodes (8): Repository Guidelines (AGENTS.md), CI checks: ruff, bandit, pip-audit, unittest, PostgreSQL security/E2E tests (WAREHOUSE_TEST_ADMIN_DATABASE_URL), Automotive intelligence implementation progress, Private automotive schema migration 0006, Conversation evaluation harness (46 bilingual scenarios), automotive --setup-local helper, Automotive intelligence application

### Community 73 - "chat.py"
Cohesion: 0.16
Nodes (8): CodeInput, EmptyInput, PresentInput, _days(), _flag(), _seconds(), Server-side Claude orchestration with strict read tools, anthropic==0.72.0

### Community 74 - "Chat Log Tests"
Cohesion: 0.25
Nodes (9): Backend-built replies, Deterministic dialogue (answered without model call), Ephemeral in-memory chat conversations, Optional reply rewording (AUTOMOTIVE_CHAT_REPHRASE), Messenger bubbles and quick-reply chips, Response model dropped note / quick_replies / act / language (fixed), Texting-style parts chat UI verification, Conversational parts assistant UI verification (+1 more)

### Community 75 - "analytics Kimball-style star schema"
Cohesion: 0.19
Nodes (11): Parts assistant chat panel, dim_date, dim_product (Type 1), fact_product_current, fact_product_observation, fact_sync_run, analytics Kimball-style star schema, Conversational parts assistant (+3 more)

### Community 77 - "Repository Guidelines & Migration Policy"
Cohesion: 0.25
Nodes (11): NHTSA vPIC VIN decoder, Oil-change flow (grade, capacity, engine family), Unified VIN read federation (/vehicles/search), Fuchs oil chooser connector, JS Asakashi (jsfilter.jp) VIN filter connector, OATS chooser API shape (search / equipment), Pakelo oil chooser connector (per-lookup page token), Public oil and filter connectors (+3 more)

### Community 78 - "Chat Log Export"
Cohesion: 0.33
Nodes (4): Optional TecDoc-style catalog API (CATALOG_API_KEY), Name-match fallback (NAME_MATCH_ONLY), 24-hour shared caches and prewarm, Stock source columns (store column_10 / TUZ 11 column_12)

### Community 79 - "vin_search.py"
Cohesion: 0.11
Nodes (12): _json(), _make_matches(), identity(), Motorium VIN part-code panel, Unified all-sources VIN search panel, validate_vin(), Key-gated VIN-to-OE catalog API client (RapidAPI), Motorium VIN-to-part-code connector (+4 more)

### Community 80 - "Initial Warehouse Schema"
Cohesion: 0.42
Nodes (8): warehouse_product_versions, warehouse_product_versions_product_time_idx, warehouse_products, warehouse_products_active_idx, warehouse_products_last_seen_run_idx, warehouse_products_product_code_idx, warehouse_schema_migrations, warehouse_sync_runs

### Community 81 - "Durable Indexing Jobs"
Cohesion: 0.29
Nodes (6): Conservative part-code normalization (retains '/'), Stock-independent content hash (migration 0007), Enrichment index worker (leases, SKIP LOCKED, checkpoints), automotive.part_numbers code index, Migration 0007 content_hash code index, Durable indexing jobs with leases and checkpoints

### Community 82 - "Dependabot Config"
Cohesion: 0.25
Nodes (7): interval, updates, directory, open-pull-requests-limit, package-ecosystem, schedule, version

### Community 83 - "_clean"
Cohesion: 0.15
Nodes (7): _clean(), continues(), references(), small_talk(), starts_with_greeting(), ReferenceTests, SmallTalkTests

### Community 85 - "Analytics Star Schema Docs"
Cohesion: 0.29
Nodes (7): Authorized evidence import (CSV/JSON operator import), Compatibility statuses (VERIFIED / PROVISIONAL / CONFLICTED / UNKNOWN), Hard offer rules (always on), Deterministic oil eligibility, Live federation acceptance run, Staff rules file (automotive/rules.toml), Provisional supplier results (no automatic evidence approval)

### Community 86 - "Runtime Requirements"
Cohesion: 0.29
Nodes (7): Runtime requirements pin list, fastapi==0.155.0, httpx==0.28.1, psycopg[binary]==3.3.2, python-dotenv==1.2.1, requests==2.32.5, uvicorn==0.54.0

### Community 88 - "problems"
Cohesion: 0.25
Nodes (4): facts_text(), problems(), check(), GroundingTests

### Community 89 - "Parts Assistant Design"
Cohesion: 0.29
Nodes (5): Local SQLite chat log, Migration 0006_automotive (private schema with RLS), Ephemeral HMAC-keyed search snapshot, --setup-local local provisioning helper, 30-day sanitized VIN decode cache

### Community 90 - "Parts Assistant Chat Panel"
Cohesion: 0.17
Nodes (15): Assistant Reply: catalog candidates, fitment needs verification, Bilingual Georgian / English UI Labels, Parts Assistant Chat Panel, Conservative Stock and Fitment Confirmation Pattern, Fitment Unknown Badge, Message Composer (textarea, Send button, Ctrl/Cmd+Enter hint), New Chat Button, Part Candidate Card (Front brake pads, main/matched codes, match source) (+7 more)

### Community 91 - "Settings"
Cohesion: 0.21
Nodes (5): ChatService, _dump(), Settings, mock_store(), MockVinService

### Community 92 - "Conversation Suite Tests"
Cohesion: 0.33
Nodes (4): Automotive /api/v1 endpoint surface, Per-process rate limits (60 API / 10 provider requests per minute), Motorium ai-assistant.php protocol, Product JSON-LD metadata (MPN, SKU, OEM)

### Community 95 - "Nine-item production release gate"
Cohesion: 0.18
Nodes (10): Required release attachments (pentest, SBOM, restore drill, sign-off), ~$28.50/month baseline and $28 billing alert, Encrypted off-host backups (pg_dump, age, rclone), GitHub governance (CODEOWNERS, branch protection, production environment, CodeQL, Dependabot), Nine-item production release gate, Isolated quarterly restore drill, Supabase Frankfurt project with private warehouse schema and IP restriction, systemd encrypted credentials and network sandbox (+2 more)

### Community 120 - "element"
Cohesion: 0.47
Nodes (11): api(), element(), evidenceLink(), facts(), loadVehicleParts(), perform(), product(), render() (+3 more)

### Community 121 - "byId"
Cohesion: 0.33
Nodes (11): byId(), chatMessage(), clearDependentResults(), clearVinContext(), deleteConversation(), invalidateUnified(), readVehicle(), resetChat() (+3 more)

### Community 122 - "test_automotive_vin_search.py"
Cohesion: 0.35
Nodes (4): merge_parts(), MergeTests, supplier(), warehouse()

### Community 123 - "conversations.py"
Cohesion: 0.22
Nodes (3): _redact(), ConversationError, _shown()

### Community 130 - "Automotive intelligence MVP"
Cohesion: 0.50
Nodes (3): Anthropic API (language/tool orchestration), Automotive intelligence MVP, Strict tool schemas and finish_response contract

## Ambiguous Edges - Review These
- `Automotive catalog UI page (index.html)` → `Truncated Lower Card (Below Fold)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png · relation: references
- `Configuration Refinement Card` → `Truncated Field (likely Fuel)`  [AMBIGUOUS]
  docs/automotive-review/automotive-mobile.png · relation: references

## Knowledge Gaps
- **50 isolated node(s):** `warehouse-sync`, `Match Source (EXACT_CODE)`, `New Chat Button`, `Automotive Catalog Header and Hero Prompt`, `Result Count / Pagination Indicator` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 663 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **51 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Automotive intelligence implementation progress` connect `Automotive intelligence implementation progress` to `Warehouse to PostgreSQL synchroniser README`, `automotive/repository.py`, `API Request Models`, `Parts Assistant Desktop UI`, `FastAPI App Routes`, `Settings`, `automotive/repository.py`, `Nine-item production release gate`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `ConversationalChatTests` (e.g. with `ChatService` and `Settings`) actually correct?**
  _`ConversationalChatTests` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `warehouse-sync`, `Match Source (EXACT_CODE)`, `New Chat Button` to the rest of the system?**
  _50 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Reply Composition & Grounding` be split into smaller, more focused modules?**
  _Cohesion score 0.14035087719298245 - nodes in this community are weakly interconnected._
- **Why does `Automotive catalog UI page (index.html)` connect `Parts Assistant Desktop UI` to `Automotive Desktop UI`, `Automotive Mobile UI`, `Automotive intelligence implementation progress`, `automotive/static/app.js`, `analytics Kimball-style star schema`, `vin_search.py`, `Parts Assistant Mobile UI`, `Chat Texting Desktop UI`, `Chat Texting Mobile UI`, `Parts Assistant Chat Panel`?**
  _High betweenness centrality (0.072) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Automotive catalog UI page (index.html)` and `Truncated Lower Card (Below Fold)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Configuration Refinement Card` and `Truncated Field (likely Fuel)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._