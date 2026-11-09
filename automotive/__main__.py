"""Run the local automotive application without logging VIN-bearing request URLs."""

from __future__ import annotations

import argparse
from dataclasses import replace


def main() -> None:
    parser = argparse.ArgumentParser(description="Local automotive intelligence application")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--setup-local", action="store_true",
                        help="Configure automotive roles for an existing local warehouse")
    parser.add_argument("--worker", action="store_true", help="Run the resumable enrichment worker")
    parser.add_argument("--once", action="store_true", help="Process one queued enrichment job")
    args = parser.parse_args()
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
