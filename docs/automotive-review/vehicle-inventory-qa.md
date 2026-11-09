# Selected-vehicle inventory QA — 2026-11-09

Scope: new POST /api/v1/inventory/vehicle, deterministic evidence/catalog matching,
Georgian vehicle-parts panel and current-context Claude read tool.

- Focused service tests exercise literal catalog candidates vs verified matches, explicit
  main-code/manufacturer identity, OE co-occurrence, normalization collisions, model-year
  scope, missing engine details, body/drive restrictions, stale fingerprints, pending and
  conflicting evidence, negative exclusions and bounded prioritized results.
- API tests verify request validation, typed product output (private columns stripped),
  selected-vehicle lookup and unknown stock/compatibility values.
- Chat test verifies current vehicle binding and preserved candidate uncertainty.
- Optional disposable PostgreSQL test checks literal make/model word boundaries,
  exclusions and parameterized hostile input. This test remains skipped without the
  disposable admin URL; it is not an executed database acceptance result.
- New-code Ruff, Bandit (warehouse_sync + automotive) and JS syntax checks pass.
- Full suite: 133 total, 120 passed, 13 skipped (database/loopback dependencies).
- Actual customer database read remains blocked by OperationalError in this environment.
  Local automotive database/chat credentials are now configured; no values were printed.
- Browser MCP launch is blocked by missing X. One headless static-preview attempt failed
  with sandbox crashpad/socket restrictions. No new screenshot or interaction pass is
  claimed; existing screenshots predate this panel.

Visual regression / real browser-to-database E2E: INCONCLUSIVE / BLOCKED.
All successful technical examples above are synthetic test fixtures, not seeded customer
inventory, vehicle compatibility data or a claim about actual warehouse availability.
