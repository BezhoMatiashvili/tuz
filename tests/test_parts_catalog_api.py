"""Crawled-catalog lookup and alternatives: ranking, merging and the API boundary."""

from __future__ import annotations

import asyncio
import contextlib
import unittest
from unittest.mock import patch

import httpx

from automotive.api import create_app
from automotive.config import Settings
from automotive.parts_catalog import PartsCatalogService


def part(part_id, brand, number, kind="aftermarket"):
    return {"id": part_id, "brand": brand, "number": number, "normalized": number,
            "name": "Oil Filter", "kind": kind, "first_source": "rockauto"}


class FakeRepository:
    def __init__(self, stock):
        self.stock = stock

    @contextlib.contextmanager
    def _connect(self):
        yield object()

    def search_parts_many(self, codes):
        return {code: self.stock[code] for code in codes if code in self.stock}


class FakeService(PartsCatalogService):
    def _parts(self, conn, normalized, brand):
        return [part(1, "ULTRA-POWER", "57047")] if normalized == "57047" else []

    def _cross_ref_rows(self, conn, ids, limit):
        return [
            {**part(2, "TOYOTA", "0415231090", "oe"), "relation": "oe", "source": "rockauto",
             "direction": "outgoing"},
            {**part(3, "FRAM", "PH4967"), "relation": "equivalent", "source": "fapi",
             "direction": "outgoing"},
            {**part(3, "FRAM", "PH4967"), "relation": "equivalent", "source": "fapi",
             "direction": "incoming"},
        ]

    def _same_slot(self, conn, ids, limit):
        return [{**part(4, "HENGST", "E1024HD234"), "shared_vehicles": 3,
                 "sources": ["rockauto"], "positions": ["Oil Filter"]},
                {**part(1, "ULTRA-POWER", "57047"), "shared_vehicles": 9,
                 "sources": ["rockauto"], "positions": ["Oil Filter"]}]

    def _vehicles(self, conn, ids, limit):
        return [{"make": "TOYOTA", "model": "CAMRY", "year_from": 2015}]


class PartsCatalogServiceTests(unittest.TestCase):
    def setUp(self):
        stock = {"E1024HD234": [{"source_product_id": "p1", "stock": {"store": 2,
                                                                       "tuz11": 0}}],
                 "PH4967": [{"source_product_id": "p2", "stock": {"store": 0, "tuz11": 0}}]}
        self.service = FakeService(FakeRepository(stock))

    def test_alternatives_rank_stocked_items_first_and_exclude_the_part_itself(self):
        result = self.service.alternatives("57 047")
        self.assertEqual(result["status"], "PROVISIONAL")
        numbers = [item["number"] for item in result["items"]]
        self.assertEqual(numbers[0], "E1024HD234")
        self.assertNotIn("57047", numbers)
        self.assertTrue(result["items"][0]["in_store"])
        self.assertEqual(numbers[1], "PH4967")  # in the warehouse, but not stocked
        self.assertTrue(all(item["status"] == "PROVISIONAL" for item in result["items"]))

    def test_cross_references_published_from_both_ends_are_merged(self):
        result = self.service.lookup("57047")
        fram = [ref for ref in result["cross_references"] if ref["number"] == "PH4967"]
        self.assertEqual(len(fram), 1)
        self.assertEqual(fram[0]["direction"], "both")
        self.assertEqual(result["vehicles"][0]["model"], "CAMRY")

    def test_unknown_code_and_empty_code(self):
        self.assertEqual(self.service.alternatives("ZZ9999")["status"], "NOT_FOUND")
        with self.assertRaises(ValueError):
            self.service.lookup("---")


class PartsCatalogRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.heartbeat = asyncio.create_task(self.pulse())
        app = create_app(settings=Settings(), repository=object(),
                         vin_service=object(), chat_service=object())
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                        base_url="http://localhost:8767")

    async def pulse(self):
        while True:
            await asyncio.sleep(0.01)

    async def asyncTearDown(self):
        await self.client.aclose()
        self.heartbeat.cancel()

    async def test_routes_pass_validated_parameters(self):
        with patch.object(PartsCatalogService, "alternatives",
                          return_value={"status": "PROVISIONAL", "items": []}) as alternatives, \
                patch.object(PartsCatalogService, "lookup",
                             return_value={"status": "NOT_FOUND", "parts": []}) as lookup:
            response = await self.client.get("/api/v1/parts/alternatives",
                                             params={"code": "AP104/2", "brand": "ATE"})
            self.assertEqual(response.status_code, 200)
            alternatives.assert_called_once_with("AP104/2", brand="ATE", limit=50)
            response = await self.client.get("/api/v1/parts/catalog",
                                             params={"code": "W 712/96", "limit": 10})
            self.assertEqual(response.json()["status"], "NOT_FOUND")
            lookup.assert_called_once_with("W 712/96", brand=None, limit=10)
        response = await self.client.get("/api/v1/parts/alternatives",
                                         params={"code": "X1", "limit": 500})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
