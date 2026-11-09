# Graph Report - tuz  (2026-11-09)

## Corpus Check
- 134 files · ~106,755 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 22 file(s) not represented in the graph (top: (none) 6, .service 5, .timer 4)

## Summary
- 2335 nodes · 5587 edges · 130 communities (83 shown, 47 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 486 edges (avg confidence: 0.86)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `75570d9d`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Reply Composition & Grounding
- Conversational Chat Tests
- sync
- Automotive Docs & Integrations
- .respond
- VIN Federation Tests
- Analytics & Automotive Schema SQL
- Motorium Connector
- Conversation Turn Engine
- automotive/static/app.js
- dialogue.py
- API Request Models
- CI Workflow
- DB Explorer CLI & Analytics
- Vehicle Inventory Service
- VIN Search Coordinator
- Sync Configuration & CLI
- Engine Matching (OATS)
- test_automotive_requirements.py
- AnalyticsIntegrationTests
- Parts Assistant Mobile UI
- WarehouseClient
- FastAPI App Routes
- DB Explorer Frontend
- Production Deploy Workflow
- oil_match.py
- .recommend_oil
- Docker Compose Services
- oats.py
- AutomotiveApiTests
- Evidence Model
- PublicHttp
- Local Setup Helper
- test_dialogue.py
- VIN List Evaluation
- VehicleTextTests
- Conversation Eval Harness
- parser.py
- ._connect
- automotive/__main__.py
- automotive/repository.py
- Eval Mock Repository
- rules.py
- ._typed_vehicle
- Parts Assistant Desktop UI
- .submit
- service.py
- grounding.py
- CatalogApiClient
- test_automotive_fluids.py
- ChooserResolutionTests
- Public Oil Catalogs (OATS/Shell/Pakelo)
- integrity.py
- Chat Texting Desktop UI
- matches_category
- .decode
- Chat Texting Mobile UI
- Automotive UI JS Tests
- Chat UI DOM Stubs
- warehouse_sync/__main__.py
- unittest
- Chat UI JS Tests
- VehicleInventoryService
- Code Index Plumbing Tests
- Warehouse to PostgreSQL synchroniser README
- Automotive Desktop UI
- Automotive Mobile UI
- Automotive UI DOM Stubs
- Enrichment Worker
- _sync_once
- Automotive Postgres Tests
- _clean
- chat.py
- Chat Log Tests
- Conversational parts assistant
- FakeRepository
- Repository Guidelines & Migration Policy
- Chat Log Export
- vin_search.py
- Initial Warehouse Schema
- Durable Indexing Jobs
- Dependabot Config
- small_talk
- Eval Mock Model
- Analytics Star Schema Docs
- Runtime Requirements
- Production Settings Tests
- .vehicle_catalog_candidates
- Parts Assistant Design
- Settings
- test_automotive_api.py
- Conversation Suite Tests
- load_scenarios
- _designation_at
- code_question
- Sync E2E Source Stub
- Federated one-VIN search across sources
- Project Metadata
- .cache_vin
- chat_log.py
- AutomotiveRepository
- test_analytics_cli.py
- Engine
- PostgresPrivilegeTests
- _MockSourceHandler

## God Nodes (most connected - your core abstractions)
1. `ConversationalChatTests` - 73 edges
2. `create_app()` - 67 edges
3. `AutomotiveRepository` - 60 edges
4. `FederationTests` - 45 edges
5. `Settings` - 39 edges
6. `ConversationTurn` - 36 edges
7. `PublicSourceError` - 33 edges
8. `category_for_query()` - 32 edges
9. `AutomotiveApiTests` - 31 edges
10. `VINSearchCoordinator` - 30 edges

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

## Communities (130 total, 47 thin omitted)

### Community 0 - "Reply Composition & Grounding"
Cohesion: 0.13
Nodes (28): availability(), _axle(), brand_name(), car_label(), _clarify(), compose(), _compose(), engine_choice() (+20 more)

### Community 1 - "Conversational Chat Tests"
Cohesion: 0.08
Nodes (4): ConversationalChatTests, finish(), response(), tool()

### Community 2 - "sync"
Cohesion: 0.45
Nodes (4): _html(), ReliabilityTests, _settings(), sync()

### Community 3 - "Automotive Docs & Integrations"
Cohesion: 0.29
Nodes (9): Automotive intelligence MVP, Hard offer rules (always on), pip-audit vulnerable pins (lxml, pytest, python-dotenv, requests), Self-review scorecard (3.8/5), Unified VIN source search QA (2026-11-09), Warehouse preservation digest check, Staff rules file (automotive/rules.toml), Unified VIN read federation (/vehicles/search) (+1 more)

### Community 4 - ".respond"
Cohesion: 0.17
Nodes (6): ChatService, tool_definitions(), ChatTests, handler(), response(), tool()

### Community 5 - "VIN Federation Tests"
Cohesion: 0.11
Nodes (6): FluidFederationTests, FederationTests, MergeTests, product(), supplier(), warehouse()

### Community 6 - "Analytics & Automotive Schema SQL"
Cohesion: 0.07
Nodes (39): warehouse.finish_sync_run(), warehouse.prune_product_versions(), warehouse.record_page_seen(), warehouse.record_product(), warehouse.warehouse_sync_seen_pages, analytics.dim_date, analytics.dim_product, analytics.fact_product_current (+31 more)

### Community 7 - "Motorium Connector"
Cohesion: 0.10
Nodes (9): _identity(), MotoriumClient, MotoriumError, ProductMetadata, _text(), details(), MotoriumTests, response() (+1 more)

### Community 8 - "Conversation Turn Engine"
Cohesion: 0.06
Nodes (13): _code_identity(), conversation_tools(), ConversationTurn, read(), run(), read(), EmptyInput, _identity() (+5 more)

### Community 9 - "automotive/static/app.js"
Cohesion: 0.13
Nodes (42): api(), byId(), chatMessage(), chatText(), chatWarnings, clearDependentResults(), clearQuickReplies(), clearVinContext() (+34 more)

### Community 10 - "dialogue.py"
Cohesion: 0.18
Nodes (9): _display(), _georgian_form(), _known_model(), _make_at(), _model_word(), part_codes(), read_vehicle(), _stated_size() (+1 more)

### Community 11 - "API Request Models"
Cohesion: 0.07
Nodes (34): CatalogProduct, ChatRequest, ConversationCard, ConversationMessage, ConversationTurn, fitments(), FluidProduct, FluidReport (+26 more)

### Community 12 - "CI Workflow"
Cohesion: 0.05
Nodes (40): permissions, runs-on, steps, GITHUB_TOKEN, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, WAREHOUSE_TEST_ADMIN_DATABASE_URL (+32 more)

### Community 13 - "DB Explorer CLI & Analytics"
Cohesion: 0.12
Nodes (8): browse(), catalog(), connect(), Handler, json_default(), main(), run_query(), ExplorerTests

### Community 14 - "Vehicle Inventory Service"
Cohesion: 0.19
Nodes (4): evidence(), product(), VehicleInventoryTests, page()

### Community 15 - "VIN Search Coordinator"
Cohesion: 0.09
Nodes (9): _key(), same_vehicle(), VINSearchCoordinator, fail(), one(), soft(), start(), start_fluid() (+1 more)

### Community 16 - "Sync Configuration & CLI"
Cohesion: 0.18
Nodes (15): _days(), _flag(), _seconds(), ASVS V14 configuration (fail-closed, SHA-pinned CI), _configuration_error(), _database_url_is_production_safe(), _decode_hmac_key(), _load_environment() (+7 more)

### Community 17 - "Engine Matching (OATS)"
Cohesion: 0.10
Nodes (20): by_body(), by_engine(), engine_code(), engine_family(), engine_matches(), engine_option(), engine_options(), words() (+12 more)

### Community 18 - "test_automotive_requirements.py"
Cohesion: 0.14
Nodes (7): age_hours(), availability_text(), classify(), is_stale(), _number(), parse_count(), StockTests

### Community 20 - "Parts Assistant Mobile UI"
Cohesion: 0.12
Nodes (22): Conversational Parts Assistant Mobile QA Screenshot, Assistant Reply: Catalog Candidates Need Fitment Verification, Bilingual Georgian/English UI Labels, Parts Assistant Chat Panel, Collapsible Direct Search & Vehicle Details, Fitment Unknown Badge, Footer Read-Only Data Disclaimer, Match Source (EXACT_CODE) (+14 more)

### Community 21 - "WarehouseClient"
Cohesion: 0.20
Nodes (5): WarehouseClientTests, WarehouseClient, AuthenticationError, SourceProtocolError, _fetch_and_parse()

### Community 23 - "DB Explorer Frontend"
Cohesion: 0.19
Nodes (22): acceptSuggestion(), activateTab(), api(), completionContext(), drawSuggestions(), escapeHTML(), exportResults(), format() (+14 more)

### Community 24 - "Production Deploy Workflow"
Cohesion: 0.06
Nodes (30): concurrency, cancel-in-progress, group, environment, name, runs-on, steps, DEPLOY_HOST (+22 more)

### Community 25 - "oil_match.py"
Cohesion: 0.16
Nodes (12): coverage(), engine_family_rows(), _fuel_excludes(), normalize_grade(), oil_rows(), pack_litres(), product_line(), requirements() (+4 more)

### Community 26 - ".recommend_oil"
Cohesion: 0.06
Nodes (23): import_evidence(), parse_import(), _matches_viscosity(), _positive_approval(), _required(), _signature(), _specific_configuration(), _viscosities() (+15 more)

### Community 27 - "Docker Compose Services"
Cohesion: 0.08
Nodes (28): context, dockerfile, build, depends_on, environment, ports, restart, postgres (+20 more)

### Community 28 - "oats.py"
Cohesion: 0.16
Nodes (10): identical(), signature(), _ambiguous(), _covers_year(), _make_matches(), _model_matches(), fluid_product(), fluid_report() (+2 more)

### Community 30 - "Evidence Model"
Cohesion: 0.25
Nodes (5): applies_to(), evaluate_compatibility(), flatten_record(), _same(), verified_record()

### Community 31 - "PublicHttp"
Cohesion: 0.15
Nodes (4): JsFilterClient, _Table, PublicHttp, JsFilterTests

### Community 32 - "Local Setup Helper"
Cohesion: 0.17
Nodes (6): configure_local(), local_admin_url(), LocalSetupError, _new_login(), _require_catalog(), LocalSetupTests

### Community 33 - "test_dialogue.py"
Cohesion: 0.17
Nodes (5): detect_language(), references(), LanguageTests, ReferenceTests, ReviewRegressionTests

### Community 34 - "VIN List Evaluation"
Cohesion: 0.15
Nodes (9): anonymous_misses(), classify(), evaluate(), row_for(), summarize(), write_misses(), --evaluate VIN measurement CLI, --evaluate-chat conversation evaluation (+1 more)

### Community 36 - "Conversation Eval Harness"
Cohesion: 0.12
Nodes (13): Counting, evaluate(), _fill(), _hide(), judge(), _key(), legacy_language(), live_store() (+5 more)

### Community 37 - "parser.py"
Cohesion: 0.13
Nodes (7): ASVS V10 malicious input (cell-text parser, bounded response), ParseProductsPageTests, WarehouseProduct, parse_products_page(), _ProductRowsParser, _row_hash(), _value_at()

### Community 39 - "automotive/__main__.py"
Cohesion: 0.23
Nodes (6): read_vins(), read_vins(), check_catalog_api(), main(), run_chat_evaluation(), run_evaluation()

### Community 40 - "automotive/repository.py"
Cohesion: 0.29
Nodes (4): parse_codes(), PartCode, product_codes(), CodeTests

### Community 42 - "rules.py"
Cohesion: 0.06
Nodes (27): oil(), vehicle_inventory(), approvals_stated(), axle_verdict(), _check(), _key(), load_rules(), NeverOffer (+19 more)

### Community 43 - "._typed_vehicle"
Cohesion: 0.18
Nodes (3): engine_answer(), TypedVehicle, year_answer()

### Community 44 - "Parts Assistant Desktop UI"
Cohesion: 0.08
Nodes (30): Automotive catalog UI page (index.html), Part code search panel, Source and evidence panel, Fitment, cross-reference and oil panel, Vehicle parts (warehouse) panel, VIN and configuration forms, Conversational Parts Lookup Desktop Screenshot, Auto Catalog Header (cars, parts, fitment) with local service label (+22 more)

### Community 45 - ".submit"
Cohesion: 0.05
Nodes (12): ChatLogger, _json(), _text(), ConversationError, ConversationStore, cancelled(), progress(), _same() (+4 more)

### Community 46 - "service.py"
Cohesion: 0.14
Nodes (5): failure_code(), SourceChangedError, ParsedPage, _check_page(), _page_signature()

### Community 47 - "grounding.py"
Cohesion: 0.07
Nodes (16): check_turn(), _claims(), digit_tokens(), facts_text(), language_ok(), names_part_brand(), _norm(), numbers() (+8 more)

### Community 48 - "CatalogApiClient"
Cohesion: 0.06
Nodes (17): CatalogApiClient, _dicts(), parse_articles(), parse_group_ids(), parse_vehicles(), _text(), _key(), _make_names() (+9 more)

### Community 49 - "test_automotive_fluids.py"
Cohesion: 0.18
Nodes (6): FuchsClient, _model_covers_year(), ShellOilClient, equipment(), FakeHttp, FuchsTests

### Community 50 - "ChooserResolutionTests"
Cohesion: 0.29
Nodes (6): ChooserResolutionTests, coupe(), mercedes(), gle(), detail(), item()

### Community 51 - "Public Oil Catalogs (OATS/Shell/Pakelo)"
Cohesion: 0.14
Nodes (6): _number(), OatsCatalog, distance(), PakeloClient, PublicSourceError, OatsTests

### Community 52 - "integrity.py"
Cohesion: 0.26
Nodes (3): VersionIntegrityTests, verify_version_integrity_tag(), version_integrity_tag()

### Community 53 - "Chat Texting Desktop UI"
Cohesion: 0.17
Nodes (17): Chat Texting Desktop QA Screenshot, Bilingual Georgian/English Labels, Details Disclosure, Fitment and Stock Disclaimer, Match Reason ('Chosen because'), Car-name Match Fallback, New Chat Button, Part Result Card (+9 more)

### Community 54 - "matches_category"
Cohesion: 0.16
Nodes (7): categories_for(), category_for_query(), matches_category(), page(), CategoryTests, product(), CategoryTests

### Community 55 - ".decode"
Cohesion: 0.22
Nodes (4): ProviderUnavailable, _text(), VinService, VinTests

### Community 56 - "Chat Texting Mobile UI"
Cohesion: 0.22
Nodes (16): Chat Texting Mobile QA Screenshot, Assistant Message Bubble, Brand and Part Number Line, Result Count + Front/Rear Clarifying Question, Collapsible Details Disclosure, Fitment Verification Warning Callout, Georgian-Language Chat Localization, Greeting Small-Talk Exchange (+8 more)

### Community 57 - "Automotive UI JS Tests"
Cohesion: 0.15
Nodes (13): assert, calls, context, flush(), fs, html, part, responder() (+5 more)

### Community 59 - "warehouse_sync/__main__.py"
Cohesion: 0.23
Nodes (5): ConfigurationError, sign_unsigned_observations(), main(), _parser(), apply_migrations()

### Community 61 - "unittest"
Cohesion: 0.13
Nodes (3): _E2ESourceHandler, SyncEndToEndTests, apply_migrations_on_connection()

### Community 62 - "Chat UI JS Tests"
Cohesion: 0.15
Nodes (9): assert, calls, ctx, fixture, fs, html, responder(), timers (+1 more)

### Community 63 - "VehicleInventoryService"
Cohesion: 0.24
Nodes (3): _vehicle_scoped(), VehicleInventoryService, _year_scoped()

### Community 65 - "Warehouse to PostgreSQL synchroniser README"
Cohesion: 0.06
Nodes (38): Repository Guidelines (AGENTS.md), CI checks: ruff, bandit, pip-audit, unittest, PostgreSQL security/E2E tests (WAREHOUSE_TEST_ADMIN_DATABASE_URL), Warehouse analytics guide, dim_date, dim_product (Type 1), fact_product_current, fact_product_observation (+30 more)

### Community 66 - "Automotive Desktop UI"
Cohesion: 0.23
Nodes (13): Automotive UI Desktop Screenshot, Archived Source Badge, Ask the Catalog (Free-text Question), Configuration Refinement Card, Evidence-first Disclaimer Pattern, Georgian-language Interface, Header: Autocatalog Brand (Vehicle · Parts · Evidence), Hero: Find the Part. Check Compatibility. (+5 more)

### Community 67 - "Automotive Mobile UI"
Cohesion: 0.19
Nodes (13): Automotive UI Mobile QA Screenshot, Autocatalog Brand Header (vehicle · parts · evidence), Manufacturer, Model, Year, Engine Code Fields, Configuration Refinement Card, Truncated Field (likely Fuel), Hero: Find the Part, Check Compatibility, Identify Vehicle Button, Year Input (optional) (+5 more)

### Community 70 - "_sync_once"
Cohesion: 0.18
Nodes (5): SyncCounters, WarehouseRepository, _log_progress(), _sync_once(), SyncResult

### Community 72 - "_clean"
Cohesion: 0.20
Nodes (5): _clean(), continues(), engine_details(), fuel_named(), pick_option()

### Community 73 - "chat.py"
Cohesion: 0.17
Nodes (8): CodeInput, _dump(), EmptyInput, PresentInput, _redact(), RecommendationService, Server-side Claude orchestration with strict read tools, anthropic==0.72.0

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

### Community 79 - "vin_search.py"
Cohesion: 0.13
Nodes (5): query_patterns(), normalize_code(), merge_parts(), validate_vin(), vin_checksum()

### Community 80 - "Initial Warehouse Schema"
Cohesion: 0.42
Nodes (8): warehouse_product_versions, warehouse_product_versions_product_time_idx, warehouse_products, warehouse_products_active_idx, warehouse_products_last_seen_run_idx, warehouse_products_product_code_idx, warehouse_schema_migrations, warehouse_sync_runs

### Community 81 - "Durable Indexing Jobs"
Cohesion: 0.29
Nodes (6): Conservative part-code normalization (retains '/'), Stock-independent content hash (migration 0007), Enrichment index worker (leases, SKIP LOCKED, checkpoints), automotive.part_numbers code index, Migration 0007 content_hash code index, Durable indexing jobs with leases and checkpoints

### Community 82 - "Dependabot Config"
Cohesion: 0.25
Nodes (7): interval, updates, directory, open-pull-requests-limit, package-ecosystem, schedule, version

### Community 83 - "small_talk"
Cohesion: 0.33
Nodes (3): small_talk(), starts_with_greeting(), SmallTalkTests

### Community 84 - "Eval Mock Model"
Cohesion: 0.27
Nodes (3): mock_store(), MockModel, MockVinService

### Community 85 - "Analytics Star Schema Docs"
Cohesion: 0.22
Nodes (10): Authorized evidence import (CSV/JSON operator import), Compatibility statuses (VERIFIED / PROVISIONAL / CONFLICTED / UNKNOWN), Deterministic oil eligibility, ASGI transport test harness with event-loop heartbeat, Automotive API and interface QA (2026-11-08), Blocked browser checks (missing X server, sandbox crashpad), Live federation acceptance run, Selected-vehicle inventory matching (POST /inventory/vehicle) (+2 more)

### Community 86 - "Runtime Requirements"
Cohesion: 0.20
Nodes (7): Runtime requirements pin list, fastapi==0.155.0, httpx==0.28.1, psycopg[binary]==3.3.2, python-dotenv==1.2.1, requests==2.32.5, uvicorn==0.54.0

### Community 89 - "Parts Assistant Design"
Cohesion: 0.20
Nodes (6): Local SQLite chat log, Migration 0006_automotive (private schema with RLS), NHTSA vPIC VIN decoder, Ephemeral HMAC-keyed search snapshot, --setup-local local provisioning helper, 30-day sanitized VIN decode cache

### Community 91 - "test_automotive_api.py"
Cohesion: 0.29
Nodes (3): FakeChat, test_unified_search_uses_provider_rate_budget(), test_unified_vin_read_job_is_typed_pollable_and_private()

### Community 92 - "Conversation Suite Tests"
Cohesion: 0.25
Nodes (6): Automotive /api/v1 endpoint surface, Per-process rate limits (60 API / 10 provider requests per minute), Motorium ai-assistant.php protocol, Motorium VIN / part-code connector, Product JSON-LD metadata (MPN, SKU, OEM), reverse-api-engineer capture attempt

### Community 93 - "load_scenarios"
Cohesion: 0.29
Nodes (3): load_scenarios(), summarize(), ConversationSuiteTests

### Community 94 - "_designation_at"
Cohesion: 0.29
Nodes (4): _bmw_at(), _class_at(), _designation_at(), _latin()

### Community 95 - "code_question"
Cohesion: 0.25
Nodes (3): code_question(), grades(), CodeTests

### Community 97 - "Federated one-VIN search across sources"
Cohesion: 0.25
Nodes (8): Motorium VIN part-code panel, Unified all-sources VIN search panel, Key-gated VIN-to-OE catalog API client (RapidAPI), Motorium VIN-to-part-code connector, Federated one-VIN search across sources, Keyed HMAC VIN cache with sanitized responses, vPIC VIN decoding (format/checksum/warnings), POST /api/v1/vehicles/part-codes (Motorium)

### Community 125 - "AutomotiveRepository"
Cohesion: 0.15
Nodes (3): AutomotiveRepository, _evidence(), RepositoryBoundaryTests

### Community 128 - "Engine"
Cohesion: 0.24
Nodes (3): Settings, ConversationApiTests, Engine

## Ambiguous Edges - Review These
- `Automotive catalog UI page (index.html)` → `Truncated Lower Card (Below Fold)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png · relation: references
- `Configuration Refinement Card` → `Truncated Field (likely Fuel)`  [AMBIGUOUS]
  docs/automotive-review/automotive-mobile.png · relation: references

## Knowledge Gaps
- **50 isolated node(s):** `warehouse-sync`, `Anthropic API (language/tool orchestration)`, `Strict tool schemas and finish_response contract`, `Match Source (EXACT_CODE)`, `New Chat Button` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 667 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **47 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Automotive intelligence implementation progress` connect `Warehouse to PostgreSQL synchroniser README` to `Engine`, `API Request Models`, `Parts Assistant Desktop UI`, `vin_search.py`, `FastAPI App Routes`?**
  _High betweenness centrality (0.094) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `ConversationalChatTests` (e.g. with `ChatService` and `Settings`) actually correct?**
  _`ConversationalChatTests` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `warehouse-sync`, `Anthropic API (language/tool orchestration)`, `Strict tool schemas and finish_response contract` to the rest of the system?**
  _50 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Reply Composition & Grounding` be split into smaller, more focused modules?**
  _Cohesion score 0.13205128205128205 - nodes in this community are weakly interconnected._
- **Why does `Automotive catalog UI page (index.html)` connect `Parts Assistant Desktop UI` to `Federated one-VIN search across sources`, `Automotive Desktop UI`, `Automotive Mobile UI`, `Warehouse to PostgreSQL synchroniser README`, `automotive/static/app.js`, `Conversational parts assistant`, `Parts Assistant Mobile UI`, `Chat Texting Desktop UI`, `Chat Texting Mobile UI`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Automotive catalog UI page (index.html)` and `Truncated Lower Card (Below Fold)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Configuration Refinement Card` and `Truncated Field (likely Fuel)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._