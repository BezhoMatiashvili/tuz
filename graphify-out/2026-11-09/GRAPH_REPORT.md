# Graph Report - tuz  (2026-11-09)

## Corpus Check
- 186 files · ~148,242 words
- Verdict: corpus is large enough that graph structure adds value.
- Unclassified: 25 file(s) not represented in the graph (top: (none) 7, .service 5, .timer 4)

## Summary
- 3133 nodes · 8127 edges · 157 communities (96 shown, 61 thin omitted)
- Extraction: 91% EXTRACTED · 9% INFERRED · 0% AMBIGUOUS · INFERRED: 770 edges (avg confidence: 0.87)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `4171d895`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- Store
- Conversational Chat Tests
- KnowledgePostgresTests
- Automotive Docs & Integrations
- run_once
- VIN Federation Tests
- Analytics & Automotive Schema SQL
- .lookup
- PublicSourceError
- automotive/static/app.js
- read_vehicle
- API Request Models
- CI Workflow
- db_ui/__main__.py
- VehicleInventoryService
- VINSearchCoordinator
- Sync Configuration & CLI
- Engine Matching (OATS)
- .submit
- OatsCatalog
- Parts Assistant Mobile UI
- WarehouseClient
- FastAPI App Routes
- DB Explorer Frontend
- Production Deploy Workflow
- oil_match.py
- Settings
- Docker Compose Services
- ChatLogger
- AutomotiveApiTests
- json
- ConversationStore
- Local Setup Helper
- _sync_once
- evaluate.py
- knowledge_types.py
- orchestration.py
- test_automotive_requirements.py
- AutomotiveRepository
- apply_migrations_on_connection
- OrchestratedTurn
- Eval Mock Repository
- rules.py
- fuchs.py
- Parts Assistant Desktop UI
- .fake_connection
- chat.py
- re
- generic.py
- Engine
- Automotive intelligence implementation progress
- catalog_api.py
- integrity.py
- Chat Texting Desktop UI
- test_automotive_rules.py
- .decode
- Chat Texting Mobile UI
- Automotive UI JS Tests
- Chat UI DOM Stubs
- Rules
- ._connect
- service.py
- Chat UI JS Tests
- SQLiteTTLCache
- test_automotive_catalog_fallback.py
- StoredAIConversationTests
- Automotive Desktop UI
- Automotive Mobile UI
- Automotive UI DOM Stubs
- ResolutionTests
- ChatLogTests
- re
- conversation_eval.py
- scheduler.py
- Chat Log Tests
- research.py
- OilApprovalAndCoverageTests
- Repository Guidelines & Migration Policy
- Chat Log Export
- unittest
- Initial Warehouse Schema
- test_parts_crawler_sources.py
- Dependabot Config
- _Table
- .connection
- Authorized evidence import (CSV/JSON operator import)
- 0009_parts_catalog.up.sql
- ProductionSettingsTests
- Settings
- Parts Assistant Design
- typing
- Conversation Suite Tests
- json
- Sync E2E Source Stub
- test_parts_catalog_api.py
- Project Metadata
- FakeResponse
- parser.py
- KnowledgeService
- automotive/__main__.py
- turn_metrics
- LearningSearchCoordinator
- RateLimiter
- orchestration-surface-qa.md
- vin_search.py
- Engine
- name_match.py
- mock_store
- PartsCatalogService
- matches_category
- .__init__
- ConversationTurn
- test_automotive_api.py
- test_reliability.py
- Parts catalog crawler
- knowledge_judge.py
- MockModel
- worker.py
- test_analytics_cli.py
- Runtime requirements pin list
- _MockSourceHandler

## God Nodes (most connected - your core abstractions)
1. `ConversationalChatTests` - 73 edges
2. `create_app()` - 69 edges
3. `AutomotiveRepository` - 61 edges
4. `Store` - 53 edges
5. `Settings` - 53 edges
6. `FederationTests` - 45 edges
7. `PublicSourceError` - 43 edges
8. `category_for_query()` - 42 edges
9. `normalize_code()` - 38 edges
10. `ConversationTurn` - 36 edges

