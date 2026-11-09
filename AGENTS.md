# Repository Guidelines

## Project Structure & Module Organization

`warehouse_sync/` contains the read-only source client, parser, sync service, PostgreSQL repository, configuration, and CLI. `db_ui/` provides the local database explorer; its browser assets are in `db_ui/static/`. Numbered SQL upgrade and rollback scripts live in `migrations/`. Tests are in `tests/`, operational templates in `ops/`, and analytics and production guidance in `docs/`.

## Build, Test, and Development Commands

Use Python 3.11 or newer and install the locked development dependencies with `uv sync --extra dev --locked`.

- `docker compose up -d postgres` starts the local PostgreSQL service.
- `uv run python -m warehouse_sync --migrate` applies pending migrations; `--dry-run` previews a sync.
- `docker compose up --build -d postgres db-ui` starts the database explorer at `http://localhost:8766`.
- `uv run python -m unittest discover -s tests -v` runs the test suite.
- `uv run ruff check .`, `uv run bandit -q -r warehouse_sync`, and `uv run pip-audit` run lint, security, and dependency checks used by CI.

Copy `.env.example` to `.env` for local configuration and supply local credentials before running the sync. See `README.md` for setup details.

## Coding Style & Naming Conventions

Use four-space indentation, Python type hints, and `snake_case` for modules, functions, and variables; use `PascalCase` for classes. Follow the existing small, focused modules and explicit error handling. Ruff targets Python 3.11, enforces a 100-character line limit, and checks errors, imports, and Python upgrades. Keep SQL migrations numbered with paired `.up.sql` and `.down.sql` files.

## Testing Guidelines

Tests use `unittest`; name files `test_*.py` and test methods `test_*`. Add focused tests for behavior changes. PostgreSQL security and end-to-end tests require `WAREHOUSE_TEST_ADMIN_DATABASE_URL` pointing to a disposable PostgreSQL database; they skip when it is unavailable. CI supplies this database. No coverage threshold is configured.

## Commit & Pull Request Guidelines

Git history is unavailable in this checkout, so no established commit convention can be inferred. Write concise, imperative commit subjects. Pull requests should explain the change, identify related issues, and list checks run; include screenshots for visible `db_ui` changes. Highlight migration and security implications.

## Security & Configuration

Never commit `.env`, credentials, cookies, or HMAC keys. Preserve the source integration's read-only behavior and database least-privilege model. Consult `docs/production-runbook.md` before production changes.
