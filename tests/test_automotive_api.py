"""API boundary tests use injected services; no provider or PostgreSQL claims."""

from __future__ import annotations

import asyncio
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

import httpx

from automotive.api import create_app
from automotive.config import Settings


class FakeRepository:
    def __init__(self) -> None:
        self.calls = []
        self.jobs = {}

    def search_parts(self, code, manufacturer=None, limit=30):
        self.calls.append((code, manufacturer, limit))
        return {"status": "PROVISIONAL", "warnings": ["Stock is unknown"], "items": [{
            "source_product_id": "text-id", "product_code": code,
            "product_name": "<script>alert(1)</script>", "brand": manufacturer or "",
            "private_column": "must not serialize", "stock": None, "price": None,
        }]}

    def get_product(self, product_id):
        return None

    def vehicle_fitment_records(self):
        return []

    def vehicle_catalog_candidates(self, vehicle, limit=30, exclude_ids=None):
        self.calls.append((vehicle, limit, exclude_ids))
        return {"items": [{"source_product_id": "candidate-1", "product_code": "AP104/2",
                           "product_model": "Example Car", "stock": None,
                           "private_column": "must not serialize"}], "has_more": False}

    def fitments(self, code, vehicle=None):
        self.calls.append((code, vehicle))
        return {"status": "UNKNOWN", "items": [], "evidence": [], "warnings": []}

    def cross_references(self, code):
        self.calls.append(code)
        return {"status": "UNKNOWN", "items": [], "evidence": [], "warnings": []}

    def create_job(self, kind, payload):
        result = {"job_id": "job-1", "kind": kind, "status": "pending", "attempts": 0}
        self.jobs[result["job_id"]] = result
        return result

    def get_job(self, job_id):
        return self.jobs.get(job_id)

    def import_evidence(self, records):
        self.calls.append(records)
        return {"status": "IMPORTED", "count": len(records)}


class FakeVin:
    def decode(self, vin, model_year=None):
        if vin.startswith("!"):
            raise ValueError("secret raw VIN " + vin)
        return {
            "status": "PROVISIONAL", "vehicle": {"make": "Example", "engine_code": None},
            "warnings": ["Engine code unknown"], "evidence": [],
        }


class FakeChat:
    def respond(self, message, vehicle=None, language="ka"):
        return {"status": "UNKNOWN", "answer": "მონაცემები არასაკმარისია.", "records": []}


class AutomotiveApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clients = []
        self.heartbeat = asyncio.create_task(self.pulse())
        self.repository = FakeRepository()
        self.writer = FakeRepository()
        self.settings = Settings(operator_token="test-operator-token" * 3)
        self.client = self.make_client()

    def make_client(self, **kwargs):
        app = create_app(
            settings=kwargs.pop("settings", self.settings),
            repository=kwargs.pop("repository", self.repository),
            enrichment_repository=self.writer,
            vin_service=kwargs.pop("vin_service", FakeVin()),
            chat_service=kwargs.pop("chat_service", FakeChat()),
            **kwargs,
        )
        client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost:8767"
        )
        self.clients.append(client)
        return client

    async def pulse(self):
        # This sandbox cannot wake an event loop from a worker thread; periodic
        # timers let the real synchronous ASGI routes complete without sockets.
        while True:
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        for client in self.clients:
            await client.aclose()
        self.heartbeat.cancel()
        try:
            await self.heartbeat
        except asyncio.CancelledError:
            pass

    async def test_reverse_oil_lookup_returns_only_reviewed_configurations(self):
        vehicle = {"make": "Test", "model": "Example", "model_year": 2020,
                   "engine_code": "TEST1", "market": "US"}
        review = {"authority": "manufacturer", "reuse_permission": "owned",
                  "review_status": "approved", "reviewed_by": "test reviewer"}

        class OilRepository(FakeRepository):
            def oil_approval_products(self, product_id=None):
                if product_id not in (None, "oil/1"):
                    return []
                return [(
                    {"source_product_id": "oil/1", "approval_content_fingerprint": "current"},
                    [{**review, "product_id": "oil/1", "approval": "TEST-OIL",
                      "approval_content_fingerprint": "current", "viscosity": "5W-30"}],
                )]

            def vehicle_specifications(self, configuration=None):
                return [{**review, "applicability": vehicle, "category": "engine_oil",
                         "approval": "TEST-OIL", "viscosity": "5W-30"}]

        client = self.make_client(repository=OilRepository())
        path = "/api/v1/recommendations/oil"
        reverse = await client.get(f"{path}/vehicles", params={"product_id": "oil/1"})
        self.assertEqual(reverse.status_code, 200)
        self.assertEqual(reverse.json()["status"], "VERIFIED")
        self.assertEqual(reverse.json()["items"][0]["vehicle"], vehicle)
        matching = await client.post(path, json={"vehicle": vehicle})
        self.assertEqual(matching.json()["items"][0]["product"]["source_product_id"], "oil/1")
        unknown = await client.get(f"{path}/vehicles", params={"product_id": "oil/2"})
        self.assertEqual(unknown.json()["status"], "INSUFFICIENT_DATA")
        self.assertEqual(unknown.json()["items"], [])

    async def test_code_with_slash_and_catalog_allowlist(self):
        response = await self.client.get(
            "/api/v1/inventory/search", params={"code": "AP104/2", "manufacturer": "Example"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.repository.calls, [("AP104/2", "Example", 30)])
        item = response.json()["items"][0]
        self.assertIsNone(item["stock"])
        self.assertIsNone(item["price"])
        self.assertNotIn("private_column", item)

    async def test_configuration_status_exposes_only_flags_not_secrets(self):
        client = self.make_client(settings=replace(
            self.settings, vin_hmac_key="h" * 32, anthropic_api_key="private-api-key",
        ))
        response = await client.get("/api/v1/status")
        self.assertFalse(response.json()["inventory_configured"])
        self.assertTrue(response.json()["vin_configured"])
        self.assertTrue(response.json()["chat_configured"])
        self.assertFalse(response.json()["provider_health_checked"])
        self.assertNotIn("private-api-key", response.text)
        self.assertNotIn("h" * 32, response.text)

    async def test_vehicle_inventory_lists_unverified_candidates_with_unknown_stock(self):
        response = await self.client.post("/api/v1/inventory/vehicle", json={
            "vehicle": {"make": "Example", "model": "Car", "model_year": 2013}, "limit": 10,
        })
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual(result["verified_count"], 0)
        self.assertEqual(result["items"][0]["status"], "UNKNOWN")
        self.assertIsNone(result["items"][0]["compatible"])
        self.assertIsNone(result["items"][0]["product"]["stock"])
        self.assertNotIn("private_column", result["items"][0]["product"])
        self.assertEqual(self.repository.calls[0][0]["model"], "Car")

    async def test_vehicle_inventory_validates_context_and_limits(self):
        for body in ({"vehicle": {}, "limit": 101},
                     {"vehicle": {"verified": True}},
                     {"vehicle": {"model_year": True}}):
            response = await self.client.post("/api/v1/inventory/vehicle", json=body)
            self.assertEqual(response.status_code, 422)
        response = await self.client.post("/api/v1/inventory/vehicle", json={"vehicle": {}})
        self.assertEqual(response.json()["status"], "INSUFFICIENT_DATA")

    async def test_motorium_lookup_is_typed_and_independent_of_local_chat_or_database(self):
        from unittest.mock import Mock

        service = Mock()
        service.lookup.return_value = {
            "provider": "Motorium", "status": "PROVISIONAL", "vehicle": {"model": "Car"},
            "vehicle_context_matched": True, "items": [{
                "provider_product_id": 17, "mpn": "AP104/2", "oem_codes": ["OE123"],
                "supplier_sku": "internal-17", "source_url": "https://motorium.ge/product.php?id=17",
                "status": "PROVISIONAL", "warnings": [], "private_field": "hidden",
            }], "has_more": False, "observed_at": "2026-11-09T00:00:00+00:00", "warnings": [],
        }
        client = self.make_client(motorium_service=service,
                                  settings=replace(self.settings, motorium_enabled=True))
        response = await client.post("/api/v1/vehicles/part-codes", json={
            "vin": "1HGCM82633A004352", "part_query": "filter", "limit": 2,
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["items"][0]["oem_codes"], ["OE123"])
        self.assertEqual(response.json()["items"][0]["supplier_sku"], "internal-17")
        self.assertNotIn("private_field", response.text)
        service.lookup.assert_called_once_with("1HGCM82633A004352", "filter", limit=2)

    async def test_motorium_disable_errors_and_validation_are_explicit(self):
        from unittest.mock import Mock

        from automotive.motorium import MotoriumError

        service = Mock()
        client = self.make_client(motorium_service=service,
                                  settings=replace(self.settings, motorium_enabled=False))
        payload = {"vin": "1HGCM82633A004352"}
        response = await client.post("/api/v1/vehicles/part-codes", json=payload)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "MOTORIUM_DISABLED")
        service.lookup.assert_not_called()
        self.assertFalse(Settings().motorium_enabled)  # Off unless explicitly enabled.
        client = self.make_client(motorium_service=service,
                                  settings=replace(self.settings, motorium_enabled=True))
        service.lookup.side_effect = MotoriumError("RATE_LIMITED")
        response = await client.post("/api/v1/vehicles/part-codes", json=payload)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["detail"]["code"], "MOTORIUM_RATE_LIMITED")
        for bad in ({"vin": "secret-short"}, {**payload, "limit": 7},
                    {**payload, "history": []}):
            response = await client.post("/api/v1/vehicles/part-codes", json=bad)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn("secret-short", response.text)

    async def test_vin_works_with_real_service_and_no_database(self):
        from automotive.repository import AutomotiveRepository
        from automotive.vin import VinService

        repo = AutomotiveRepository("")
        client = self.make_client(repository=repo, vin_service=VinService(repo, "h" * 32))
        with patch("automotive.vin.requests.post") as post:
            post.return_value.status_code = 200
            post.return_value.iter_content.return_value = [json.dumps({
                "Results": [{"Make": "Example", "Model": "Car"}],
            }).encode()]
            response = await client.post("/api/v1/vehicles/identify", json={
                "vin": "1M8GDM9AXKP042788",
            })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["vehicle"]["make"], "Example")
        self.assertTrue(any("ქეში" in w for w in response.json()["warnings"]))

    async def test_missing_database_and_vin_provider_have_distinct_safe_errors(self):
        from automotive.repository import AutomotiveRepository

        client = self.make_client(repository=AutomotiveRepository(""))
        response = await client.get("/api/v1/inventory/search", params={"code": "AP104/2"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "DATABASE_NOT_CONFIGURED")
        vin = FakeVin()
        vin.decode = lambda *args, **kwargs: self.raise_provider_error()
        client = self.make_client(vin_service=vin)
        response = await client.post("/api/v1/vehicles/identify", json={
            "vin": "1M8GDM9AXKP042788",
        })
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"]["code"], "VIN_PROVIDER_UNAVAILABLE")
        self.assertNotIn("private", response.text)

    @staticmethod
    def raise_provider_error():
        from automotive.vin import ProviderUnavailable

        raise ProviderUnavailable("private server connection detail")

    async def test_slash_fitment_and_cross_reference_query(self):
        for path in ("fitments", "cross-references"):
            response = await self.client.get("/api/v1/parts/" + path, params={"code": "AP104/2"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "UNKNOWN")

    async def test_negative_fitment_and_collision_candidates_are_preserved(self):
        self.repository.fitments = lambda code, vehicle=None: {
            "status": "VERIFIED", "compatible": False, "items": [], "evidence": [],
            "candidates": [{"source_product_id": "part-1", "source_fingerprint": "abc"}],
        }
        response = await self.client.get("/api/v1/parts/fitments?code=AP104%2F2")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["compatible"], False)
        self.assertEqual(response.json()["candidates"][0]["source_fingerprint"], "abc")

    async def test_vin_format_errors_never_echo_input(self):
        vin = "!" * 17
        response = await self.client.post("/api/v1/vehicles/identify", json={"vin": vin})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(vin, response.text)
        response = await self.client.post("/api/v1/vehicles/identify", json={"vin": "short-secret"})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("short-secret", response.text)

    async def test_untrusted_context_cannot_claim_provider_verification(self):
        response = await self.client.post("/api/v1/chat", json={
            "message": "ზეთი", "vehicle": {"provider_status": "VERIFIED", "engine_code": "X"},
        })
        self.assertEqual(response.status_code, 422)

    async def test_disallowed_origin_and_host(self):
        response = await self.client.get("/api/v1/health", headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        response = await self.client.get("/api/v1/health", headers={"Host": "evil.example"})
        self.assertEqual(response.status_code, 400)

    async def test_limited_provider_calls(self):
        client = self.make_client(
            settings=replace(self.settings, expensive_rate_limit_per_minute=1)
        )
        self.assertEqual(
            (await client.post("/api/v1/chat", json={"message": "hello"})).status_code, 200
        )
        self.assertEqual(
            (await client.post("/api/v1/chat", json={"message": "hello"})).status_code, 429
        )

    async def test_turn_polling_has_its_own_bounded_allowance(self):
        client = self.make_client(settings=replace(self.settings, rate_limit_per_minute=3))
        poll = f"/api/v1/chat/conversations/{'c' * 43}/turns/{'t' * 32}"
        for _ in range(15):
            self.assertNotEqual((await client.get(poll)).status_code, 429)
        self.assertEqual((await client.get(poll)).status_code, 429)
        for _ in range(3):
            self.assertEqual((await client.get("/api/v1/health")).status_code, 200)
        self.assertEqual((await client.get("/api/v1/health")).status_code, 429)

    async def test_operator_required_and_job_persisted_before_accepted(self):
        url = "/api/v1/operator/enrichment/jobs"
        response = await self.client.post(url, json={"kind": "index"})
        self.assertEqual(response.status_code, 401)
        headers = {"Authorization": "Bearer " + self.settings.operator_token}
        response = await self.client.post(url, json={"kind": "index"}, headers=headers)
        self.assertEqual(response.status_code, 202)
        self.assertIn("job-1", self.writer.jobs)
        self.assertEqual(self.repository.jobs, {})
        status = await self.client.get(url + "/job-1", headers=headers)
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.json()["status"], "pending")
        self.assertEqual((await self.client.get(url + "/job-1")).status_code, 401)

    async def test_imported_verified_label_is_pending(self):
        content = json.dumps([{
            "kind": "fitment", "source_name": "Owned feed", "authority": "Supplier",
            "reuse_permission": "owned", "review_status": "VERIFIED",
            "payload": {"part_code": "AP104/2", "status": "VERIFIED"},
        }])
        response = await self.client.post("/api/v1/operator/evidence/import", json={
            "format": "json", "content": content,
        }, headers={"Authorization": "Bearer " + self.settings.operator_token})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.writer.calls[0][0]["review_status"], "pending")
        self.assertNotIn("status", self.writer.calls[0][0]["payload"])

    async def test_body_limit_and_security_headers(self):
        response = await self.client.post("/api/v1/chat", content="x" * 1_100_001)
        self.assertEqual(response.status_code, 413)
        # The staff workspace moved from "/" to "/staff"; "/" is now the public landing page.
        response = await self.client.get("/staff")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        script = (await self.client.get("/static/app.js")).text
        self.assertNotIn("innerHTML", script)
        self.assertIn("textContent", script)
        self.assertNotIn(self.settings.operator_token, script)

    async def test_boolean_year_is_not_a_vehicle_configuration(self):
        response = await self.client.post("/api/v1/chat", json={
            "message": "oil", "vehicle": {"model_year": True},
        })
        self.assertEqual(response.status_code, 422)

    async def test_cli_custom_port_updates_allowed_origins(self):
        from automotive.__main__ import main

        with (patch("sys.argv", ["automotive", "--port", "8877"]),
              patch("automotive.config.Settings.from_env", return_value=self.settings),
              patch("automotive.api.create_app") as factory,
              patch("uvicorn.run") as runner):
            main()
        self.assertEqual(
            factory.call_args.kwargs["settings"].allowed_origins,
            ("http://localhost:8877", "http://127.0.0.1:8877"),
        )
        self.assertFalse(runner.call_args.kwargs["access_log"])
        self.assertFalse(runner.call_args.kwargs["proxy_headers"])

    async def test_provider_failure_is_generic(self):
        def failure(*args, **kwargs):
            raise RuntimeError("postgres password=private; VIN=private")
        self.repository.search_parts = failure
        response = await self.client.get("/api/v1/inventory/search?code=X")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("password", response.text)
        self.assertNotIn("VIN", response.text)


