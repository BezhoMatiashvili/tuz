# Graph Report - tuz  (2026-11-09)

## Corpus Check
- 134 files · ~104,797 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 21 file(s) not represented in the graph (top: (none) 5, .service 5, .timer 4)

## Summary
- 2286 nodes · 5476 edges · 120 communities (78 shown, 42 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 490 edges (avg confidence: 0.86)
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
- Chat Service
- VIN Federation Tests
- Analytics & Automotive Schema SQL
- Motorium Connector
- Conversation Turn Engine
- Automotive Frontend (app.js)
- Dialogue Parsing & Categories
- API Request Models
- CI Workflow
- DB Explorer CLI & Analytics
- Vehicle Inventory Service
- VIN Search Coordinator
- Sync Configuration & CLI
- Engine Matching (OATS)
- Catalog API & VIN Validation
- Migrations & Chat Log Setup
- Parts Assistant Mobile UI
- Warehouse Source Client
- FastAPI App Routes
- DB Explorer Frontend
- Production Deploy Workflow
- Stock Classification
- Oil Recommendations
- Docker Compose Services
- Fuchs Fluid Connector
- Automotive API Tests
- Evidence Model
- Oil Requirement Matching
- Local Setup Helper
- Dialogue Code Tests
- VIN List Evaluation
- Import & Export
- Conversation Eval Harness
- Product Page Parser
- Automotive Repository Evidence
- Catalog API Client & CLI
- Part Code Normalization
- Eval Mock Repository
- Rules File Loading
- Hard Rule Tests
- Parts Assistant Desktop UI
- Chat Logger
- Chat Evaluation Runner
- Conversation Store Tests
- Name & Year Matching
- JsFilter Connector
- Conversation Store
- Public Oil Catalogs (OATS/Shell/Pakelo)
- Repository Search & Jobs
- Chat Texting Desktop UI
- Category Matching Tests
- VIN Decode Service
- Chat Texting Mobile UI
- Automotive UI JS Tests
- Chat UI DOM Stubs
- Requirement Chooser Tests
- Conversation Input Identity
- Version Integrity (HMAC)
- Chat UI JS Tests
- Catalog Fallback Tests
- Code Index Plumbing Tests
- Warehouse Security & Analytics Docs
- Automotive Desktop UI
- Automotive Mobile UI
- Automotive UI DOM Stubs
- Enrichment Worker
- Rules Engine
- Automotive Postgres Tests
- Production Release Gate
- Unified VIN API Tests
- Chat Log Tests
- Fake Repository (API Tests)
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
- Database Least-Privilege Roles
- Parts Assistant Design
- Automotive Implementation Progress
- Vehicle Text Tests
- Conversation Suite Tests
- Rate Limiter
- Chat Log Test Engine
- Chat Log Failure Tests
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
9. `PublicSourceError` - 31 edges
10. `AutomotiveApiTests` - 31 edges

## Surprising Connections (you probably didn't know these)
- `Truncated Lower Card (Below Fold)` --references--> `Automotive catalog UI page (index.html)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html
- `Direct Search & Vehicle Details Disclosure` --references--> `Automotive catalog UI page (index.html)`  [INFERRED]
  docs/automotive-review/conversational-parts-desktop.png → automotive/static/index.html
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

## Communities (120 total, 42 thin omitted)

### Community 0 - "Reply Composition & Grounding"
Cohesion: 0.10
Nodes (28): availability(), _axle(), brand_name(), car_label(), _clarify(), compose(), _compose(), _greet() (+20 more)

### Community 1 - "Conversational Chat Tests"
Cohesion: 0.08
Nodes (5): Rules, ConversationalChatTests, finish(), response(), tool()

### Community 2 - "Warehouse Sync Service"
Cohesion: 0.13
Nodes (10): SyncProgressTests, _html(), ReliabilityTests, _settings(), SyncCounters, WarehouseRepository, _log_progress(), sync() (+2 more)

### Community 3 - "Automotive Docs & Integrations"
Cohesion: 0.29
Nodes (9): Automotive intelligence MVP, Hard offer rules (always on), pip-audit vulnerable pins (lxml, pytest, python-dotenv, requests), Self-review scorecard (3.8/5), Unified VIN source search QA (2026-11-09), Warehouse preservation digest check, Staff rules file (automotive/rules.toml), Unified VIN read federation (/vehicles/search) (+1 more)

### Community 4 - "Chat Service"
Cohesion: 0.13
Nodes (8): CodeInput, EmptyInput, PresentInput, _days(), _flag(), _seconds(), Server-side Claude orchestration with strict read tools, anthropic==0.72.0

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
Cohesion: 0.08
Nodes (4): ConversationTurn, read(), run(), read()

### Community 9 - "Automotive Frontend (app.js)"
Cohesion: 0.13
Nodes (42): api(), byId(), chatMessage(), chatText(), chatWarnings, clearDependentResults(), clearQuickReplies(), clearVinContext() (+34 more)

### Community 10 - "Dialogue Parsing & Categories"
Cohesion: 0.07
Nodes (19): _same(), _clean(), code_question(), continues(), detect_language(), displacement_answer(), _display(), engine_answer() (+11 more)

### Community 11 - "API Request Models"
Cohesion: 0.12
Nodes (20): CatalogProduct, ConversationCard, ConversationTurn, FluidProduct, FluidReport, JobResult, MotoriumPart, MotoriumResult (+12 more)

### Community 12 - "CI Workflow"
Cohesion: 0.05
Nodes (40): permissions, runs-on, steps, GITHUB_TOKEN, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, WAREHOUSE_TEST_ADMIN_DATABASE_URL (+32 more)

### Community 13 - "DB Explorer CLI & Analytics"
Cohesion: 0.10
Nodes (11): main(), browse(), catalog(), connect(), Handler, json_default(), main(), run_query() (+3 more)

### Community 14 - "Vehicle Inventory Service"
Cohesion: 0.12
Nodes (7): VehicleInventoryService, CategoryPaginationTests, page(), evidence(), product(), VehicleInventoryTests, page()

### Community 15 - "VIN Search Coordinator"
Cohesion: 0.09
Nodes (9): _key(), same_vehicle(), VINSearchCoordinator, fail(), one(), soft(), start(), start_fluid() (+1 more)

### Community 16 - "Sync Configuration & CLI"
Cohesion: 0.09
Nodes (19): main(), ASVS V14 configuration (fail-closed, SHA-pinned CI), _MockSourceHandler, _configuration_error(), _database_url_is_production_safe(), _decode_hmac_key(), _load_environment(), load_maintenance_hmac_key() (+11 more)

### Community 17 - "Engine Matching (OATS)"
Cohesion: 0.14
Nodes (12): by_body(), by_engine(), engine_code(), engine_family(), engine_matches(), fuel_matches(), narrow(), trim_groups() (+4 more)

### Community 18 - "Catalog API & VIN Validation"
Cohesion: 0.13
Nodes (9): categories_for(), _dicts(), parse_articles(), parse_group_ids(), parse_vehicles(), _text(), CatalogApiTests, client() (+1 more)

### Community 19 - "Migrations & Chat Log Setup"
Cohesion: 0.11
Nodes (3): AnalyticsIntegrationTests, PostgresPrivilegeTests, apply_migrations_on_connection()

### Community 20 - "Parts Assistant Mobile UI"
Cohesion: 0.08
Nodes (31): Automotive catalog UI page (index.html), Part code search panel, Source and evidence panel, Fitment, cross-reference and oil panel, Vehicle parts (warehouse) panel, VIN and configuration forms, Conversational Parts Assistant Mobile QA Screenshot, Assistant Reply: Catalog Candidates Need Fitment Verification (+23 more)

### Community 21 - "Warehouse Source Client"
Cohesion: 0.14
Nodes (4): WarehouseClientTests, SyncEndToEndTests, WarehouseClient, Settings

### Community 22 - "FastAPI App Routes"
Cohesion: 0.06
Nodes (3): create_app(), oil(), oils()

### Community 23 - "DB Explorer Frontend"
Cohesion: 0.12
Nodes (29): acceptSuggestion(), activateTab(), api(), completionContext(), drawSuggestions(), escapeHTML(), exportResults(), format() (+21 more)

### Community 24 - "Production Deploy Workflow"
Cohesion: 0.06
Nodes (30): concurrency, cancel-in-progress, group, environment, name, runs-on, steps, DEPLOY_HOST (+22 more)

### Community 25 - "Stock Classification"
Cohesion: 0.16
Nodes (7): age_hours(), availability_text(), classify(), is_stale(), _number(), parse_count(), StockTests

### Community 26 - "Oil Recommendations"
Cohesion: 0.15
Nodes (9): _matches_viscosity(), _positive_approval(), RecommendationService, _required(), _signature(), _specific_configuration(), _viscosities(), evidence() (+1 more)

### Community 27 - "Docker Compose Services"
Cohesion: 0.08
Nodes (28): context, dockerfile, build, depends_on, environment, ports, restart, postgres (+20 more)

### Community 28 - "Fuchs Fluid Connector"
Cohesion: 0.09
Nodes (17): identical(), signature(), _ambiguous(), _covers_year(), FuchsClient, _make_matches(), _model_covers_year(), _model_matches() (+9 more)

### Community 30 - "Evidence Model"
Cohesion: 0.11
Nodes (10): import_evidence(), applies_to(), evaluate_compatibility(), flatten_record(), _same(), verified_record(), parse_import(), _vehicle_scoped() (+2 more)

### Community 31 - "Oil Requirement Matching"
Cohesion: 0.17
Nodes (11): coverage(), engine_family_rows(), _fuel_excludes(), normalize_grade(), oil_rows(), pack_litres(), product_line(), _row() (+3 more)

### Community 32 - "Local Setup Helper"
Cohesion: 0.15
Nodes (6): configure_local(), local_admin_url(), LocalSetupError, _new_login(), _require_catalog(), LocalSetupTests

### Community 33 - "Dialogue Code Tests"
Cohesion: 0.09
Nodes (9): _redact(), grades(), references(), small_talk(), CodeTests, LanguageTests, ReferenceTests, ReviewRegressionTests (+1 more)

### Community 34 - "VIN List Evaluation"
Cohesion: 0.14
Nodes (9): anonymous_misses(), classify(), evaluate(), row_for(), summarize(), write_misses(), main(), main() (+1 more)

### Community 35 - "Import & Export"
Cohesion: 0.16
Nodes (5): column_name(), export_result(), spreadsheet_value(), text_value(), xml_value()

### Community 36 - "Conversation Eval Harness"
Cohesion: 0.11
Nodes (15): Counting, evaluate(), _fill(), _hide(), judge(), legacy_language(), live_store(), print_summary() (+7 more)

### Community 37 - "Product Page Parser"
Cohesion: 0.12
Nodes (10): ParseProductsPageTests, SourceChangedError, SourceProtocolError, ParsedPage, WarehouseProduct, parse_products_page(), _value_at(), _check_page() (+2 more)

### Community 38 - "Automotive Repository Evidence"
Cohesion: 0.14
Nodes (3): AutomotiveRepository, _evidence(), RepositoryBoundaryTests

### Community 39 - "Catalog API Client & CLI"
Cohesion: 0.13
Nodes (4): CatalogApiClient, check_catalog_api(), PublicHttp, PublicSourceError

### Community 40 - "Part Code Normalization"
Cohesion: 0.29
Nodes (4): parse_codes(), PartCode, product_codes(), CodeTests

### Community 42 - "Rules File Loading"
Cohesion: 0.18
Nodes (10): _check(), load_rules(), _number(), parse_rules(), RulesError, _text(), _texts(), _year() (+2 more)

### Community 43 - "Hard Rule Tests"
Cohesion: 0.16
Nodes (7): approvals_stated(), axle_verdict(), NeverOffer, oem_code_ok(), StaffConfirmed, DataClassTests, HardRuleTests

### Community 44 - "Parts Assistant Desktop UI"
Cohesion: 0.13
Nodes (21): Conversational Parts Lookup Desktop Screenshot, Auto Catalog Header (cars, parts, fitment) with local service label, Assistant Reply: catalog candidates, fitment needs verification, Bilingual Georgian / English UI Labels, Parts Assistant Chat Panel, Conservative Stock and Fitment Confirmation Pattern, Direct Search & Vehicle Details Disclosure, Fitment Unknown Badge (+13 more)

### Community 45 - "Chat Logger"
Cohesion: 0.05
Nodes (10): ChatLogger, _text(), ConversationError, ConversationStore, cancelled(), progress(), _shown(), ChatLogTests (+2 more)

### Community 46 - "Chat Evaluation Runner"
Cohesion: 0.15
Nodes (6): load_scenarios(), read_vins(), read_vins(), run_chat_evaluation(), run_evaluation(), ConversationSuiteTests

### Community 47 - "Conversation Store Tests"
Cohesion: 0.09
Nodes (14): check_turn(), _key(), _claims(), digit_tokens(), facts_text(), language_ok(), names_part_brand(), _norm() (+6 more)

### Community 48 - "Name & Year Matching"
Cohesion: 0.16
Nodes (8): _key(), _make_names(), make_pattern(), _model_core(), model_pattern(), _two_or_four(), year_fit(), YearFitTests

### Community 49 - "JsFilter Connector"
Cohesion: 0.29
Nodes (5): tool_definitions(), ChatTests, handler(), response(), tool()

### Community 50 - "Conversation Store"
Cohesion: 0.21
Nodes (5): ChatService, _dump(), Settings, mock_store(), MockVinService

### Community 51 - "Public Oil Catalogs (OATS/Shell/Pakelo)"
Cohesion: 0.15
Nodes (6): _number(), OatsCatalog, PakeloClient, ShellOilClient, equipment(), OatsTests

### Community 53 - "Chat Texting Desktop UI"
Cohesion: 0.17
Nodes (17): Chat Texting Desktop QA Screenshot, Bilingual Georgian/English Labels, Details Disclosure, Fitment and Stock Disclaimer, Match Reason ('Chosen because'), Car-name Match Fallback, New Chat Button, Part Result Card (+9 more)

### Community 54 - "Category Matching Tests"
Cohesion: 0.14
Nodes (7): category_for_query(), matches_category(), query_patterns(), normalize_code(), CategoryTests, product(), CategoryTests

### Community 55 - "VIN Decode Service"
Cohesion: 0.26
Nodes (3): _text(), VinService, VinTests

### Community 56 - "Chat Texting Mobile UI"
Cohesion: 0.22
Nodes (16): Chat Texting Mobile QA Screenshot, Assistant Message Bubble, Brand and Part Number Line, Result Count + Front/Rear Clarifying Question, Collapsible Details Disclosure, Fitment Verification Warning Callout, Georgian-Language Chat Localization, Greeting Small-Talk Exchange (+8 more)

### Community 57 - "Automotive UI JS Tests"
Cohesion: 0.15
Nodes (13): assert, calls, context, flush(), fs, html, part, responder() (+5 more)

### Community 59 - "Requirement Chooser Tests"
Cohesion: 0.34
Nodes (5): ChooserResolutionTests, mercedes(), gle(), detail(), item()

### Community 60 - "Conversation Input Identity"
Cohesion: 0.17
Nodes (11): _code_identity(), conversation_tools(), EmptyInput, _identity(), NameInput, PartInput, ReplyInput, StrictInput (+3 more)

### Community 61 - "Version Integrity (HMAC)"
Cohesion: 0.14
Nodes (5): VersionIntegrityTests, _E2ESourceHandler, sign_unsigned_observations(), verify_version_integrity_tag(), version_integrity_tag()

### Community 62 - "Chat UI JS Tests"
Cohesion: 0.15
Nodes (9): assert, calls, ctx, fixture, fs, html, responder(), timers (+1 more)

### Community 63 - "Catalog Fallback Tests"
Cohesion: 0.17
Nodes (12): ChatRequest, ConversationMessage, fitments(), IdentifyRequest, ImportRequest, JobRequest, MotoriumLookupRequest, StrictModel (+4 more)

### Community 65 - "Warehouse Security & Analytics Docs"
Cohesion: 0.22
Nodes (8): OWASP ASVS Level 2 release evidence matrix, ASVS V10 malicious input (cell-text parser, bounded response), ASVS V4 access control (scoped writer functions), ASVS V6 cryptography (TLS verify-full, external HMAC key), ASVS V7 error handling (safe failure codes), ASVS V8 data protection (no raw HTML, private schema, 24-month pruning), Legacy 0.1 upgrade: migrate through 0002 and sign legacy versions, source_columns JSONB text-only storage

### Community 66 - "Automotive Desktop UI"
Cohesion: 0.23
Nodes (13): Automotive UI Desktop Screenshot, Archived Source Badge, Ask the Catalog (Free-text Question), Configuration Refinement Card, Evidence-first Disclaimer Pattern, Georgian-language Interface, Header: Autocatalog Brand (Vehicle · Parts · Evidence), Hero: Find the Part. Check Compatibility. (+5 more)

### Community 67 - "Automotive Mobile UI"
Cohesion: 0.19
Nodes (13): Automotive UI Mobile QA Screenshot, Autocatalog Brand Header (vehicle · parts · evidence), Manufacturer, Model, Year, Engine Code Fields, Configuration Refinement Card, Truncated Field (likely Fuel), Hero: Find the Part, Check Compatibility, Identify Vehicle Button, Year Input (optional) (+5 more)

### Community 70 - "Rules Engine"
Cohesion: 0.22
Nodes (3): _key(), vehicle_consistent(), _vehicle_matches()

### Community 72 - "Production Release Gate"
Cohesion: 0.06
Nodes (32): Repository Guidelines (AGENTS.md), CI checks: ruff, bandit, pip-audit, unittest, PostgreSQL security/E2E tests (WAREHOUSE_TEST_ADMIN_DATABASE_URL), Warehouse analytics guide, dim_date, dim_product (Type 1), fact_product_current, fact_product_observation (+24 more)

### Community 73 - "Unified VIN API Tests"
Cohesion: 0.19
Nodes (4): FakeChat, FakeVin, test_unified_search_uses_provider_rate_budget(), test_unified_vin_read_job_is_typed_pollable_and_private()

### Community 74 - "Chat Log Tests"
Cohesion: 0.18
Nodes (11): Anthropic API (language/tool orchestration), Backend-built replies, Deterministic dialogue (answered without model call), Ephemeral in-memory chat conversations, Optional reply rewording (AUTOMOTIVE_CHAT_REPHRASE), Messenger bubbles and quick-reply chips, Response model dropped note / quick_replies / act / language (fixed), Texting-style parts chat UI verification (+3 more)

### Community 77 - "Repository Guidelines & Migration Policy"
Cohesion: 0.24
Nodes (8): Oil-change flow (grade, capacity, engine family), Deterministic oil eligibility, Fuchs oil chooser connector, JS Asakashi (jsfilter.jp) VIN filter connector, OATS chooser API shape (search / equipment), Pakelo oil chooser connector (per-lookup page token), Public oil and filter connectors, Shell oil chooser connector

### Community 78 - "Chat Log Export"
Cohesion: 0.20
Nodes (6): Optional TecDoc-style catalog API (CATALOG_API_KEY), Name-match fallback (NAME_MATCH_ONLY), 24-hour shared caches and prewarm, Stock source columns (store column_10 / TUZ 11 column_12), Motorium VIN / part-code connector, reverse-api-engineer capture attempt

### Community 79 - "Conversation Errors"
Cohesion: 0.08
Nodes (16): _json(), Motorium VIN part-code panel, Unified all-sources VIN search panel, merge_parts(), validate_vin(), vin_checksum(), Key-gated VIN-to-OE catalog API client (RapidAPI), Motorium VIN-to-part-code connector (+8 more)

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

### Community 85 - "Analytics Star Schema Docs"
Cohesion: 0.24
Nodes (9): Authorized evidence import (CSV/JSON operator import), Compatibility statuses (VERIFIED / PROVISIONAL / CONFLICTED / UNKNOWN), ASGI transport test harness with event-loop heartbeat, Automotive API and interface QA (2026-11-08), Blocked browser checks (missing X server, sandbox crashpad), Live federation acceptance run, Selected-vehicle inventory matching (POST /inventory/vehicle), Selected-vehicle inventory QA (2026-11-09) (+1 more)

### Community 86 - "Runtime Requirements"
Cohesion: 0.29
Nodes (7): Runtime requirements pin list, fastapi==0.155.0, httpx==0.28.1, psycopg[binary]==3.3.2, python-dotenv==1.2.1, requests==2.32.5, uvicorn==0.54.0

### Community 89 - "Parts Assistant Design"
Cohesion: 0.25
Nodes (6): Local SQLite chat log, Migration 0006_automotive (private schema with RLS), NHTSA vPIC VIN decoder, Ephemeral HMAC-keyed search snapshot, --setup-local local provisioning helper, 30-day sanitized VIN decode cache

### Community 92 - "Conversation Suite Tests"
Cohesion: 0.33
Nodes (4): Automotive /api/v1 endpoint surface, Per-process rate limits (60 API / 10 provider requests per minute), Motorium ai-assistant.php protocol, Product JSON-LD metadata (MPN, SKU, OEM)

## Ambiguous Edges - Review These
- `Automotive catalog UI page (index.html)` → `Truncated Lower Card (Below Fold)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png · relation: references
- `Configuration Refinement Card` → `Truncated Field (likely Fuel)`  [AMBIGUOUS]
  docs/automotive-review/automotive-mobile.png · relation: references

## Knowledge Gaps
- **50 isolated node(s):** `warehouse-sync`, `Source and evidence panel`, `VIN and configuration forms`, `Match Source (EXACT_CODE)`, `New Chat Button` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 649 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **42 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Automotive intelligence implementation progress` connect `Production Release Gate` to `API Request Models`, `Conversation Store`, `Parts Assistant Mobile UI`, `Category Matching Tests`, `FastAPI App Routes`?**
  _High betweenness centrality (0.096) - this node is a cross-community bridge._
- **Are the 5 inferred relationships involving `ConversationalChatTests` (e.g. with `ChatService` and `Settings`) actually correct?**
  _`ConversationalChatTests` has 5 INFERRED edges - model-reasoned connections that need verification._
- **What connects `warehouse-sync`, `Source and evidence panel`, `VIN and configuration forms` to the rest of the system?**
  _50 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Reply Composition & Grounding` be split into smaller, more focused modules?**
  _Cohesion score 0.09783368273934312 - nodes in this community are weakly interconnected._
- **Why does `Automotive catalog UI page (index.html)` connect `Parts Assistant Mobile UI` to `Automotive Desktop UI`, `Automotive Mobile UI`, `Production Release Gate`, `Automotive Frontend (app.js)`, `Parts Assistant Desktop UI`, `Conversation Errors`, `Chat Texting Desktop UI`, `Chat Texting Mobile UI`, `Conversation Input Identity`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Automotive catalog UI page (index.html)` and `Truncated Lower Card (Below Fold)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Configuration Refinement Card` and `Truncated Field (likely Fuel)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._