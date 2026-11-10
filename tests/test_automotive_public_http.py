"""Provider quota metadata stays bounded and prevents repeated network calls."""

from __future__ import annotations

import unittest
from email.utils import formatdate
from unittest.mock import MagicMock, patch

from automotive.catalog_api import CatalogApiClient
from automotive.public_http import PublicHttp, PublicSourceError, retry_delay


class RetryMetadataTests(unittest.TestCase):
    def test_seconds_dates_and_provider_reset_metadata(self):
        self.assertEqual(retry_delay({'Retry-After': '120'}, now=1000), 120)
        self.assertEqual(retry_delay({'Retry-After': formatdate(1060, usegmt=True)}, now=1000), 60)
        self.assertEqual(retry_delay({'X-RateLimit-Requests-Reset': '180'}, now=1000), 180)
        self.assertEqual(retry_delay({'X-RateLimit-Reset': '1700000200'}, now=1700000000), 200)
        self.assertEqual(retry_delay({'Retry-After': '10', 'RateLimit-Reset': '30'}, now=1000), 30)

    def test_invalid_and_extreme_metadata_is_clamped(self):
        self.assertEqual(retry_delay({'Retry-After': 'invalid'}, now=1000), 60)
        self.assertEqual(retry_delay({'Retry-After': '-100'}, now=1000), 1)
        self.assertEqual(retry_delay({'Retry-After': '999999999'}, now=1000), 86400)
        self.assertEqual(retry_delay({'Retry-After': 'nan'}, now=1000), 60)
        self.assertEqual(retry_delay({'Retry-After': 'inf'}, now=1000), 60)

    def test_rate_limit_opens_transport_circuit_and_exception_contains_only_safe_metadata(self):
        response = MagicMock(status_code=429, headers={'Retry-After': '120'})
        with patch('automotive.public_http.requests.Session') as session:
            session.return_value.__enter__.return_value.request.return_value = response
            http = PublicHttp('https://example.test/')
            with self.assertRaises(PublicSourceError) as caught:
                http.read('GET', 'https://example.test/path')
            self.assertEqual(caught.exception.retry_after, 120)
            self.assertEqual(caught.exception.code, 'RATE_LIMITED')
            self.assertNotIn('120', str(caught.exception))
            with self.assertRaises(PublicSourceError):
                http.read('GET', 'https://example.test/path')
            self.assertEqual(session.return_value.__enter__.return_value.request.call_count, 1)
            response.close.assert_called_once()

    def test_catalog_honors_retry_delay_instead_of_immediate_retry(self):
        api = CatalogApiClient('fixture-key')
        api.http = MagicMock()
        api.http.read.side_effect = PublicSourceError('RATE_LIMITED', retry_after=3600)
        with patch('automotive.catalog_api.monotonic', return_value=1000):
            for _ in range(2):
                with self.assertRaises(PublicSourceError):
                    api._get('/api/manufacturers/list/type-id/1', None)
        self.assertEqual(api._cooldown_until, 4600)
        api.http.read.assert_called_once()

    def test_local_limit_returns_time_until_the_rolling_window_resets(self):
        http = PublicHttp('https://example.test/', per_minute=1)
        with patch('automotive.public_http.monotonic', return_value=100):
            http.reserve()
        with patch('automotive.public_http.monotonic', return_value=120):
            with self.assertRaises(PublicSourceError) as caught:
                http.reserve()
        self.assertEqual(caught.exception.retry_after, 40)


if __name__ == '__main__':
    unittest.main()
