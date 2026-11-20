"""Command-line interface for migration and read-only warehouse synchronisation."""

from __future__ import annotations

import argparse
import logging
import signal
import sys


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Synchronise Garage warehouse products into PostgreSQL."
    )
    parser.add_argument(
        "--migrate", action="store_true", help="Apply pending PostgreSQL schema migrations."
    )
    parser.add_argument(
        "--migrate-through",
        help="Apply only through this migration version (legacy-upgrade maintenance only).",
    )
    parser.add_argument(
        "--sign-legacy-versions",
        action="store_true",
        help="Sign legacy observations using the migration owner and off-database HMAC key.",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Read only page 1; do not write PostgreSQL."
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        help="Limit this run for a safe partial import. Never deactivates products.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show progress logs.")
    return parser


def _stop_on_signals(error: type[Exception]) -> None:
    """SIGTERM (systemd's stop and timeout), SIGINT (Ctrl-C) and SIGHUP (a closed terminal)
    raise ``error`` in the scan, so the run is recorded as failed rather than left running."""

    def stop(signum: int, frame: object) -> None:
        raise error(f"signal {signum}")

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        number = getattr(signal, name, None)
        # A run started with nohup keeps ignoring a closed terminal, as before.
        if number is not None and signal.getsignal(number) is not signal.SIG_IGN:
            signal.signal(number, stop)


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Defer optional runtime dependencies until after argparse has handled --help.
    import psycopg

    from warehouse_sync.config import (
        Settings,
        load_maintenance_hmac_key,
        load_migration_database_url,
    )
    from warehouse_sync.errors import (
        AuthenticationError,
        ConfigurationError,
        SourceProtocolError,
        SyncAlreadyRunning,
        SyncInterrupted,
    )
    from warehouse_sync.migrations import apply_migrations
    from warehouse_sync.service import sync

    try:
        if args.migrate_through and not args.migrate:
            raise ConfigurationError("migration target requires migration mode")
        if args.migrate and args.sign_legacy_versions:
            raise ConfigurationError("choose one maintenance action")
        if args.migrate:
            database_url = load_migration_database_url()
            applied = apply_migrations(database_url, through=args.migrate_through)
            print("Applied migrations:" if applied else "Database is already current.")
            for version in applied:
                print(f"- {version}")
            return 0

        if args.sign_legacy_versions:
            from warehouse_sync.history import sign_unsigned_observations

            signed = sign_unsigned_observations(
                load_migration_database_url(), load_maintenance_hmac_key()
            )
            print(f"Signed {signed} legacy version records.")
            return 0

        settings = Settings.load(
            require_database=not args.dry_run,
            require_source_auth=True,
            require_hmac_key=not args.dry_run,
        )
        _stop_on_signals(SyncInterrupted)
        result = sync(settings, max_pages=args.max_pages, dry_run=args.dry_run)
    except SyncInterrupted:
        print(
            "SYNC STOPPED: interrupted. The run is recorded as failed; committed pages "
            "remain saved.",
            file=sys.stderr,
        )
        return 130
    except SyncAlreadyRunning:
        print("SYNC SKIPPED: another warehouse synchronisation is already running.",
              file=sys.stderr)
        return 3
    except AuthenticationError:
        print("SYNC FAILED: authentication_rejected", file=sys.stderr)
        return 10
    except SourceProtocolError as exc:
        print(f"SYNC FAILED: source_protocol_error: {exc}", file=sys.stderr)
        return 11
    except psycopg.Error as exc:
        sqlstate = exc.sqlstate if exc.sqlstate and exc.sqlstate.isalnum() else "unknown"
        print(
            f"SYNC FAILED: database_error (type={type(exc).__name__}, SQLSTATE={sqlstate}). "
            "Previously committed pages remain saved; see database logs for details.",
            file=sys.stderr,
        )
        return 1
    except (ConfigurationError, RuntimeError, ValueError):
        print("ERROR: unsafe or incomplete configuration.", file=sys.stderr)
        return 2
    except Exception as exc:  # Never print third-party exception text or connection strings.
        print(f"SYNC FAILED: sync_error (type={type(exc).__name__}).", file=sys.stderr)
        return 1

    scope = "complete" if result.complete else "partial"
    print(
        f"{scope.capitalize()} sync plan: {result.pages_planned} pages; "
        f"fetched={result.counters.pages_fetched}, seen={result.counters.rows_seen}, "
        f"added={result.counters.rows_added}, changed={result.counters.rows_changed}, "
        f"unchanged={result.counters.rows_unchanged}, "
        f"deactivated={result.counters.rows_deactivated}."
    )
    if result.complete and not args.dry_run:
        print(
            "Source pages matched in two scans. Automatic deactivation is disabled; "
            "the database run is recorded as partial."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
