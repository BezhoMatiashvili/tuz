"""Run the local automotive application without logging VIN-bearing request URLs."""

from __future__ import annotations

import argparse
from dataclasses import replace


def check_catalog_api(parser: argparse.ArgumentParser) -> None:
    """One sanitized smoke test: shows counts and a few codes, never the key or the VIN."""
    from getpass import getpass

    from automotive.catalog_api import DEFAULT_BROAD, CatalogApiClient
    from automotive.config import Settings
    from automotive.public_http import PublicSourceError

    settings = Settings.from_env()
    if not settings.catalog_api_key:
        parser.exit(2, "Set CATALOG_API_KEY in .env first.\n")
    client = CatalogApiClient(
        settings.catalog_api_key, base_url=settings.catalog_api_base_url,
        host=settings.catalog_api_host, key_header=settings.catalog_api_key_header,
    )
    try:
        report = client.lookup(getpass("VIN: ").strip().upper(), {}, DEFAULT_BROAD)
    except (PublicSourceError, ValueError) as exc:
        parser.exit(1, f"Catalog API check failed: {getattr(exc, 'code', 'INVALID_VIN')}\n")
    print(f"status={report['status']} vehicle={report['vehicle_label'] or '-'}")
    for key in DEFAULT_BROAD:
        rows = [i for i in report["items"] if i["category"] == key]
        sample = ", ".join(i["code"] for i in rows[:3])
        print(f"{key}: {len(rows)} articles {('e.g. ' + sample) if sample else ''}")
    print("If a category shows 0 for a car that should have parts, the response shape "
          "or part-group name needs adjusting before relying on this source.")


def run_evaluation(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Evaluate a local VIN list; prints VIN#n only and writes nothing but the misses CSV."""
    import sys

    from automotive.api import create_app
    from automotive.config import Settings
    from automotive.evaluate import DEFAULT_CATEGORIES, evaluate, read_vins, write_misses

    try:
        vins, skipped = read_vins(args.evaluate)
    except OSError:
        parser.exit(2, "The VIN file could not be read.\n")
    if not vins:
        parser.exit(2, "No valid 17-character VINs were found in the file.\n")
    categories = [c.strip() for c in args.categories.split(",") if c.strip()] or list(
        DEFAULT_CATEGORIES)
    print(f"{len(vins)} VINs, {len(categories)} requests each"
          f"{f', {skipped} lines skipped' if skipped else ''}")
    app = create_app(settings=Settings.from_env())
    store = app.state.conversations
    try:
        rows = evaluate(vins, categories, store.chat_service, store.search_service, sys.stdout)
    finally:
        store.search_service.close()
    if args.misses_out:
        print(f"{write_misses(rows, args.misses_out)} requests without an offer written "
              f"to {args.misses_out} (no VINs)")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local automotive intelligence application")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--setup-local", action="store_true",
                        help="Configure automotive roles for an existing local warehouse")
    parser.add_argument("--worker", action="store_true", help="Run the resumable enrichment worker")
    parser.add_argument("--once", action="store_true", help="Process one queued enrichment job")
    parser.add_argument("--evaluate", metavar="FILE",
                        help="Run a list of VINs (first column) through the assistant, read-only")
    parser.add_argument("--categories", default="",
                        help="Comma-separated requests for --evaluate (default: oil change, "
                             "front brake pads, air filter, cabin filter)")
    parser.add_argument("--misses-out", metavar="FILE",
                        help="Write requests with no offer (no VINs) to this CSV")
    parser.add_argument("--check-catalog-api", action="store_true",
                        help="Check the optional catalog API key with one VIN (asked privately)")
    args = parser.parse_args()
    if args.check_catalog_api:
        check_catalog_api(parser)
        return
    if args.evaluate:
        run_evaluation(parser, args)
        return
    if args.setup_local:
        from automotive.local_setup import LocalSetupError, configure_local

        try:
            result = configure_local()
        except LocalSetupError as exc:
            parser.exit(2, f"{exc}\n")
        print("Local automotive database configured; credentials saved in private .env.")
        if not result["anthropic_configured"]:
            print("Chat requires ANTHROPIC_API_KEY in .env; VIN and inventory do not.")
        print("Start the application: uv run python -m automotive")
        return
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if args.worker:
        import sys

        from automotive.worker import main as worker_main

        sys.argv = ["automotive.worker"] + (["--once"] if args.once else [])
        worker_main()
        return
    import uvicorn

    from automotive.api import create_app
    from automotive.config import Settings

    settings = replace(
        Settings.from_env(),
        allowed_origins=(f"http://localhost:{args.port}", f"http://127.0.0.1:{args.port}"),
    )
    uvicorn.run(
        create_app(settings=settings), host="127.0.0.1", port=args.port,
        access_log=False, proxy_headers=False,
    )


if __name__ == "__main__":
    main()
