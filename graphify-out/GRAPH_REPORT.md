# Graph Report - tuz  (2026-11-09)

## Corpus Check
- 134 files · ~104,921 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 22 file(s) not represented in the graph (top: (none) 6, .service 5, .timer 4)

## Summary
- 2287 nodes · 5476 edges · 114 communities (79 shown, 35 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 487 edges (avg confidence: 0.86)
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
- Settings
- VIN Federation Tests
- Analytics & Automotive Schema SQL
- Motorium Connector
- Conversation Turn Engine
- Automotive Frontend (app.js)
- re
- API Request Models
- CI Workflow
- DB Explorer CLI & Analytics
- Vehicle Inventory Service
- VIN Search Coordinator
- Sync Configuration & CLI
- Engine Matching (OATS)
- Catalog API & VIN Validation
- unittest
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
- Oil Requirement Matching
- Local Setup Helper
- Dialogue Code Tests
- VIN List Evaluation
- read_vehicle
- Conversation Eval Harness
- parser.py
- Automotive Repository Evidence
- Catalog API Client & CLI
- automotive/repository.py
- Eval Mock Repository
- Rules File Loading
- Hard Rule Tests
- Parts Assistant Desktop UI
- Chat Logger
- load_scenarios
- Conversation Store Tests
- Name & Year Matching
- test_automotive_fluids.py
- CatalogApiTests
- Public Oil Catalogs (OATS/Shell/Pakelo)
- run_chat_evaluation
- Chat Texting Desktop UI
- matches_category
- recommendations.py
- Chat Texting Mobile UI
- Automotive UI JS Tests
- Chat UI DOM Stubs
- automotive/__main__.py
- Conversation Input Identity
- Version Integrity (HMAC)
- Chat UI JS Tests
- Catalog Fallback Tests
- Code Index Plumbing Tests
- Federated one-VIN search across sources
- Automotive Desktop UI
- Automotive Mobile UI
- Automotive UI DOM Stubs
- Enrichment Worker
- Rules Engine
- Automotive Postgres Tests
- Production Release Gate
- _Table
- Chat Log Tests
- Conversational parts assistant
- Repository Guidelines & Migration Policy
- Chat Log Export
- Conversation Errors
- Initial Warehouse Schema
- Durable Indexing Jobs
- Dependabot Config
- Oil Approval Tests
- Eval Mock Model
- Analytics Star Schema Docs
- Runtime Requirements
- Production Settings Tests
- Parts Assistant Design
- Conversation Suite Tests
- Rate Limiter
- Sync E2E Source Stub
- Database Not Configured
- Project Metadata

## God Nodes (most connected - your core abstractions)
1. `ConversationalChatTests` - 75 edges
2. `create_app()` - 68 edges
3. `AutomotiveRepository` - 61 edges
4. `FederationTests` - 45 edges
5. `Settings` - 39 edges
6. `ConversationTurn` - 39 edges
7. `VINSearchCoordinator` - 34 edges
8. `category_for_query()` - 32 edges
9. `AutomotiveApiTests` - 31 edges
10. `PublicSourceError` - 31 edges

## Surprising Connections (you probably didn't know these)
- `Truncated Lower Card (Below Fold)` --references--> `Automotive catalog UI page (index.html)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html
- `Collapsible Direct Search & Vehicle Details` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/conversational-parts-mobile.png → automotive/static/index.html
- `Automotive UI Desktop Screenshot` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html
- `Automotive UI Mobile QA Screenshot` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/automotive-mobile.png → automotive/static/index.html
- `Configuration Refinement Card` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/automotive-mobile.png → automotive/static/index.html

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

## Communities (114 total, 35 thin omitted)

### Community 0 - "Reply Composition & Grounding"
Cohesion: 0.13
Nodes (28): availability(), _axle(), brand_name(), car_label(), _clarify(), compose(), _compose(), _greet() (+20 more)

### Community 1 - "Conversational Chat Tests"
Cohesion: 0.09
Nodes (4): ConversationalChatTests, finish(), response(), tool()

### Community 2 - "Warehouse Sync Service"
Cohesion: 0.08
Nodes (16): SyncProgressTests, _html(), ReliabilityTests, _settings(), Settings, failure_code(), ParsedPage, SyncCounters (+8 more)

### Community 3 - "Automotive Docs & Integrations"
Cohesion: 0.29
Nodes (9): Automotive intelligence MVP, Hard offer rules (always on), pip-audit vulnerable pins (lxml, pytest, python-dotenv, requests), Self-review scorecard (3.8/5), Unified VIN source search QA (2026-11-09), Warehouse preservation digest check, Staff rules file (automotive/rules.toml), Unified VIN read federation (/vehicles/search) (+1 more)

### Community 4 - "Settings"
Cohesion: 0.08
Nodes (18): ChatService, CodeInput, _dump(), EmptyInput, PresentInput, tool_definitions(), _days(), _flag() (+10 more)

### Community 5 - "VIN Federation Tests"
Cohesion: 0.10
Nodes (7): merge_parts(), FluidFederationTests, FederationTests, MergeTests, product(), supplier(), warehouse()

### Community 6 - "Analytics & Automotive Schema SQL"
Cohesion: 0.07
Nodes (39): warehouse.finish_sync_run(), warehouse.prune_product_versions(), warehouse.record_page_seen(), warehouse.record_product(), warehouse.warehouse_sync_seen_pages, analytics.dim_date, analytics.dim_product, analytics.fact_product_current (+31 more)

### Community 7 - "Motorium Connector"
Cohesion: 0.10
Nodes (9): _identity(), MotoriumClient, MotoriumError, ProductMetadata, _text(), details(), MotoriumTests, response() (+1 more)

### Community 8 - "Conversation Turn Engine"
Cohesion: 0.08
Nodes (4): ConversationTurn, read(), run(), read()

### Community 9 - "Automotive Frontend (app.js)"
Cohesion: 0.13
Nodes (42): api(), byId(), chatMessage(), chatText(), chatWarnings, clearDependentResults(), clearQuickReplies(), clearVinContext() (+34 more)

### Community 10 - "re"
Cohesion: 0.09
Nodes (17): category_for_query(), query_patterns(), _clean(), code_question(), continues(), detect_language(), engine_answer(), _georgian_form() (+9 more)

### Community 11 - "API Request Models"
Cohesion: 0.12
Nodes (20): CatalogProduct, ConversationCard, ConversationTurn, FluidProduct, FluidReport, JobResult, MotoriumPart, MotoriumResult (+12 more)

### Community 12 - "CI Workflow"
Cohesion: 0.05
Nodes (40): permissions, runs-on, steps, GITHUB_TOKEN, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, WAREHOUSE_TEST_ADMIN_DATABASE_URL (+32 more)

### Community 13 - "DB Explorer CLI & Analytics"
Cohesion: 0.11
Nodes (10): main(), browse(), catalog(), connect(), Handler, json_default(), main(), run_query() (+2 more)

### Community 14 - "Vehicle Inventory Service"
Cohesion: 0.13
Nodes (7): _vehicle_scoped(), VehicleInventoryService, _year_scoped(), evidence(), product(), VehicleInventoryTests, page()

### Community 15 - "VIN Search Coordinator"
Cohesion: 0.10
Nodes (9): _key(), same_vehicle(), VINSearchCoordinator, fail(), one(), soft(), start(), start_fluid() (+1 more)

### Community 16 - "Sync Configuration & CLI"
Cohesion: 0.16
Nodes (15): ASVS V14 configuration (fail-closed, SHA-pinned CI), _configuration_error(), _database_url_is_production_safe(), _decode_hmac_key(), _load_environment(), load_maintenance_hmac_key(), load_migration_database_url(), _positive_float() (+7 more)

### Community 17 - "Engine Matching (OATS)"
Cohesion: 0.14
Nodes (12): by_body(), by_engine(), engine_code(), engine_family(), engine_matches(), fuel_matches(), narrow(), trim_groups() (+4 more)

### Community 18 - "Catalog API & VIN Validation"
Cohesion: 0.27
Nodes (6): categories_for(), _dicts(), parse_articles(), parse_group_ids(), parse_vehicles(), _text()

### Community 19 - "unittest"
Cohesion: 0.08
Nodes (5): AnalyticsIntegrationTests, _E2ESourceHandler, SyncEndToEndTests, apply_migrations(), apply_migrations_on_connection()

### Community 20 - "Parts Assistant Mobile UI"
Cohesion: 0.12
Nodes (22): Conversational Parts Assistant Mobile QA Screenshot, Assistant Reply: Catalog Candidates Need Fitment Verification, Bilingual Georgian/English UI Labels, Parts Assistant Chat Panel, Collapsible Direct Search & Vehicle Details, Fitment Unknown Badge, Footer Read-Only Data Disclaimer, Match Source (EXACT_CODE) (+14 more)

### Community 21 - "WarehouseClient"
Cohesion: 0.11
Nodes (7): _MockSourceHandler, WarehouseClientTests, WarehouseClient, AuthenticationError, SourceChangedError, SourceProtocolError, _check_page()

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
Cohesion: 0.11
Nodes (12): age_hours(), availability_text(), classify(), is_stale(), _number(), parse_count(), ChooserResolutionTests, mercedes() (+4 more)

### Community 27 - "Docker Compose Services"
Cohesion: 0.08
Nodes (28): context, dockerfile, build, depends_on, environment, ports, restart, postgres (+20 more)

### Community 28 - "PublicSourceError"
Cohesion: 0.16
Nodes (12): _ambiguous(), _covers_year(), FuchsClient, _make_matches(), _model_covers_year(), _model_matches(), fluid_product(), fluid_report() (+4 more)

### Community 29 - "AutomotiveApiTests"
Cohesion: 0.05
Nodes (9): _text(), VinService, AutomotiveApiTests, FakeChat, FakeRepository, FakeVin, test_unified_search_uses_provider_rate_budget(), test_unified_vin_read_job_is_typed_pollable_and_private() (+1 more)

### Community 30 - "Evidence Model"
Cohesion: 0.12
Nodes (8): import_evidence(), applies_to(), evaluate_compatibility(), flatten_record(), _same(), verified_record(), parse_import(), EvidenceTests

### Community 31 - "Oil Requirement Matching"
Cohesion: 0.14
Nodes (13): coverage(), engine_family_rows(), _fuel_excludes(), normalize_grade(), oil_rows(), pack_litres(), product_line(), requirements() (+5 more)

### Community 32 - "Local Setup Helper"
Cohesion: 0.15
Nodes (6): configure_local(), local_admin_url(), LocalSetupError, _new_login(), _require_catalog(), LocalSetupTests

### Community 33 - "Dialogue Code Tests"
Cohesion: 0.11
Nodes (8): _redact(), grades(), part_codes(), references(), CodeTests, LanguageTests, ReferenceTests, ReviewRegressionTests

### Community 34 - "VIN List Evaluation"
Cohesion: 0.16
Nodes (8): anonymous_misses(), classify(), evaluate(), read_vins(), row_for(), summarize(), write_misses(), EvaluatorTests

### Community 35 - "read_vehicle"
Cohesion: 0.13
Nodes (6): _same(), displacement_answer(), _display(), read_vehicle(), TypedVehicle, VehicleTextTests

### Community 36 - "Conversation Eval Harness"
Cohesion: 0.12
Nodes (13): check_turn(), Counting, evaluate(), _fill(), _hide(), _key(), legacy_language(), _product() (+5 more)

### Community 37 - "parser.py"
Cohesion: 0.10
Nodes (11): OWASP ASVS Level 2 release evidence matrix, ASVS V10 malicious input (cell-text parser, bounded response), ASVS V6 cryptography (TLS verify-full, external HMAC key), ASVS V7 error handling (safe failure codes), ASVS V8 data protection (no raw HTML, private schema, 24-month pruning), Legacy 0.1 upgrade: migrate through 0002 and sign legacy versions, source_columns JSONB text-only storage, ParseProductsPageTests (+3 more)

### Community 38 - "Automotive Repository Evidence"
Cohesion: 0.08
Nodes (5): AutomotiveRepository, sanitized(), _evidence(), _product(), RepositoryBoundaryTests

### Community 40 - "automotive/repository.py"
Cohesion: 0.15
Nodes (5): normalize_code(), parse_codes(), PartCode, product_codes(), CodeTests

### Community 42 - "Rules File Loading"
Cohesion: 0.18
Nodes (10): _check(), load_rules(), _number(), parse_rules(), RulesError, _text(), _texts(), _year() (+2 more)

### Community 43 - "Hard Rule Tests"
Cohesion: 0.13
Nodes (8): approvals_stated(), axle_verdict(), NeverOffer, oem_code_ok(), StaffConfirmed, vehicle_consistent(), DataClassTests, HardRuleTests

### Community 44 - "Parts Assistant Desktop UI"
Cohesion: 0.08
Nodes (30): Automotive catalog UI page (index.html), Part code search panel, Source and evidence panel, Fitment, cross-reference and oil panel, Vehicle parts (warehouse) panel, VIN and configuration forms, Conversational Parts Lookup Desktop Screenshot, Auto Catalog Header (cars, parts, fitment) with local service label (+22 more)

### Community 45 - "Chat Logger"
Cohesion: 0.05
Nodes (10): ChatLogger, _text(), ConversationError, ConversationStore, cancelled(), progress(), _shown(), ChatLogTests (+2 more)

### Community 46 - "load_scenarios"
Cohesion: 0.29
Nodes (3): load_scenarios(), read_vins(), ConversationSuiteTests

### Community 47 - "Conversation Store Tests"
Cohesion: 0.08
Nodes (15): _claims(), digit_tokens(), facts_text(), language_ok(), names_part_brand(), _norm(), numbers(), part_brand() (+7 more)

### Community 48 - "Name & Year Matching"
Cohesion: 0.16
Nodes (8): _key(), _make_names(), make_pattern(), _model_core(), model_pattern(), _two_or_four(), year_fit(), YearFitTests

### Community 49 - "test_automotive_fluids.py"
Cohesion: 0.22
Nodes (6): JsFilterClient, ShellOilClient, equipment(), FakeHttp, JsFilterTests, OatsTests

### Community 50 - "CatalogApiTests"
Cohesion: 0.21
Nodes (3): CatalogApiTests, client(), FakeCatalogHttp

### Community 51 - "Public Oil Catalogs (OATS/Shell/Pakelo)"
Cohesion: 0.13
Nodes (5): identical(), signature(), _number(), OatsCatalog, PakeloClient

### Community 52 - "run_chat_evaluation"
Cohesion: 0.19
Nodes (6): judge(), live_store(), print_summary(), summarize(), run_chat_evaluation(), run_evaluation()

### Community 53 - "Chat Texting Desktop UI"
Cohesion: 0.17
Nodes (17): Chat Texting Desktop QA Screenshot, Bilingual Georgian/English Labels, Details Disclosure, Fitment and Stock Disclaimer, Match Reason ('Chosen because'), Car-name Match Fallback, New Chat Button, Part Result Card (+9 more)

### Community 54 - "matches_category"
Cohesion: 0.15
Nodes (5): matches_category(), CategoryPaginationTests, page(), CategoryTests, product()

### Community 55 - "recommendations.py"
Cohesion: 0.38
Nodes (6): _matches_viscosity(), _positive_approval(), _required(), _signature(), _specific_configuration(), _viscosities()

### Community 56 - "Chat Texting Mobile UI"
Cohesion: 0.22
Nodes (16): Chat Texting Mobile QA Screenshot, Assistant Message Bubble, Brand and Part Number Line, Result Count + Front/Rear Clarifying Question, Collapsible Details Disclosure, Fitment Verification Warning Callout, Georgian-Language Chat Localization, Greeting Small-Talk Exchange (+8 more)

### Community 57 - "Automotive UI JS Tests"
Cohesion: 0.15
Nodes (13): assert, calls, context, flush(), fs, html, part, responder() (+5 more)

### Community 59 - "automotive/__main__.py"
Cohesion: 0.39
Nodes (4): main(), check_catalog_api(), main(), main()

### Community 60 - "Conversation Input Identity"
Cohesion: 0.24
Nodes (8): _code_identity(), conversation_tools(), EmptyInput, _identity(), NameInput, PartInput, ReplyInput, StrictInput

### Community 61 - "Version Integrity (HMAC)"
Cohesion: 0.20
Nodes (4): VersionIntegrityTests, sign_unsigned_observations(), verify_version_integrity_tag(), version_integrity_tag()

### Community 62 - "Chat UI JS Tests"
Cohesion: 0.15
Nodes (9): assert, calls, ctx, fixture, fs, html, responder(), timers (+1 more)

### Community 63 - "Catalog Fallback Tests"
Cohesion: 0.17
Nodes (12): ChatRequest, ConversationMessage, fitments(), IdentifyRequest, ImportRequest, JobRequest, MotoriumLookupRequest, StrictModel (+4 more)

### Community 65 - "Federated one-VIN search across sources"
Cohesion: 0.25
Nodes (8): Motorium VIN part-code panel, Unified all-sources VIN search panel, Key-gated VIN-to-OE catalog API client (RapidAPI), Motorium VIN-to-part-code connector, Federated one-VIN search across sources, Keyed HMAC VIN cache with sanitized responses, vPIC VIN decoding (format/checksum/warnings), POST /api/v1/vehicles/part-codes (Motorium)

### Community 66 - "Automotive Desktop UI"
Cohesion: 0.23
Nodes (13): Automotive UI Desktop Screenshot, Archived Source Badge, Ask the Catalog (Free-text Question), Configuration Refinement Card, Evidence-first Disclaimer Pattern, Georgian-language Interface, Header: Autocatalog Brand (Vehicle · Parts · Evidence), Hero: Find the Part. Check Compatibility. (+5 more)

### Community 67 - "Automotive Mobile UI"
Cohesion: 0.19
Nodes (13): Automotive UI Mobile QA Screenshot, Autocatalog Brand Header (vehicle · parts · evidence), Manufacturer, Model, Year, Engine Code Fields, Configuration Refinement Card, Truncated Field (likely Fuel), Hero: Find the Part, Check Compatibility, Identify Vehicle Button, Year Input (optional) (+5 more)

### Community 70 - "Rules Engine"
Cohesion: 0.26
Nodes (3): _key(), Rules, _vehicle_matches()

### Community 72 - "Production Release Gate"
Cohesion: 0.06
Nodes (33): Repository Guidelines (AGENTS.md), CI checks: ruff, bandit, pip-audit, unittest, PostgreSQL security/E2E tests (WAREHOUSE_TEST_ADMIN_DATABASE_URL), Warehouse analytics guide, dim_date, dim_product (Type 1), fact_product_current, fact_product_observation (+25 more)

### Community 74 - "Chat Log Tests"
Cohesion: 0.25
Nodes (8): Anthropic API (language/tool orchestration), Deterministic dialogue (answered without model call), Ephemeral in-memory chat conversations, Response model dropped note / quick_replies / act / language (fixed), Texting-style parts chat UI verification, Conversational parts assistant UI verification, Node DOM UI tests (automotive_ui_test.cjs, automotive_chat_ui_test.cjs), Strict tool schemas and finish_response contract

### Community 75 - "Conversational parts assistant"
Cohesion: 0.33
Nodes (5): Parts assistant chat panel, Conversational parts assistant, Deterministic dialogue and backend-written replies, Name-match fallback with verify-compatibility label, Stock mapping: column_10 store, column_12 TUZ 11

### Community 77 - "Repository Guidelines & Migration Policy"
Cohesion: 0.27
Nodes (7): Oil-change flow (grade, capacity, engine family), Fuchs oil chooser connector, JS Asakashi (jsfilter.jp) VIN filter connector, OATS chooser API shape (search / equipment), Pakelo oil chooser connector (per-lookup page token), Public oil and filter connectors, Shell oil chooser connector

### Community 78 - "Chat Log Export"
Cohesion: 0.33
Nodes (4): Optional TecDoc-style catalog API (CATALOG_API_KEY), Name-match fallback (NAME_MATCH_ONLY), 24-hour shared caches and prewarm, Stock source columns (store column_10 / TUZ 11 column_12)

### Community 79 - "Conversation Errors"
Cohesion: 0.11
Nodes (5): _json(), ProviderUnavailable, validate_vin(), vin_checksum(), _row_hash()

### Community 80 - "Initial Warehouse Schema"
Cohesion: 0.42
Nodes (8): warehouse_product_versions, warehouse_product_versions_product_time_idx, warehouse_products, warehouse_products_active_idx, warehouse_products_last_seen_run_idx, warehouse_products_product_code_idx, warehouse_schema_migrations, warehouse_sync_runs

### Community 81 - "Durable Indexing Jobs"
Cohesion: 0.29
Nodes (6): Conservative part-code normalization (retains '/'), Stock-independent content hash (migration 0007), Enrichment index worker (leases, SKIP LOCKED, checkpoints), automotive.part_numbers code index, Migration 0007 content_hash code index, Durable indexing jobs with leases and checkpoints

### Community 82 - "Dependabot Config"
Cohesion: 0.25
Nodes (7): interval, updates, directory, open-pull-requests-limit, package-ecosystem, schedule, version

### Community 83 - "Oil Approval Tests"
Cohesion: 0.48
Nodes (3): vehicle_inventory(), OilApproval, OilApprovalAndCoverageTests

### Community 84 - "Eval Mock Model"
Cohesion: 0.27
Nodes (3): mock_store(), MockModel, MockVinService

### Community 85 - "Analytics Star Schema Docs"
Cohesion: 0.22
Nodes (10): Authorized evidence import (CSV/JSON operator import), Compatibility statuses (VERIFIED / PROVISIONAL / CONFLICTED / UNKNOWN), Deterministic oil eligibility, ASGI transport test harness with event-loop heartbeat, Automotive API and interface QA (2026-11-08), Blocked browser checks (missing X server, sandbox crashpad), Live federation acceptance run, Selected-vehicle inventory matching (POST /inventory/vehicle) (+2 more)

### Community 86 - "Runtime Requirements"
Cohesion: 0.29
Nodes (7): Runtime requirements pin list, fastapi==0.155.0, httpx==0.28.1, psycopg[binary]==3.3.2, python-dotenv==1.2.1, requests==2.32.5, uvicorn==0.54.0

### Community 89 - "Parts Assistant Design"
Cohesion: 0.20
Nodes (6): Local SQLite chat log, Migration 0006_automotive (private schema with RLS), NHTSA vPIC VIN decoder, Ephemeral HMAC-keyed search snapshot, --setup-local local provisioning helper, 30-day sanitized VIN decode cache

### Community 92 - "Conversation Suite Tests"
Cohesion: 0.25
Nodes (6): Automotive /api/v1 endpoint surface, Per-process rate limits (60 API / 10 provider requests per minute), Motorium ai-assistant.php protocol, Motorium VIN / part-code connector, Product JSON-LD metadata (MPN, SKU, OEM), reverse-api-engineer capture attempt

## Ambiguous Edges - Review These
- `Automotive catalog UI page (index.html)` → `Truncated Lower Card (Below Fold)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png · relation: references
- `Configuration Refinement Card` → `Truncated Field (likely Fuel)`  [AMBIGUOUS]
  docs/automotive-review/automotive-mobile.png · relation: references

## Knowledge Gaps
- **50 isolated node(s):** `warehouse-sync`, `Source and evidence panel`, `VIN and configuration forms`, `Match Source (EXACT_CODE)`, `New Chat Button` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 649 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **35 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Automotive intelligence implementation progress` connect `Production Release Gate` to `Settings`, `automotive/repository.py`, `API Request Models`, `Parts Assistant Desktop UI`, `FastAPI App Routes`?**
  _High betweenness centrality (0.108) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `ConversationalChatTests` (e.g. with `ChatService` and `Settings`) actually correct?**
  _`ConversationalChatTests` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `warehouse-sync`, `Source and evidence panel`, `VIN and configuration forms` to the rest of the system?**
  _50 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Reply Composition & Grounding` be split into smaller, more focused modules?**
  _Cohesion score 0.13076923076923078 - nodes in this community are weakly interconnected._
- **Why does `Automotive catalog UI page (index.html)` connect `Parts Assistant Desktop UI` to `Federated one-VIN search across sources`, `Automotive Desktop UI`, `Automotive Mobile UI`, `Production Release Gate`, `Automotive Frontend (app.js)`, `Conversational parts assistant`, `Parts Assistant Mobile UI`, `Chat Texting Desktop UI`, `Chat Texting Mobile UI`?**
  _High betweenness centrality (0.105) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Automotive catalog UI page (index.html)` and `Truncated Lower Card (Below Fold)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Configuration Refinement Card` and `Truncated Field (likely Fuel)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._