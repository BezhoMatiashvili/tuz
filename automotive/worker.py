"""Durable enrichment worker: python -m automotive.worker --once."""

from __future__ import annotations

import argparse
import os
import time

from dotenv import load_dotenv

from automotive.repository import AutomotiveRepository


def run_once(repository: AutomotiveRepository, batch_size: int = 500) -> bool:
    try:
        job = repository.claim_job()
    except Exception:
        raise RuntimeError("Enrichment database unavailable; retry later") from None
    if job is None:
        return False
    try:
        while not repository.index_batch(job, batch_size):
            pass
    except Exception:
        try:
            repository.fail_job(job)
        except Exception:
            # A lost DB connection must not leak its error. The durable lease
            # still permits recovery from the last committed checkpoint.
            raise RuntimeError("Enrichment unavailable; lease recovery pending") from None
        raise RuntimeError("Enrichment job failed; checkpoint retained") from None
    return True


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--auto", action="store_true",
                        help="Queue an index job whenever the catalog changed")
    args = parser.parse_args()
    database_url = os.getenv("AUTOMOTIVE_ENRICHMENT_DATABASE_URL")
    if not database_url:
        parser.error("AUTOMOTIVE_ENRICHMENT_DATABASE_URL is required")
    repository = AutomotiveRepository(database_url)
    while True:
        try:
            if args.auto:
                repository.enqueue_index_if_needed()
            run_once(repository, args.batch_size)
        except RuntimeError:
            if args.once:
                raise SystemExit("Enrichment job failed; checkpoint retained") from None
        if args.once:
            return
        time.sleep(5)


if __name__ == "__main__":
    main()
