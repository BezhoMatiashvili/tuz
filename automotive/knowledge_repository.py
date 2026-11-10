"""Durable learning queue and atomic, lease-bound publication."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from psycopg.types.json import Jsonb

from automotive.knowledge_types import PROMPT_VERSION, Claim, digest, normalize_vehicle
from automotive.repository import AutomotiveRepository


class KnowledgeRepository(AutomotiveRepository):
    def policies(self) -> dict:
        with self._connect() as conn:
            return {r["source"]: dict(r) for r in conn.execute(
                "SELECT * FROM automotive_knowledge.source_policies "
                "WHERE reuse_permission IN ('owned','authorized')"
            )}

    def lookup(self, vehicle: dict, categories: list[str]) -> list[dict]:
        key = digest(normalize_vehicle(vehicle))
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM automotive_knowledge.accepted_facts "
                "WHERE profile_key=%s AND category=ANY(%s) AND state='active' "
                "AND valid_until>now() ORDER BY validated_at DESC",
                (key, categories),
            )]

    def enqueue(self, context: dict, claims: list[dict]) -> bool:
        claims = [Claim.model_validate(c).model_dump() for c in claims]
        if not claims:
            return False
        profile = digest(context["vehicle"])
        fingerprint = digest([profile, context["category"], sorted(c["claim_id"] for c in claims),
                              datetime.now(UTC).date().isoformat()])
        with self._connect() as conn:
            return conn.execute(
                "SELECT automotive_knowledge.enqueue(%s,%s,%s,%s,%s) AS queued",
                (uuid.uuid4().hex, fingerprint, profile, Jsonb(context), Jsonb(claims)),
            ).fetchone()["queued"]

    def claim(self, model: str, cap: int = 25, concurrency: int = 1) -> dict | None:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM automotive_knowledge.claim_job(%s,%s,%s,%s,%s)",
                (uuid.uuid4().hex, model, PROMPT_VERSION, cap, concurrency),
            ).fetchone()

    def finish(self, job: dict, result: dict, freshness_days: int = 180) -> bool:
        """No partial publication, stale lease writes, or inferred acceptance."""
        with self._connect() as conn:
            current = conn.execute(
                "SELECT * FROM automotive_knowledge.candidate_jobs WHERE job_id=%s "
                "AND status='running' AND lease_token=%s AND lease_until>now() FOR UPDATE",
                (job["job_id"], job["lease_token"]),
            ).fetchone()
            if current is None:
                return False
            # Re-check the live policy at publication, in case authorization was removed.
            policies = {r["source"]: dict(r) for r in conn.execute(
                "SELECT * FROM automotive_knowledge.source_policies "
                "WHERE reuse_permission IN ('owned','authorized')"
            )}
            by_id = {c["claim_id"]: c for c in current["payload"]["claims"]}
            # Full fetched text stays in the audit record only.
            receipts = [{k: r[k] for k in ("receipt_id", "url", "retrieved_at")}
                        for r in result["receipts"]]
            for verdict in result["verdicts"]:
                candidate = by_id[verdict["claim_id"]]
                if verdict["verdict"] != "accepted" or candidate["source"] not in policies:
                    continue
                provenance = {"candidate": candidate, "validation": verdict,
                              "receipts": receipts, "prompt_version": PROMPT_VERSION,
                              "model": result["model"], "reuse_permission":
                              policies[candidate["source"]]["reuse_permission"]}
                # A disagreement remains suspended for an operator to resolve. A later
                # machine attempt cannot silently reinstate one side of the conflict.
                conflict = conn.execute(
                    "SELECT 1 FROM automotive_knowledge.accepted_facts WHERE profile_key=%s "
                    "AND category=%s AND kind=%s AND slot=%s AND state<>'revoked' "
                    "AND (value<>%s OR state='suspended') LIMIT 1",
                    (current["profile_key"], candidate["category"], candidate["kind"],
                     candidate["slot"], Jsonb(candidate["value"])),
                ).fetchone()
                if conflict:
                    conn.execute(
                        "UPDATE automotive_knowledge.accepted_facts SET state='suspended' "
                        "WHERE profile_key=%s AND category=%s AND kind=%s AND slot=%s "
                        "AND state='active'", (current["profile_key"], candidate["category"],
                                              candidate["kind"], candidate["slot"]),
                    )
                conn.execute(
                    "INSERT INTO automotive_knowledge.accepted_facts "
                    "(fact_id,profile_key,category,kind,slot,value,provenance,attempt_id,"
                    "valid_until,state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,"
                    "now()+make_interval(days=>%s),%s) ON CONFLICT DO NOTHING",
                    (digest([job["lease_token"], candidate["claim_id"]]), current["profile_key"],
                     candidate["category"], candidate["kind"], candidate["slot"],
                     Jsonb(candidate["value"]), Jsonb(provenance), job["lease_token"],
                     freshness_days, "suspended" if conflict else "active"),
                )
            conn.execute(
                "UPDATE automotive_knowledge.validation_audits SET status='completed',"
                "research=%s,verdicts=%s,completed_at=now() WHERE attempt_id=%s",
                (Jsonb(result["receipts"]), Jsonb(result["verdicts"]), job["lease_token"]),
            )
            conn.execute(
                "UPDATE automotive_knowledge.candidate_jobs SET status='succeeded',"
                "updated_at=now(),lease_until=NULL WHERE job_id=%s", (job["job_id"],),
            )
            return True

    def fail(self, job: dict, code: str, *, retry: bool, research: dict | None = None) -> None:
        with self._connect() as conn:
            updated = conn.execute(
                "UPDATE automotive_knowledge.candidate_jobs SET "
                "status=CASE WHEN %s AND attempts<2 THEN 'pending' ELSE 'failed' END,"
                "updated_at=now(),lease_until=NULL WHERE job_id=%s AND lease_token=%s "
                "AND status='running' AND lease_until>now() RETURNING job_id",
                (retry, job["job_id"], job["lease_token"]),
            ).fetchone()
            if updated:
                conn.execute(
                    "UPDATE automotive_knowledge.validation_audits SET status='technical_failure',"
                    "error_code=%s,research=%s,completed_at=now() WHERE attempt_id=%s",
                    (code, Jsonb(research or {}), job["lease_token"]),
                )

    def status(self) -> dict:
        with self._connect() as conn:
            counts = {r["status"]: r["count"] for r in conn.execute(
                "SELECT status,count(*) FROM automotive_knowledge.candidate_jobs GROUP BY status"
            )}
            quota = conn.execute(
                "SELECT attempts FROM automotive_knowledge.daily_quota "
                "WHERE utc_day=(now() AT TIME ZONE 'UTC')::date"
            ).fetchone()
            return {"jobs": counts, "attempts_today": quota["attempts"] if quota else 0}

    def revoke(self, fact_id: str, reason: str, actor: str) -> bool:
        if not reason.strip() or not actor.strip():
            raise ValueError("An actor and reason are required")
        with self._connect() as conn:
            return conn.execute(
                "UPDATE automotive_knowledge.accepted_facts SET state='revoked',"
                "revocation_reason=%s,revoked_by=%s,revoked_at=now() "
                "WHERE fact_id=%s AND state<>'revoked' RETURNING fact_id",
                (reason[:2000], actor[:200], fact_id),
            ).fetchone() is not None

    def prune(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE automotive_knowledge.validation_audits a SET context=NULL,research=NULL,"
                "verdicts=NULL FROM automotive_knowledge.candidate_jobs j WHERE a.job_id=j.job_id "
                "AND a.started_at<now()-interval '30 days' AND NOT EXISTS (SELECT 1 FROM "
                "automotive_knowledge.accepted_facts f WHERE f.attempt_id=a.attempt_id)"
            )
            conn.execute(
                "UPDATE automotive_knowledge.candidate_jobs j SET payload=NULL WHERE "
                "status IN ('failed','succeeded') AND updated_at<now()-interval '30 days' "
                "AND NOT EXISTS (SELECT 1 FROM automotive_knowledge.accepted_facts f JOIN "
                "automotive_knowledge.validation_audits a USING(attempt_id) "
                "WHERE a.job_id=j.job_id)"
            )
