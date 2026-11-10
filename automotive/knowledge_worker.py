"""Separate validator process: python -m automotive.knowledge_worker process --once."""

from __future__ import annotations

import argparse
import json
import time

import anthropic
import psycopg
from pydantic import ValidationError

from automotive.config import Settings
from automotive.knowledge_judge import ResearchJudge
from automotive.knowledge_repository import KnowledgeRepository


def run_once(repository: KnowledgeRepository, judge: ResearchJudge, settings: Settings) -> bool:
    job = repository.claim(settings.knowledge_judge_model, settings.knowledge_daily_attempts,
                           settings.knowledge_concurrency)
    if not job:
        return False
    try:
        result = judge.run(job["payload"], repository.policies())
        repository.finish(job, result, settings.knowledge_freshness_days)
    except (anthropic.APIError, TimeoutError, OSError):
        repository.fail(job, "RESEARCH_TECHNICAL_FAILURE", retry=True,
                        research={"receipts": judge.receipts})
    except (ValueError, ValidationError, KeyError, TypeError):
        repository.fail(job, "INVALID_JUDGEMENT", retry=False,
                        research={"receipts": judge.receipts})
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Private automotive knowledge validator")
    parser.add_argument("command", choices=("process", "status", "revoke"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--fact-id")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    settings = Settings.from_env()
    if not settings.knowledge_validator_database_url:
        parser.exit(2, "Set AUTOMOTIVE_VALIDATOR_DATABASE_URL to a separate validator login.\n")
    repository = KnowledgeRepository(settings.knowledge_validator_database_url)
    try:
        if args.command == "status":
            print(json.dumps(repository.status()))
            return
        if args.command == "revoke":
            if not all((args.fact_id, args.actor, args.reason)):
                parser.error("revoke requires --fact-id, --actor and --reason")
            print(json.dumps({"revoked": repository.revoke(args.fact_id, args.reason, args.actor)}))
            return
        if not settings.knowledge_enabled or not settings.anthropic_api_key:
            parser.exit(2, "Enable knowledge collection and configure the judge API key first.\n")
        judge = ResearchJudge(settings)
        while True:
            repository.prune()
            processed = run_once(repository, judge, settings)
            if args.once:
                break
            if not processed:
                time.sleep(10)
    except psycopg.Error:
        parser.exit(1, "Knowledge database unavailable; credentials and server errors withheld.\n")


if __name__ == "__main__":
    main()
