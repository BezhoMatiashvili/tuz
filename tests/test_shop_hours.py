"""The staff's hours (10:00-19:00 Tbilisi, every day) and the quiet part_not_found alarm."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch

from automotive import shop_hours
from automotive.live_store import quiet

TBILISI = timezone(timedelta(hours=4))


def local(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, 21, hour, minute, second, tzinfo=TBILISI)


class ShopHoursTests(unittest.TestCase):
    def test_the_edges_of_the_day(self):
        self.assertFalse(shop_hours.is_open(local(9, 59, 59)))
        self.assertTrue(shop_hours.is_open(local(10)))
        self.assertTrue(shop_hours.is_open(local(18, 59, 59)))
        self.assertFalse(shop_hours.is_open(local(19)))
        self.assertFalse(shop_hours.is_open(local(23, 30)))
        self.assertFalse(shop_hours.is_open(local(3)))

    def test_every_day_of_the_week(self):
        for day in range(7):
            self.assertTrue(shop_hours.is_open(local(12) + timedelta(days=day)))

    def test_utc_and_naive_times_are_read_in_tbilisi(self):
        self.assertTrue(shop_hours.is_open(datetime(2026, 11, 21, 6, 0, tzinfo=UTC)))  # 10:00
        self.assertFalse(shop_hours.is_open(datetime(2026, 11, 21, 15, 0, tzinfo=UTC)))  # 19:00
        self.assertTrue(shop_hours.is_open(datetime(2026, 11, 21, 6, 0)))  # naive = UTC

    def test_now_is_the_default(self):
        with patch("automotive.shop_hours.now", return_value=local(11)):
            self.assertTrue(shop_hours.is_open())
        with patch("automotive.shop_hours.now", return_value=local(20)):
            self.assertFalse(shop_hours.is_open())


class QuietAlarmTests(unittest.TestCase):
    def test_only_a_part_not_found_after_hours_is_quiet(self):
        self.assertTrue(quiet("part_not_found", local(22)))
        self.assertTrue(quiet("part_not_found", local(9, 59)))
        self.assertFalse(quiet("part_not_found", local(10)))
        self.assertFalse(quiet("part_not_found", local(18, 59)))
        for kind in ("visit_request", "service_booking", "needs_staff"):
            self.assertFalse(quiet(kind, local(22)))
        self.assertFalse(quiet("part_not_found", None))


if __name__ == "__main__":
    unittest.main()