## Surprising Connections (you probably didn't know these)
- `Truncated Lower Card (Below Fold)` --references--> `Automotive catalog UI page (index.html)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png → automotive/static/index.html
- `Setup` --references--> `warehouse()`  [INFERRED]
  docs/parts-crawler.md → tests/test_automotive_vin_search.py
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

## Communities (157 total, 61 thin omitted)

### Community 0 - "Store"
Cohesion: 0.06
Nodes (29): load_settings(), import_har(), import_ymm(), cmd_import_fapi(), cmd_import_har(), cmd_import_vpic(), cmd_import_ymm(), cmd_reparse() (+21 more)

### Community 1 - "Conversational Chat Tests"
Cohesion: 0.09
Nodes (4): ConversationalChatTests, finish(), response(), tool()

### Community 2 - "KnowledgePostgresTests"
Cohesion: 0.15
Nodes (8): capacity(), KnowledgePostgresTests, payload(), receipt(), SettingsTests, verdict(), VerdictCheckTests, viscosity()

### Community 3 - "Automotive Docs & Integrations"
Cohesion: 0.29
Nodes (9): Automotive intelligence MVP, Hard offer rules (always on), pip-audit vulnerable pins (lxml, pytest, python-dotenv, requests), Self-review scorecard (3.8/5), Unified VIN source search QA (2026-11-09), Warehouse preservation digest check, Staff rules file (automotive/rules.toml), Unified VIN read federation (/vehicles/search) (+1 more)

### Community 5 - "VIN Federation Tests"
Cohesion: 0.10
Nodes (7): merge_parts(), FluidFederationTests, FederationTests, MergeTests, product(), supplier(), warehouse()

### Community 6 - "Analytics & Automotive Schema SQL"
Cohesion: 0.07
Nodes (39): warehouse.finish_sync_run(), warehouse.prune_product_versions(), warehouse.record_page_seen(), warehouse.record_product(), warehouse.warehouse_sync_seen_pages, analytics.dim_date, analytics.dim_product, analytics.fact_product_current (+31 more)

### Community 7 - ".lookup"
Cohesion: 0.09
Nodes (12): _identity(), MotoriumClient, MotoriumError, ProductMetadata, _text(), Motorium VIN part-code panel, Motorium VIN-to-part-code connector, POST /api/v1/vehicles/part-codes (Motorium) (+4 more)

### Community 8 - "PublicSourceError"
Cohesion: 0.10
Nodes (6): CatalogApiClient, check_catalog_api(), PublicHttp, PublicSourceError, retry_delay(), RetryMetadataTests

### Community 9 - "automotive/static/app.js"
Cohesion: 0.12
Nodes (44): api(), byId(), chatMessage(), chatText(), chatWarnings, clearDependentResults(), clearQuickReplies(), clearVinContext() (+36 more)

### Community 10 - "read_vehicle"
Cohesion: 0.12
Nodes (8): _display(), _georgian_form(), _known_model(), _make_at(), read_vehicle(), _two_word_model(), TypedVehicle, VehicleTextTests

### Community 11 - "API Request Models"
Cohesion: 0.09
Nodes (32): CatalogProduct, ChatRequest, ConversationCard, ConversationMessage, ConversationTurn, fitments(), FluidProduct, FluidReport (+24 more)

### Community 12 - "CI Workflow"
Cohesion: 0.05
Nodes (40): permissions, runs-on, steps, GITHUB_TOKEN, POSTGRES_DB, POSTGRES_PASSWORD, POSTGRES_USER, WAREHOUSE_TEST_ADMIN_DATABASE_URL (+32 more)

### Community 13 - "db_ui/__main__.py"
Cohesion: 0.17
Nodes (7): browse(), catalog(), connect(), Handler, json_default(), main(), run_query()

### Community 14 - "VehicleInventoryService"
Cohesion: 0.15
Nodes (5): VehicleInventoryService, evidence(), product(), VehicleInventoryTests, page()

### Community 15 - "VINSearchCoordinator"
Cohesion: 0.09
Nodes (8): _key(), VINSearchCoordinator, fail(), one(), soft(), start(), start_fluid(), start_motorium()

### Community 16 - "Sync Configuration & CLI"
Cohesion: 0.17
Nodes (15): _configuration_error(), _database_url_is_production_safe(), _decode_hmac_key(), _load_environment(), load_maintenance_hmac_key(), load_migration_database_url(), _positive_float(), _positive_int() (+7 more)

### Community 17 - "Engine Matching (OATS)"
Cohesion: 0.08
Nodes (22): by_body(), by_engine(), engine_code(), engine_matches(), engine_option(), engine_options(), words(), fuel_matches() (+14 more)

### Community 19 - "OatsCatalog"
Cohesion: 0.13
Nodes (7): _number(), OatsCatalog, distance(), PakeloClient, ShellOilClient, equipment(), OatsTests

### Community 20 - "Parts Assistant Mobile UI"
Cohesion: 0.07
Nodes (35): Automotive catalog UI page (index.html), Part code search panel, Source and evidence panel, Fitment, cross-reference and oil panel, Vehicle parts (warehouse) panel, VIN and configuration forms, Conversational Parts Lookup Desktop Screenshot, Auto Catalog Header (cars, parts, fitment) with local service label (+27 more)

### Community 21 - "WarehouseClient"
Cohesion: 0.18
Nodes (3): WarehouseClientTests, WarehouseClient, Settings

### Community 22 - "FastAPI App Routes"
Cohesion: 0.05
Nodes (4): create_app(), import_evidence(), parse_import(), ConversationApiTests

### Community 23 - "DB Explorer Frontend"
Cohesion: 0.19
Nodes (22): acceptSuggestion(), activateTab(), api(), completionContext(), drawSuggestions(), escapeHTML(), exportResults(), format() (+14 more)

### Community 24 - "Production Deploy Workflow"
Cohesion: 0.06
Nodes (30): concurrency, cancel-in-progress, group, environment, name, runs-on, steps, DEPLOY_HOST (+22 more)

### Community 25 - "oil_match.py"
Cohesion: 0.12
Nodes (14): engine_family(), coverage(), engine_family_rows(), _fuel_excludes(), normalize_grade(), oil_rows(), pack_litres(), product_line() (+6 more)

### Community 26 - "Settings"
Cohesion: 0.07
Nodes (19): ChatService, CodeInput, _dump(), EmptyInput, PresentInput, tool_definitions(), _days(), _flag() (+11 more)

### Community 27 - "Docker Compose Services"
Cohesion: 0.08
Nodes (28): context, dockerfile, build, depends_on, environment, ports, restart, postgres (+20 more)

### Community 28 - "ChatLogger"
Cohesion: 0.12
Nodes (3): ChatLogger, _json(), _text()

### Community 30 - "json"
Cohesion: 0.09
Nodes (13): oil(), _matches_viscosity(), _positive_approval(), RecommendationService, _required(), _signature(), _specific_configuration(), _viscosities() (+5 more)

### Community 31 - "ConversationStore"
Cohesion: 0.12
Nodes (4): ConversationStore, cancelled(), progress(), _shown()

### Community 32 - "Local Setup Helper"
Cohesion: 0.15
Nodes (9): configure_local(), local_admin_url(), LocalSetupError, _new_login(), _require_catalog(), admin_url(), configure_local(), _new_login() (+1 more)

### Community 33 - "_sync_once"
Cohesion: 0.13
Nodes (6): SyncProgressTests, SyncCounters, WarehouseProduct, WarehouseRepository, _log_progress(), _sync_once()

### Community 34 - "evaluate.py"
Cohesion: 0.15
Nodes (8): anonymous_misses(), classify(), evaluate(), row_for(), summarize(), --evaluate VIN measurement CLI, --evaluate-chat conversation evaluation, EvaluatorTests

### Community 35 - "knowledge_types.py"
Cohesion: 0.16
Nodes (13): canonical(), Citation, claim(), Claim, digest(), frozen_context(), Judgement, normalize_vehicle() (+5 more)

### Community 36 - "orchestration.py"
Cohesion: 0.12
Nodes (17): codes_in_text(), add(), _code_identity(), conversation_tools(), EmptyInput, _identity(), NameInput, PartInput (+9 more)

### Community 37 - "test_automotive_requirements.py"
Cohesion: 0.07
Nodes (25): Parts assistant chat panel, age_hours(), availability_text(), classify(), is_stale(), _number(), parse_count(), dim_date (+17 more)

### Community 38 - "AutomotiveRepository"
Cohesion: 0.07
Nodes (5): AutomotiveRepository, sanitized(), _evidence(), _product(), RepositoryBoundaryTests

### Community 39 - "apply_migrations_on_connection"
Cohesion: 0.08
Nodes (5): AnalyticsIntegrationTests, _E2ESourceHandler, SyncEndToEndTests, apply_migrations(), apply_migrations_on_connection()

### Community 40 - "OrchestratedTurn"
Cohesion: 0.10
Nodes (3): OrchestratedTurn, DeadlineRepository, TurnBudget

### Community 42 - "rules.py"
Cohesion: 0.18
Nodes (10): _check(), load_rules(), _number(), parse_rules(), RulesError, _text(), _texts(), _year() (+2 more)

### Community 43 - "fuchs.py"
Cohesion: 0.17
Nodes (7): _covers_year(), FuchsClient, _model_covers_year(), JsFilterClient, FakeHttp, FuchsTests, JsFilterTests

### Community 44 - "Parts Assistant Desktop UI"
Cohesion: 0.15
Nodes (17): Assistant Reply: catalog candidates, fitment needs verification, Bilingual Georgian / English UI Labels, Parts Assistant Chat Panel, Conservative Stock and Fitment Confirmation Pattern, Fitment Unknown Badge, Message Composer (textarea, Send button, Ctrl/Cmd+Enter hint), New Chat Button, Part Candidate Card (Front brake pads, main/matched codes, match source) (+9 more)

### Community 46 - "chat.py"
Cohesion: 0.23
Nodes (5): normalize_code(), parse_codes(), PartCode, product_codes(), CodeTests

### Community 47 - "re"
Cohesion: 0.05
Nodes (43): _claims(), digit_tokens(), facts_text(), language_ok(), names_part_brand(), _norm(), numbers(), part_brand() (+35 more)

### Community 48 - "generic.py"
Cohesion: 0.08
Nodes (31): clean(), CrossRef, Fitment, Link, normalize_brand(), ParseResult, Part, PartRef (+23 more)

### Community 50 - "Automotive intelligence implementation progress"
Cohesion: 0.06
Nodes (34): Repository Guidelines (AGENTS.md), CI checks: ruff, bandit, pip-audit, unittest, PostgreSQL security/E2E tests (WAREHOUSE_TEST_ADMIN_DATABASE_URL), Warehouse analytics guide, Owner-only idempotent analytics refresh/backfill, OWASP ASVS Level 2 release evidence matrix, Required release attachments (pentest, SBOM, restore drill, sign-off), ASVS V10 malicious input (cell-text parser, bounded response) (+26 more)

### Community 51 - "catalog_api.py"
Cohesion: 0.17
Nodes (17): _body(), _capacity(), categories_for(), _dicts(), _engine_codes(), _fuel(), _normalized(), parse_articles() (+9 more)

### Community 52 - "integrity.py"
Cohesion: 0.23
Nodes (3): VersionIntegrityTests, verify_version_integrity_tag(), version_integrity_tag()

### Community 53 - "Chat Texting Desktop UI"
Cohesion: 0.17
Nodes (17): Chat Texting Desktop QA Screenshot, Bilingual Georgian/English Labels, Details Disclosure, Fitment and Stock Disclaimer, Match Reason ('Chosen because'), Car-name Match Fallback, New Chat Button, Part Result Card (+9 more)

### Community 54 - "test_automotive_rules.py"
Cohesion: 0.13
Nodes (8): approvals_stated(), axle_verdict(), NeverOffer, oem_code_ok(), StaffConfirmed, vehicle_consistent(), DataClassTests, HardRuleTests

### Community 55 - ".decode"
Cohesion: 0.20
Nodes (4): ProviderUnavailable, _text(), VinService, VinTests

### Community 56 - "Chat Texting Mobile UI"
Cohesion: 0.22
Nodes (16): Chat Texting Mobile QA Screenshot, Assistant Message Bubble, Brand and Part Number Line, Result Count + Front/Rear Clarifying Question, Collapsible Details Disclosure, Fitment Verification Warning Callout, Georgian-Language Chat Localization, Greeting Small-Talk Exchange (+8 more)

### Community 57 - "Automotive UI JS Tests"
Cohesion: 0.15
Nodes (13): assert, calls, context, flush(), fs, html, part, responder() (+5 more)

### Community 59 - "Rules"
Cohesion: 0.26
Nodes (3): _key(), Rules, _vehicle_matches()

### Community 60 - "._connect"
Cohesion: 0.20
Nodes (5): ResearchJudge, KnowledgeRepository, main(), run_once(), WorkerTests

### Community 61 - "service.py"
Cohesion: 0.14
Nodes (9): AuthenticationError, failure_code(), SourceChangedError, SourceProtocolError, ParsedPage, _check_page(), _fetch_and_parse(), _page_signature() (+1 more)

### Community 62 - "Chat UI JS Tests"
Cohesion: 0.15
Nodes (9): assert, calls, ctx, fixture, fs, html, responder(), timers (+1 more)

### Community 66 - "Automotive Desktop UI"
Cohesion: 0.23
Nodes (13): Automotive UI Desktop Screenshot, Archived Source Badge, Ask the Catalog (Free-text Question), Configuration Refinement Card, Evidence-first Disclaimer Pattern, Georgian-language Interface, Header: Autocatalog Brand (Vehicle · Parts · Evidence), Hero: Find the Part. Check Compatibility. (+5 more)

### Community 67 - "Automotive Mobile UI"
Cohesion: 0.19
Nodes (13): Automotive UI Mobile QA Screenshot, Autocatalog Brand Header (vehicle · parts · evidence), Manufacturer, Model, Year, Engine Code Fields, Configuration Refinement Card, Truncated Field (likely Fuel), Hero: Find the Part, Check Compatibility, Identify Vehicle Button, Year Input (optional) (+5 more)

### Community 69 - "ResolutionTests"
Cohesion: 0.13
Nodes (3): FakeCatalogHttp, ResolutionTests, TTLCacheTests

### Community 71 - "re"
Cohesion: 0.07
Nodes (21): category_for_query(), query_patterns(), _same(), _bmw_at(), _class_at(), _clean(), code_question(), continues() (+13 more)

### Community 72 - "conversation_eval.py"
Cohesion: 0.13
Nodes (12): check_turn(), Counting, evaluate(), _fill(), _hide(), _key(), legacy_language(), live_store() (+4 more)

### Community 73 - "scheduler.py"
Cohesion: 0.08
Nodes (20): ChallengeDetected, CircuitOpen, Disallowed, FetchError, HostState, Interrupted, is_challenge(), Page (+12 more)

### Community 74 - "Chat Log Tests"
Cohesion: 0.25
Nodes (8): Anthropic API (language/tool orchestration), Deterministic dialogue (answered without model call), Ephemeral in-memory chat conversations, Response model dropped note / quick_replies / act / language (fixed), Texting-style parts chat UI verification, Conversational parts assistant UI verification, Node DOM UI tests (automotive_ui_test.cjs, automotive_chat_ui_test.cjs), Strict tool schemas and finish_response contract

### Community 75 - "research.py"
Cohesion: 0.11
Nodes (11): AutomotiveResearch, _get(), _plain(), sanitize_vehicle(), _url(), AutomotiveResearchTests, Budget, MemoryCache (+3 more)

### Community 76 - "OilApprovalAndCoverageTests"
Cohesion: 0.39
Nodes (4): parts_alternatives(), vehicle_inventory(), OilApproval, OilApprovalAndCoverageTests

### Community 77 - "Repository Guidelines & Migration Policy"
Cohesion: 0.27
Nodes (7): Oil-change flow (grade, capacity, engine family), Fuchs oil chooser connector, JS Asakashi (jsfilter.jp) VIN filter connector, OATS chooser API shape (search / equipment), Pakelo oil chooser connector (per-lookup page token), Public oil and filter connectors, Shell oil chooser connector

### Community 78 - "Chat Log Export"
Cohesion: 0.33
Nodes (4): Optional TecDoc-style catalog API (CATALOG_API_KEY), Name-match fallback (NAME_MATCH_ONLY), 24-hour shared caches and prewarm, Stock source columns (store column_10 / TUZ 11 column_12)

### Community 80 - "Initial Warehouse Schema"
Cohesion: 0.42
Nodes (8): warehouse_product_versions, warehouse_product_versions_product_time_idx, warehouse_products, warehouse_products_active_idx, warehouse_products_last_seen_run_idx, warehouse_products_product_code_idx, warehouse_schema_migrations, warehouse_sync_runs

### Community 81 - "test_parts_crawler_sources.py"
Cohesion: 0.07
Nodes (12): har_pages(), CatCarAdapter, decode_step(), step_labels(), absolute(), split_application(), CatCarAndGenericTests, HarTests (+4 more)

### Community 82 - "Dependabot Config"
Cohesion: 0.25
Nodes (7): interval, updates, directory, open-pull-requests-limit, package-ecosystem, schedule, version

### Community 85 - "Authorized evidence import (CSV/JSON operator import)"
Cohesion: 0.22
Nodes (10): Authorized evidence import (CSV/JSON operator import), Compatibility statuses (VERIFIED / PROVISIONAL / CONFLICTED / UNKNOWN), Deterministic oil eligibility, ASGI transport test harness with event-loop heartbeat, Automotive API and interface QA (2026-11-08), Blocked browser checks (missing X server, sandbox crashpad), Live federation acceptance run, Selected-vehicle inventory matching (POST /inventory/vehicle) (+2 more)

### Community 86 - "0009_parts_catalog.up.sql"
Cohesion: 0.10
Nodes (27): automotive_knowledge.accepted_facts, automotive_knowledge.candidate_jobs, automotive_knowledge.claim_job(), automotive_knowledge.daily_quota, automotive_knowledge.enqueue(), automotive_knowledge.provision(), automotive_knowledge.source_policies, automotive_knowledge.validation_audits (+19 more)

### Community 88 - "Settings"
Cohesion: 0.12
Nodes (9): _float(), _int(), Settings, fetch_sample(), import_fapi(), download(), import_vpic(), latest_plain_url() (+1 more)

### Community 89 - "Parts Assistant Design"
Cohesion: 0.20
Nodes (6): Local SQLite chat log, Migration 0006_automotive (private schema with RLS), NHTSA vPIC VIN decoder, Ephemeral HMAC-keyed search snapshot, --setup-local local provisioning helper, 30-day sanitized VIN decode cache

### Community 91 - "typing"
Cohesion: 0.16
Nodes (10): _ambiguous(), _make_matches(), fluid_product(), fluid_report(), identity(), redact(), validate_vin(), vin_checksum() (+2 more)

### Community 92 - "Conversation Suite Tests"
Cohesion: 0.25
Nodes (6): Automotive /api/v1 endpoint surface, Per-process rate limits (60 API / 10 provider requests per minute), Motorium ai-assistant.php protocol, Motorium VIN / part-code connector, Product JSON-LD metadata (MPN, SKU, OEM), reverse-api-engineer capture attempt

### Community 95 - "json"
Cohesion: 0.15
Nodes (12): column_name(), export_result(), spreadsheet_value(), text_value(), xml_value(), Warehouse data explorer page (index.html), Excel/CSV/JSON export controls, Overview view (stats and table directory) (+4 more)

### Community 97 - "test_parts_catalog_api.py"
Cohesion: 0.12
Nodes (5): FakeRepository, FakeService, part(), PartsCatalogRouteTests, PartsCatalogServiceTests

### Community 120 - "FakeResponse"
Cohesion: 0.17
Nodes (4): Clock, FakeResponse, FakeSession, fetcher()

### Community 125 - "parser.py"
Cohesion: 0.15
Nodes (5): ParseProductsPageTests, parse_products_page(), _ProductRowsParser, _row_hash(), _value_at()

### Community 126 - "KnowledgeService"
Cohesion: 0.16
Nodes (5): knowledge_for(), KnowledgeService, add(), requested_categories(), KnowledgeServiceTests

### Community 127 - "automotive/__main__.py"
Cohesion: 0.17
Nodes (9): judge(), print_summary(), read_vins(), read_vins(), write_misses(), main(), run_chat_evaluation(), run_evaluation() (+1 more)

### Community 128 - "turn_metrics"
Cohesion: 0.14
Nodes (5): load_ai_scenarios(), load_scenarios(), summarize(), turn_metrics(), ConversationSuiteTests

### Community 129 - "LearningSearchCoordinator"
Cohesion: 0.17
Nodes (3): LearningSearchCoordinator, fact(), LearningSearchTests

### Community 133 - "vin_search.py"
Cohesion: 0.11
Nodes (10): applies_to(), evaluate_compatibility(), flatten_record(), _same(), verified_record(), DatabaseNotConfigured, _vehicle_scoped(), _year_scoped() (+2 more)

### Community 135 - "name_match.py"
Cohesion: 0.16
Nodes (8): _key(), _make_names(), make_pattern(), _model_core(), model_pattern(), _two_or_four(), year_fit(), YearFitTests

### Community 136 - "mock_store"
Cohesion: 0.15
Nodes (5): mock_store(), MockAISearch, MockSearch, MockVehicleCatalog, MockVinService

### Community 139 - "matches_category"
Cohesion: 0.23
Nodes (5): matches_category(), CategoryPaginationTests, page(), CategoryTests, product()

### Community 142 - ".__init__"
Cohesion: 0.10
Nodes (9): _redact(), detect_language(), grades(), part_codes(), references(), CodeTests, LanguageTests, ReferenceTests (+1 more)

### Community 143 - "ConversationTurn"
Cohesion: 0.08
Nodes (5): ConversationTurn, read(), run(), read(), requested_axle()

### Community 144 - "test_automotive_api.py"
Cohesion: 0.10
Nodes (4): FakeChat, FakeRepository, test_unified_search_uses_provider_rate_budget(), test_unified_vin_read_job_is_typed_pollable_and_private()

### Community 146 - "test_reliability.py"
Cohesion: 0.38
Nodes (4): _html(), ReliabilityTests, _settings(), sync()

### Community 147 - "Parts catalog crawler"
Cohesion: 0.18
Nodes (11): parts_catalog(), API, Bulk imports, Expected size and duration, Parts catalog crawler, Recording a HAR for blocked sites, Running, Safeguards (+3 more)

### Community 148 - "knowledge_judge.py"
Cohesion: 0.27
Nodes (6): check_verdicts(), _compact(), dataset(), InvalidJudgement, _plain(), public_url()

### Community 150 - "worker.py"
Cohesion: 0.29
Nodes (6): Conservative part-code normalization (retains '/'), Stock-independent content hash (migration 0007), Enrichment index worker (leases, SKIP LOCKED, checkpoints), automotive.part_numbers code index, Migration 0007 content_hash code index, Durable indexing jobs with leases and checkpoints

### Community 153 - "Runtime requirements pin list"
Cohesion: 0.29
Nodes (7): Runtime requirements pin list, fastapi==0.155.0, httpx==0.28.1, psycopg[binary]==3.3.2, python-dotenv==1.2.1, requests==2.32.5, uvicorn==0.54.0

## Ambiguous Edges - Review These
- `Automotive catalog UI page (index.html)` → `Truncated Lower Card (Below Fold)`  [AMBIGUOUS]
  docs/automotive-review/automotive-desktop.png · relation: references
- `Configuration Refinement Card` → `Truncated Field (likely Fuel)`  [AMBIGUOUS]
  docs/automotive-review/automotive-mobile.png · relation: references

## Knowledge Gaps
- **50 isolated node(s):** `warehouse-sync`, `dim_date`, `fact_sync_run`, `Source and evidence panel`, `VIN and configuration forms` (+45 more)
  These have ≤1 connection - possible missing edges or undocumented components. (Counts symbols only; 844 node(s) total have ≤1 connection when file, concept and rationale nodes are included.)
- **61 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Automotive intelligence implementation progress` connect `Automotive intelligence implementation progress` to `vin_search.py`, `API Request Models`, `chat.py`, `Parts Assistant Mobile UI`, `FastAPI App Routes`, `Settings`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Are the 2 inferred relationships involving `ConversationalChatTests` (e.g. with `ChatService` and `Settings`) actually correct?**
  _`ConversationalChatTests` has 2 INFERRED edges - model-reasoned connections that need verification._
- **What connects `warehouse-sync`, `dim_date`, `fact_sync_run` to the rest of the system?**
  _50 weakly-connected nodes found - possible documentation gaps or missing edges._
- **Should `Store` be split into smaller, more focused modules?**
  _Cohesion score 0.05666293393057111 - nodes in this community are weakly interconnected._
- **Why does `Automotive catalog UI page (index.html)` connect `Parts Assistant Mobile UI` to `Automotive Desktop UI`, `Automotive Mobile UI`, `test_automotive_requirements.py`, `.lookup`, `automotive/static/app.js`, `Parts Assistant Desktop UI`, `Automotive intelligence implementation progress`, `catalog_api.py`, `Chat Texting Desktop UI`, `Chat Texting Mobile UI`?**
  _High betweenness centrality (0.077) - this node is a cross-community bridge._

### Low-confidence Hints
_AMBIGUOUS edges — the extractor was unsure. Verify before acting on these._

- **What is the exact relationship between `Automotive catalog UI page (index.html)` and `Truncated Lower Card (Below Fold)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._
- **What is the exact relationship between `Configuration Refinement Card` and `Truncated Field (likely Fuel)`?**
  _Edge tagged AMBIGUOUS (relation: references) - confidence is low._