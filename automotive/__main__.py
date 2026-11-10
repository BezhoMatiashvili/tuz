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


def run_chat_evaluation(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Scripted conversations, mock (offline) or live; prints {VIN1} placeholders only."""
    import json
    import os
    import re
    import sys

    from automotive import conversation_eval as chat_eval

    try:
        scenarios = chat_eval.load_scenarios(args.scenarios or chat_eval.DEFAULT_SCENARIOS)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"Scenarios could not be read: {exc}\n")
    if args.only:
        scenarios = [s for s in scenarios if args.only in s.get("tags", [])
                     or args.only == s["id"]]
    live = args.evaluate_chat == "live"
    if live:
        if not args.vins:
            parser.exit(2, "Live chat evaluation needs --vins FILE (VINs are never printed).\n")
        try:
            vins = chat_eval.read_vins(args.vins)
        except OSError:
            parser.exit(2, "The VIN file could not be read.\n")
        needed = {key for s in scenarios for t in s["turns"]
                  for key in re.findall(r"\{(VIN\d)\}", t["say"])}
        if not needed <= set(vins):
            parser.exit(2, f"The VIN file needs at least {len(needed)} VINs.\n")
        store = chat_eval.live_store()
    else:
        vins, store = dict(chat_eval.MOCK_VINS), chat_eval.mock_store()
    print(f"{len(scenarios)} scenarios, {'live' if live else 'mock'} mode")
    try:
        results = chat_eval.evaluate(store, scenarios, vins, sys.stdout, live=live)
        summary = chat_eval.summarize(results)
        if args.judge and live:
            from automotive.config import Settings

            summary["judge"] = chat_eval.judge(results, Settings.from_env(), sys.stdout)
    finally:
        store.close()
        store.search_service.close()
    chat_eval.print_summary(summary, sys.stdout)
    if args.report_out:
        fd = os.open(args.report_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"mode": "live" if live else "mock", "summary": summary,
                       "scenarios": results}, handle, ensure_ascii=False, indent=1)
        print(f"Report written to {args.report_out}")


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
    parser.add_argument("--evaluate-chat", choices=("mock", "live"),
                        help="Run scripted conversations and score accuracy, feel and speed")
    parser.add_argument("--vins", metavar="FILE",
                        help="VIN list for --evaluate-chat live ({VIN1} is the first line); "
                             "for --evaluate-assistant, name,VIN lines that fill {VIN:name} "
                             "(default: vins.csv, scenarios without their VIN are skipped)")
    parser.add_argument("--scenarios", metavar="PATH",
                        help="Scenario file or directory (default: tests/fixtures/conversations)")
    parser.add_argument("--only", metavar="TAG",
                        help="Run scenarios with this tag or id (--evaluate-assistant takes "
                             "several, comma-separated)")
    parser.add_argument("--judge", action="store_true",
                        help="Live only: one model rating of each transcript's natural feel")
    parser.add_argument("--report-out", metavar="FILE", help="Write the full report as JSON")
    parser.add_argument("--evaluate-assistant", choices=("conversations", "catalogue"),
                        help="Run the assistant engine on the real model and catalogue: its "
                             "test conversations, or the catalogue-wide check that samples "
                             "stocked rows and has a writer model ask for each (spends "
                             "money; see --budget)")
    parser.add_argument("--sample", type=int, metavar="N",
                        help="Rows of --evaluate-assistant catalogue (default: 150)")
    parser.add_argument("--seed", type=int, metavar="N",
                        help="Seed of the catalogue sample (default: 1)")
    parser.add_argument("--review", type=int, metavar="N",
                        help="Turns with cards that a stronger model reviews after "
                             "--evaluate-assistant catalogue (default: 60; 0 skips it)")
    parser.add_argument("--csv-out", metavar="FILE",
                        help="Write the reviewed offers as CSV for a parts person")
    parser.add_argument("--requests-from", metavar="FILE",
                        help="Take rows and requests from an earlier catalogue report; "
                             "with --only misses just the rows it missed, with --only "
                             "ID,ID those rows")
    parser.add_argument("--read-part-names", action="store_true",
                        help="Have a model read what the part names of the catalogue guide "
                             "mean, from the rows that carry them, and keep the readings in "
                             "AUTOMOTIVE_ASSISTANT_NAMES_PATH for the assistant's guide "
                             "(spends money; see --budget)")
    parser.add_argument("--all", action="store_true",
                        help="With --read-part-names: read every name again, not only the "
                             "names without a reading")
    parser.add_argument("--model", metavar="ID",
                        help="Model for --evaluate-assistant (default: the configured one) "
                             "or --read-part-names (default: claude-opus-5-5)")
    parser.add_argument("--audience", choices=("staff", "public"),
                        help="Which chat --evaluate-assistant talks to (default: public)")
    parser.add_argument("--budget", type=float, metavar="USD",
                        help="Stop --evaluate-assistant once the ledger total reaches this; "
                             "--read-part-names does not start beyond it (default: 40)")
    parser.add_argument("--ledger", metavar="FILE",
                        help="Spending ledger for --evaluate-assistant and --read-part-names "
                             "(default: AUTOMOTIVE_EVAL_LEDGER or "
                             "data/assistant_eval/cost_ledger.jsonl)")
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help="Conversations --evaluate-assistant runs side by side")
    args = parser.parse_args()
    if args.check_catalog_api:
        check_catalog_api(parser)
        return
    if args.read_part_names:
        import sys

        from automotive import assistant_names

        assistant_names.run(parser, args, sys.stdout)
        return
    if args.evaluate_assistant:
        import sys

        from automotive import assistant_eval

        assistant_eval.run(parser, args, sys.stdout)
        return
    if args.evaluate_chat:
        run_chat_evaluation(parser, args)
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
    from automotive.site import check_serving

    base = Settings.from_env()
    settings = replace(
        base,
        allowed_origins=(f"http://localhost:{args.port}", f"http://127.0.0.1:{args.port}",
                         *base.public_origins),
    )
    check_serving(settings)
    uvicorn.run(
        create_app(settings=settings), host="127.0.0.1", port=args.port,
        access_log=False, proxy_headers=False,
    )


if __name__ == "__main__":
    main()