if __name__ == "__main__":
    unittest.main()

    async def test_unified_vin_read_job_is_typed_pollable_and_private(self):
        from unittest.mock import Mock

        from automotive.vin_search import VINSearchCoordinator

        selected = {'make': 'Example', 'model': 'Car', 'model_year': 2013}
        decoder = Mock()
        decoder.decode.return_value = {'vehicle': selected, 'warnings': []}
        motorium = Mock()
        motorium.lookup.return_value = {
            'vehicle': selected, 'vehicle_context_matched': True, 'items': [],
            'has_more': False, 'status': 'INSUFFICIENT_DATA',
        }
        service = VINSearchCoordinator(self.repository, decoder, motorium, 'k' * 32,
                                       queries=('only',))
        self.addCleanup(service.close)
        client = self.make_client(search_service=service)
        vin = '1HGCM82633A004352'
        response = await client.post('/api/v1/vehicles/search', json={'vin': vin})
        self.assertEqual(response.status_code, 202)
        identifier = response.json()['search_id']
        for _ in range(20):
            response = await client.get('/api/v1/vehicles/search/' + identifier)
            if response.json()['status'] != 'RUNNING':
                break
            await asyncio.sleep(0.01)
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.json()['status'], 'RUNNING')
        self.assertEqual(response.json()['total'], 1)
        self.assertEqual(len(response.json()['sources']), 7)
        self.assertNotIn('private_column', response.text)
        self.assertNotIn(vin, response.text)
        self.assertFalse(response.json()['coverage_complete'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        invalid = await client.get('/api/v1/vehicles/search/' + identifier, params={'limit': 101})
        self.assertEqual(invalid.status_code, 422)
        expired = await client.get('/api/v1/vehicles/search/' + '0' * 32)
        self.assertEqual(expired.status_code, 404)
        self.assertEqual(expired.json()['detail']['code'], 'SEARCH_EXPIRED')
        invalid = await client.post('/api/v1/vehicles/search', json={'vin': '!' * 17})
        self.assertEqual(invalid.status_code, 422)
        self.assertNotIn('!' * 17, invalid.text)
        invalid = await client.post('/api/v1/vehicles/search', json={
            'vin': vin, 'configuration': {'model_year': True},
        })
        self.assertEqual(invalid.status_code, 422)

    async def test_unified_search_uses_provider_rate_budget(self):
        from unittest.mock import Mock

        service = Mock()
        service.start.side_effect = RuntimeError('SEARCH_CAPACITY')
        client = self.make_client(search_service=service, settings=replace(
            self.settings, expensive_rate_limit_per_minute=1,
        ))
        first = await client.post('/api/v1/vehicles/search', json={'vin': '1HGCM82633A004352'})
        self.assertEqual(first.status_code, 429)
        self.assertEqual(first.json()['detail']['code'], 'SEARCH_CAPACITY')
        second = await client.post('/api/v1/vehicles/search', json={'vin': '1HGCM82633A004352'})
        self.assertEqual(second.status_code, 429)
        self.assertEqual(service.start.call_count, 1)
