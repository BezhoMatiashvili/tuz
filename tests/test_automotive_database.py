"""Identifier, checkpoint and optional disposable-PostgreSQL acceptance checks."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import Mock

from automotive.codes import normalize_code, parse_codes, product_codes
from automotive.worker import run_once

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.getenv("WAREHOUSE_TEST_ADMIN_DATABASE_URL")


class CodeTests(unittest.TestCase):
    def test_combined_values_preserve_slash_and_manufacturer(self):
        codes = parse_codes("AP104/2 (W719/45); 03L-115-562, HU719/7X / 1457429192",
                            field="original_code", manufacturer="MANN")
        self.assertEqual([item.original for item in codes],
                         ["AP104/2", "W719/45", "03L-115-562", "HU719/7X", "1457429192"])
        self.assertTrue(all(item.manufacturer == "MANN" for item in codes))
        self.assertTrue(all(item.field == "original_code" for item in codes))
        self.assertEqual(codes[0].raw_value,
                         "AP104/2 (W719/45); 03L-115-562, HU719/7X / 1457429192")

    def test_normalization_collision_retains_originals(self):
        codes = parse_codes("AB-123, AB123")
        self.assertEqual([item.normalized for item in codes], ["AB123", "AB123"])
        self.assertEqual([item.original for item in codes], ["AB-123", "AB123"])
        self.assertNotEqual(normalize_code("AP104/2"), normalize_code("AP1042"))

    def test_cooccurrence_has_no_interchangeability_claim(self):
        codes = product_codes({"product_code": "ABC123", "original_code": "DEF456",
                               "duplicate_code": "", "brand": "maker"})
        self.assertEqual([code.field for code in codes], ["product_code", "original_code"])
        self.assertFalse(hasattr(codes[0], "compatible"))

    def test_numeric_spaces_and_non_codes(self):
        self.assertEqual(parse_codes("03 115 562")[0].normalized, "03115562")
        self.assertEqual(parse_codes("manufacturer description"), [])


class RepositoryBoundaryTests(unittest.TestCase):
    def test_missing_url_cannot_use_ambient_libpq_credentials(self):
        from unittest.mock import patch

        from automotive.repository import AutomotiveRepository

        with patch("automotive.repository.psycopg.connect") as connect:
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                AutomotiveRepository("")._connect()
        connect.assert_not_called()

    def test_product_fingerprint_binds_current_catalog_record(self):
        from automotive.repository import _product

        current = _product({"source_product_id": "p1", "row_hash": "a" * 64})
        changed = _product({"source_product_id": "p1", "row_hash": "b" * 64})
        self.assertEqual(current["source_fingerprint"], "a" * 64)
        self.assertNotEqual(current["source_fingerprint"], changed["source_fingerprint"])
        self.assertNotIn("row_hash", current)

    def test_evidence_verification_is_derived_and_preserves_supersession(self):
        from automotive.repository import _evidence

        metadata = {
            "payload": {"status": "VERIFIED", "review_status": "approved"},
            "review_status": "pending", "authority": "manufacturer",
            "reuse_permission": "owned", "reviewed_by": None,
        }
        self.assertEqual(_evidence(metadata)["verification_status"], "PROVISIONAL")
        self.assertEqual(_evidence(metadata)["status"], "PROVISIONAL")
        approved = {**metadata, "review_status": "approved", "reviewed_by": "operator"}
        self.assertEqual(_evidence(approved)["verification_status"], "VERIFIED")
        superseded = {**approved, "payload": {"superseded_by": "ev_new"}}
        result = _evidence(superseded)
        self.assertEqual(result["verification_status"], "UNKNOWN")
        self.assertEqual(result["superseded_by"], "ev_new")

    def test_manufacturer_ambiguity_downgrades_aggregate_verification(self):
        from automotive.repository import AutomotiveRepository

        repository = AutomotiveRepository("unused")
        repository.search_parts = Mock(return_value={
            "items": [{"source_product_id": "p1"}, {"source_product_id": "p2"}],
            "warnings": ["Ambiguous normalized identifier"],
        })
        result = repository._code_evidence_result(
            "AB123", [], {"status": "VERIFIED", "warnings": []},
        )
        self.assertEqual(result["status"], "PROVISIONAL")
        self.assertEqual(len(result["candidates"]), 2)
        self.assertIn("ambiguity", result["warnings"][0])

    def test_import_uses_server_review_defaults_and_keeps_source_name(self):
        from automotive.repository import AutomotiveRepository

        repository = AutomotiveRepository("unused")
        connection = Mock()
        connection.execute.return_value.fetchone.return_value = None
        context = Mock()
        context.__enter__ = Mock(return_value=connection)
        context.__exit__ = Mock(return_value=False)
        repository._connect = Mock(return_value=context)
        result = repository.import_evidence([{
            "kind": "fitment", "source_name": "Authorized supplier", "payload": {},
            "review_status": "approved", "reviewed_by": "forged",
        }])
        statement, parameters = connection.execute.call_args.args
        self.assertNotIn("review_status", statement)
        self.assertNotIn("reviewed_by", statement)
        self.assertEqual(parameters[3], "Authorized supplier")
        self.assertTrue(result["evidence_ids"][0].startswith("ev_"))


class WorkerTests(unittest.TestCase):
    def test_database_claim_failure_is_safe_and_retryable(self):
        repository = Mock()
        repository.claim_job.side_effect = RuntimeError("credential=private")
        with self.assertRaisesRegex(RuntimeError, "retry later") as error:
            run_once(repository)
        self.assertNotIn("private", str(error.exception))

    def test_failed_error_marker_leaves_lease_recovery_without_leaking(self):
        repository = Mock()
        repository.claim_job.return_value = {"checkpoint": "last-commit"}
        repository.index_batch.side_effect = RuntimeError("credential=private")
        repository.fail_job.side_effect = RuntimeError("credential=another-private")
        with self.assertRaisesRegex(RuntimeError, "lease recovery pending") as error:
            run_once(repository)
        self.assertNotIn("private", str(error.exception))

    def test_worker_checkpoints_until_complete(self):
        repository = Mock()
        job = {"job_id": "abc", "checkpoint": ""}
        repository.claim_job.return_value = job
        repository.index_batch.side_effect = [False, True]
        self.assertTrue(run_once(repository, 25))
        self.assertEqual(repository.index_batch.call_count, 2)
        repository.fail_job.assert_not_called()

    def test_worker_failure_keeps_job_and_marks_retryable(self):
        repository = Mock()
        job = {"job_id": "abc", "checkpoint": "last-committed"}
        repository.claim_job.return_value = job
        repository.index_batch.side_effect = RuntimeError("private connection secret")
        with self.assertRaisesRegex(RuntimeError, "checkpoint retained") as error:
            run_once(repository)
        self.assertNotIn("secret", str(error.exception))
        repository.fail_job.assert_called_once_with(job)

    def test_worker_no_pending_jobs(self):
        repository = Mock()
        repository.claim_job.return_value = None
        self.assertFalse(run_once(repository))


@unittest.skipUnless(ADMIN_URL, "requires disposable PostgreSQL admin URL")
class AutomotivePostgresTests(unittest.TestCase):
    """Independent temporary database, preserving other suites' warehouse data."""

    @classmethod
    def setUpClass(cls):
        import uuid

        import psycopg
        from psycopg import sql
        from psycopg.conninfo import conninfo_to_dict, make_conninfo

        from warehouse_sync.migrations import apply_migrations_on_connection

        cls.psycopg = psycopg
        cls.dbname = "auto_test_" + uuid.uuid4().hex
        cls.empty_role = "auto_none_" + uuid.uuid4().hex
        cls.admin = psycopg.connect(ADMIN_URL, autocommit=True)
        cls.admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.dbname)))
        cls.admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(
            sql.Identifier(cls.empty_role)))
        cls.admin.execute(sql.SQL("GRANT {} TO CURRENT_USER").format(
            sql.Identifier(cls.empty_role)))
        info = conninfo_to_dict(ADMIN_URL)
        info["dbname"] = cls.dbname
        cls.url = make_conninfo(**info)
        with psycopg.connect(cls.url) as conn:
            conn.execute((ROOT / "ops/sql/provision_automotive_roles.sql").read_text())
            conn.execute("GRANT automotive_runtime, automotive_enrichment TO CURRENT_USER")
            apply_migrations_on_connection(conn)
            run = conn.execute(
                "INSERT INTO warehouse.warehouse_sync_runs(status) VALUES('running') "
                "RETURNING run_id"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO warehouse.warehouse_products(source_product_id,product_code,
                original_code,brand,source_columns,row_hash,integrity_tag,source_page,
                first_seen_run_id,last_seen_run_id) VALUES
                ('p1','AP104/2','AB-123','maker','{}',%s,%s,1,%s,%s),
                ('p2','AB123','','other','{}',%s,%s,1,%s,%s)""",
                ("a" * 64, "b" * 64, run, run, "c" * 64, "d" * 64, run, run),
            )
        cls.repository_class = __import__(
            "automotive.repository", fromlist=["AutomotiveRepository"]
        ).AutomotiveRepository

    @classmethod
    def tearDownClass(cls):
        from psycopg import sql

        cls.admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
            sql.Identifier(cls.dbname)))
        cls.admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(cls.empty_role)))
        cls.admin.close()

    def _role_repository(self, role):
        repository = self.repository_class(self.url)
        original = repository._connect

        def connect():
            conn = original()
            conn.execute(self.psycopg.sql.SQL("SET ROLE {}").format(
                self.psycopg.sql.Identifier(role)))
            return conn
        repository._connect = connect
        return repository

    def test_local_setup_creates_independent_logins_preserving_catalog(self):
        import tempfile

        from dotenv import dotenv_values, set_key
        from psycopg.conninfo import conninfo_to_dict

        from automotive.local_setup import configure_local

        created_logins = []
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.touch(mode=0o600)
            set_key(str(env_path), "DATABASE_URL", self.url)
            try:
                with self.psycopg.connect(self.url) as connection:
                    before = connection.execute(
                        "SELECT source_product_id,row_hash FROM warehouse.warehouse_products "
                        "ORDER BY source_product_id"
                    ).fetchall()
                configure_local(env_path)
                values = dotenv_values(env_path)
                for key in ("AUTOMOTIVE_DATABASE_URL", "AUTOMOTIVE_ENRICHMENT_DATABASE_URL",
                            "AUTOMOTIVE_VALIDATOR_DATABASE_URL"):
                    created_logins.append(conninfo_to_dict(values[key])["user"])
                reader = self.repository_class(values["AUTOMOTIVE_DATABASE_URL"])
                enricher = self.repository_class(values["AUTOMOTIVE_ENRICHMENT_DATABASE_URL"])
                self.assertIsNotNone(reader.get_product("p2"))
                from automotive.knowledge_repository import KnowledgeRepository

                validator = KnowledgeRepository(values["AUTOMOTIVE_VALIDATOR_DATABASE_URL"])
                self.assertEqual(validator.status()["attempts_today"], 0)
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    KnowledgeRepository(values["AUTOMOTIVE_DATABASE_URL"]).status()
                with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                    reader.create_job("index", {})
                job = enricher.create_job("index", {})
                self.assertTrue(run_once(enricher, 10))
                self.assertEqual(enricher.get_job(job["job_id"])["status"], "succeeded")
                with self.psycopg.connect(self.url) as connection:
                    after = connection.execute(
                        "SELECT source_product_id,row_hash FROM warehouse.warehouse_products "
                        "ORDER BY source_product_id"
                    ).fetchall()
                self.assertEqual(before, after)
            finally:
                for login in created_logins:
                    self.admin.execute(self.psycopg.sql.SQL("DROP ROLE {}").format(
                        self.psycopg.sql.Identifier(login),
                    ))

    def test_multilingual_category_sql_filters_before_pagination(self):
        runtime = self._role_repository("automotive_runtime")
        ids = [f"category_fixture_{i:03}" for i in range(64)]
        descriptions = ["Unrelated belt"] * 60 + [
            "უკანა სამუხრუჭე ხუნდი", "Engine oil 5W30", "ზეთის ფილტრი", "Transmission oil",
        ]
        try:
            with self.psycopg.connect(self.url) as connection:
                run_id = connection.execute(
                    "SELECT first_seen_run_id FROM warehouse.warehouse_products LIMIT 1"
                ).fetchone()[0]
                for identifier, name in zip(ids, descriptions, strict=True):
                    connection.execute(
                        """INSERT INTO warehouse.warehouse_products(source_product_id,
                        product_code,product_name,product_model,source_columns,row_hash,
                        integrity_tag,source_page,first_seen_run_id,last_seen_run_id)
                        VALUES(%s,'FIXTURE123',%s,'Example Car','{}',%s,%s,1,%s,%s)""",
                        (identifier, name, "a" * 64, "b" * 64, run_id, run_id),
                    )
            for query in ("rear brake pads", "უკანა სამუხრუჭე ხუნდები", "breaks"):
                self.assertEqual(runtime.search_products(query, limit=1)["items"][0][
                    "source_product_id"], ids[60])
                result = runtime.vehicle_catalog_candidates(
                    {"make": "Example", "model": "Car"}, limit=1, part_query=query,
                )
                self.assertEqual([p["source_product_id"] for p in result["items"]], [ids[60]])
                self.assertFalse(result["has_more"])
            for query, expected in (("engine oil", ids[61]), ("oil filter", ids[62])):
                result = runtime.search_products(query, limit=1)
                self.assertEqual([p["source_product_id"] for p in result["items"]], [expected])
                self.assertFalse(result["has_more"])
                self.assertIsNone(result["items"][0]["stock"])
            self.assertEqual(runtime.search_products("' OR TRUE --")["items"], [])
        finally:
            with self.psycopg.connect(self.url) as connection:
                connection.execute(
                    "DELETE FROM warehouse.warehouse_products WHERE source_product_id=ANY(%s)",
                    (ids,),
                )

    def test_vehicle_catalog_search_uses_literal_word_boundaries_and_exclusions(self):
        runtime = self._role_repository("automotive_runtime")
        ids = ["vehicle_fixture_match", "vehicle_fixture_other"]
        try:
            with self.psycopg.connect(self.url) as connection:
                run_id = connection.execute(
                    "SELECT first_seen_run_id FROM warehouse.warehouse_products LIMIT 1"
                ).fetchone()[0]
                for product_id, description in zip(
                    ids, ["Example Car 2013", "Example Cargo 2013"], strict=True,
                ):
                    connection.execute(
                        """INSERT INTO warehouse.warehouse_products(source_product_id,
                        product_code,product_model,source_columns,row_hash,integrity_tag,
                        source_page,first_seen_run_id,last_seen_run_id)
                        VALUES(%s,'FIXTURE123',%s,'{}',%s,%s,1,%s,%s)""",
                        (product_id, description, "a" * 64, "b" * 64, run_id, run_id),
                    )
            selected = {"make": "Example", "model": "Car", "model_year": 2013}
            result = runtime.vehicle_catalog_candidates(selected)
            self.assertEqual([p["source_product_id"] for p in result["items"]], [ids[0]])
            self.assertIsNone(result["items"][0]["stock"])
            self.assertEqual(runtime.vehicle_catalog_candidates(
                selected, exclude_ids=[ids[0]],
            )["items"], [])
            self.assertEqual(runtime.vehicle_catalog_candidates(
                {"make": "Example' OR true --", "model": "Car"},
            )["items"], [])
        finally:
            with self.psycopg.connect(self.url) as connection:
                connection.execute(
                    "DELETE FROM warehouse.warehouse_products WHERE source_product_id=ANY(%s)",
                    (ids,),
                )

    def test_index_search_staleness_recovery_and_catalog_preservation(self):
        enrichment = self._role_repository("automotive_enrichment")
        runtime = self._role_repository("automotive_runtime")
        self.assertEqual(runtime.search_parts("AP104/2")["items"][0]["source_product_id"], "p1")
        job = enrichment.create_job("index", {})
        claimed = enrichment.claim_job()
        self.assertFalse(enrichment.index_batch(claimed, 1))
        with self.psycopg.connect(self.url) as conn:
            conn.execute("UPDATE automotive.enrichment_jobs SET lease_until=now()-INTERVAL '1 day' "
                         "WHERE job_id=%s", (job["job_id"],))
        self.assertTrue(run_once(enrichment, 1))
        self.assertEqual(enrichment.get_job(job["job_id"])["status"], "succeeded")
        exhausted = enrichment.create_job("index", {})
        with self.psycopg.connect(self.url) as conn:
            conn.execute(
                "UPDATE automotive.enrichment_jobs SET status='running',attempts=5,"
                "lease_until=now()-INTERVAL '1 day' WHERE job_id=%s",
                (exhausted["job_id"],),
            )
        self.assertIsNone(enrichment.claim_job())
        self.assertEqual(enrichment.get_job(exhausted["job_id"])["status"], "failed")
        self.assertEqual(enrichment.get_job(exhausted["job_id"])["error_code"], "retry_exhausted")
        self.assertEqual(len(runtime.search_parts("AB123")["items"]), 2)
        with self.psycopg.connect(self.url) as conn:
            conn.execute("UPDATE warehouse.warehouse_products SET product_code='NEW123',"
                         "row_hash=%s WHERE source_product_id='p1'", ("e" * 64,))
        self.assertEqual(runtime.search_parts("AP104/2")["items"], [])
        self.assertEqual(runtime.search_parts("NEW123")["items"][0]["source_product_id"], "p1")
        with self.psycopg.connect(self.url) as conn:
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM warehouse.warehouse_products"
            ).fetchone()[0], 2)

    def test_product_approval_is_invalidated_by_current_inventory_change(self):
        from automotive.recommendations import RecommendationService

        enrichment = self._role_repository("automotive_enrichment")
        runtime = self._role_repository("automotive_runtime")
        product = runtime.get_product("p2")
        vehicle = {"make": "Test", "model": "Synthetic", "model_year": 2020,
                   "engine_code": "TEST1"}
        imported = enrichment.import_evidence([
            {
                "kind": "service_specification", "authority": "manufacturer",
                "reuse_permission": "owned", "applicability": vehicle,
                "payload": {"category": "engine_oil", "approval": "TEST-OIL"},
            },
            {
                "kind": "product_approval", "authority": "manufacturer",
                "reuse_permission": "owned",
                "payload": {"product_id": "p2", "approval": "TEST-OIL",
                            "source_fingerprint": product["source_fingerprint"]},
            },
        ])
        with self.psycopg.connect(self.url) as conn:
            conn.execute(
                "UPDATE automotive.evidence SET review_status='approved',"
                "reviewed_by='test_independent_reviewer',reviewed_at=now() "
                "WHERE evidence_id = ANY(%s)", (imported["evidence_ids"],),
            )
        service = RecommendationService(runtime)
        self.assertEqual(service.recommend_oil(vehicle)["status"], "VERIFIED")
        with self.psycopg.connect(self.url) as conn:
            conn.execute("UPDATE warehouse.warehouse_products SET brand='changed',row_hash=%s "
                         "WHERE source_product_id='p2'", ("9" * 64,))
        self.assertEqual(service.recommend_oil(vehicle)["status"], "INSUFFICIENT_DATA")

    def test_runtime_cannot_enrich_or_read_as_public_and_import_cannot_approve(self):
        runtime = self._role_repository("automotive_runtime")
        with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
            runtime.create_job("index", {})
        enrichment = self._role_repository("automotive_enrichment")
        result = enrichment.import_evidence([{
            "kind": "fitment", "payload": {"part_code": "AP104/2", "compatible": True},
            "authority": "manufacturer", "reuse_permission": "owned",
            "review_status": "approved", "reviewed_by": "forged",
        }])
        evidence = runtime.get_evidence(result["evidence_ids"][0])
        self.assertEqual(evidence["review_status"], "pending")
        self.assertIsNone(evidence["reviewed_by"])
        with enrichment._connect() as conn:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("UPDATE automotive.evidence SET review_status='approved'")
        with enrichment._connect() as conn:
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute(
                    """INSERT INTO automotive.evidence(evidence_id,kind,reuse_permission,
                    review_status,reviewed_by,reviewed_at,payload,fingerprint)
                    VALUES('forged','fitment','owned','approved','forged',now(),'{}',%s)""",
                    ("f" * 64,),
                )
        with self.psycopg.connect(self.url, autocommit=True) as conn:
            conn.execute(self.psycopg.sql.SQL("SET ROLE {}").format(
                self.psycopg.sql.Identifier(self.empty_role)))
            with self.assertRaises(self.psycopg.errors.InsufficientPrivilege):
                conn.execute("SELECT * FROM automotive.evidence")
            conn.execute("RESET ROLE")
            conn.execute(self.psycopg.sql.SQL("GRANT USAGE ON SCHEMA automotive TO {}").format(
                self.psycopg.sql.Identifier(self.empty_role)))
            conn.execute(self.psycopg.sql.SQL(
                "GRANT SELECT ON automotive.evidence TO {}"
            ).format(self.psycopg.sql.Identifier(self.empty_role)))
            conn.execute(self.psycopg.sql.SQL("SET ROLE {}").format(
                self.psycopg.sql.Identifier(self.empty_role)))
            self.assertEqual(conn.execute("SELECT * FROM automotive.evidence").fetchall(), [])
