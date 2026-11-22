"""robots.txt rules, fetch classification, pacing, circuit breaker and backoff."""

from __future__ import annotations

import unittest

import requests

from parts_crawler.http import (
    ChallengeDetected,
    CircuitOpen,
    Disallowed,
    NetworkDown,
    PermanentFetchError,
    PoliteFetcher,
    RetryableFetchError,
    SourcePaused,
    has_default_route,
    is_challenge,
    redirect_target,
)
from parts_crawler.robots import parse_robots
from parts_crawler.scheduler import backoff

HOSTS = frozenset({"example.com"})


class FakeResponse:
    def __init__(self, status=200, body=b"<html>ok</html>", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {"Content-Type": "text/html; charset=utf-8"}

    def iter_content(self, _size):
        yield self.content

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses, robots=b"User-agent: *\nDisallow: /private/\n"):
        self.responses = list(responses)
        self.robots = robots
        self.headers = {}
        self.requested = []

    def get(self, url, **_kwargs):
        return FakeResponse(200, self.robots)

    def request(self, method, url, **_kwargs):
        self.requested.append((method, url))
        return self.responses.pop(0)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def fetcher(session, clock=None, sleeps=None, **options):
    clock = clock or Clock()
    sleeps = sleeps if sleeps is not None else []

    def sleeper(seconds):
        sleeps.append(seconds)
        clock.now += seconds
        return True

    # Tests never depend on the test machine's own network state.
    options.setdefault("network_check", lambda: True)
    return PoliteFetcher(user_agent="test", robots_token="TuzPartsCrawler", min_delay=5.0,
                         jitter=0.0, session_factory=lambda: session, clock=clock,
                         sleeper=sleeper, **options)


class RobotsTests(unittest.TestCase):
    def test_wildcards_and_longest_match(self):
        rules = parse_robots("User-agent: *\nDisallow: /*?id=*\nDisallow: /typo3/*\n"
                             "Allow: /typo3/public\n", "TuzPartsCrawler")
        self.assertFalse(rules.allowed("/part-finder/?id=4"))
        self.assertTrue(rules.allowed("/part-finder/?cat=4"))
        self.assertFalse(rules.allowed("/typo3/conf"))
        self.assertTrue(rules.allowed("/typo3/public/x"))

    def test_end_anchor(self):
        rules = parse_robots("User-agent: *\nDisallow: /*.pdf$\n", "x")
        self.assertFalse(rules.allowed("/a/b.pdf"))
        self.assertTrue(rules.allowed("/a/b.pdf?page=2"))

    def test_named_group_does_not_apply_to_other_agents_and_groups_merge(self):
        text = ("User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nDisallow: /info/\n"
                "User-agent: *\nCrawl-delay: 10\nDisallow: /cgi-bin/\n")
        rules = parse_robots(text, "TuzPartsCrawler")
        self.assertTrue(rules.allowed("/en/catalog/"))
        self.assertFalse(rules.allowed("/info/123.jpg"))
        self.assertFalse(rules.allowed("/cgi-bin/x"))
        self.assertEqual(rules.crawl_delay, 10.0)
        self.assertFalse(parse_robots(text, "ClaudeBot").allowed("/en/catalog/"))


class FetcherTests(unittest.TestCase):
    def test_success_and_pacing_between_requests(self):
        sleeps = []
        session = FakeSession([FakeResponse(), FakeResponse()])
        client = fetcher(session, sleeps=sleeps)
        page = client.fetch("https://example.com/a", allowed_hosts=HOSTS)
        self.assertEqual(page.status, 200)
        self.assertIn("ok", page.text)
        client.fetch("https://example.com/b", allowed_hosts=HOSTS)
        self.assertEqual(sleeps, [5.0])

    def test_robots_and_host_rules_are_enforced_before_any_request(self):
        session = FakeSession([])
        client = fetcher(session)
        with self.assertRaises(Disallowed):
            client.fetch("https://example.com/private/x", allowed_hosts=HOSTS)
        with self.assertRaises(Disallowed):
            client.fetch("https://other.example/x", allowed_hosts=HOSTS)
        with self.assertRaises(Disallowed):
            client.fetch("http://example.com/x", allowed_hosts=HOSTS)
        self.assertEqual(session.requested, [])

    def test_redirect_to_another_host_is_refused(self):
        session = FakeSession([FakeResponse(302, b"", {"Location": "https://evil.example/"})])
        with self.assertRaises(Disallowed):
            fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS)

    def test_rate_limit_is_retryable_with_retry_after(self):
        session = FakeSession([FakeResponse(429, b"slow down", {"Retry-After": "120"})])
        with self.assertRaises(RetryableFetchError) as caught:
            fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS)
        self.assertEqual(caught.exception.code, "RATE_LIMITED")
        self.assertEqual(caught.exception.retry_after, 120.0)

    def test_not_found_is_permanent(self):
        session = FakeSession([FakeResponse(404, b"missing")])
        with self.assertRaises(PermanentFetchError):
            fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS)

    def test_challenge_page_is_detected_not_bypassed(self):
        body = b"<html><title>Just a moment...</title><script src='/cdn-cgi/challenge-platform'>"
        session = FakeSession([FakeResponse(403, body)])
        with self.assertRaises(ChallengeDetected):
            fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS)
        self.assertEqual(len(session.requested), 1)

    def test_breaker_opens_then_pauses_source(self):
        clock = Clock()
        session = FakeSession([FakeResponse(503, b"down") for _ in range(9)])
        client = fetcher(session, clock=clock, breaker_threshold=3, breaker_seconds=60,
                         breaker_trips_to_pause=2)
        for _ in range(3):
            with self.assertRaises(RetryableFetchError):
                client.fetch("https://example.com/a", allowed_hosts=HOSTS)
        with self.assertRaises(CircuitOpen):
            client.fetch("https://example.com/a", allowed_hosts=HOSTS)
        clock.now += 61
        for _ in range(2):
            with self.assertRaises(RetryableFetchError):
                client.fetch("https://example.com/a", allowed_hosts=HOSTS)
        with self.assertRaises(SourcePaused):
            client.fetch("https://example.com/a", allowed_hosts=HOSTS)

    def test_challenge_markers(self):
        self.assertTrue(is_challenge(200, {"cf-mitigated": "challenge"}, b""))
        self.assertTrue(is_challenge(403, {}, b"<div class='g-recaptcha'>"))
        self.assertFalse(is_challenge(200, {}, b"<html>captcha mentioned in an article"))
        # A site whose normal pages embed a captcha widget: its error pages are not
        # challenges, but a real challenge header or title still is.
        self.assertFalse(is_challenge(503, {}, b"<div class='g-recaptcha'>", widget_ok=True))
        self.assertTrue(is_challenge(503, {"cf-mitigated": "challenge"}, b"", widget_ok=True))
        self.assertTrue(is_challenge(403, {}, b"<title>Just a moment...</title>",
                                     widget_ok=True))

    def test_error_page_with_captcha_widget_is_retried_not_blocked(self):
        session = FakeSession([FakeResponse(503, b"<form><div class='g-recaptcha'></div>")])
        with self.assertRaises(RetryableFetchError) as caught:
            fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS,
                                   captcha_widget=True)
        self.assertEqual(caught.exception.code, "SERVER_ERROR")

    def test_unreadable_robots_makes_the_source_wait_instead_of_skipping_urls(self):
        class Outage(FakeSession):
            robots_requests = 0

            def get(self, url, **_kwargs):
                self.robots_requests += 1
                return FakeResponse(503, b"unavailable")

        session = Outage([])
        client = fetcher(session)
        for _ in range(4):
            with self.assertRaises(CircuitOpen) as caught:
                client.fetch("https://example.com/a", allowed_hosts=HOSTS)
            self.assertEqual(caught.exception.code, "ROBOTS_UNAVAILABLE")
        self.assertEqual((session.robots_requests, session.requested), (1, []))

    def test_robots_exemption_covers_only_the_listed_paths(self):
        session = FakeSession([FakeResponse(), FakeResponse()],
                              robots=b"User-agent: *\nDisallow: /api/\nDisallow: /private/\n")
        client = fetcher(session)
        with self.assertRaises(Disallowed):
            client.fetch("https://example.com/api/x", allowed_hosts=HOSTS)
        page = client.fetch("https://example.com/api/x", allowed_hosts=HOSTS,
                            robots_exempt=("/api/",))
        self.assertEqual(page.status, 200)
        with self.assertRaises(Disallowed):
            client.fetch("https://example.com/private/x", allowed_hosts=HOSTS,
                         robots_exempt=("/api/",))
        redirect = FakeSession([FakeResponse(302, b"", {"Location": "/private/y"})],
                               robots=b"User-agent: *\nDisallow: /api/\nDisallow: /private/\n")
        with self.assertRaises(Disallowed):
            fetcher(redirect).fetch("https://example.com/api/x", allowed_hosts=HOSTS,
                                    robots_exempt=("/api/",))

    def test_malformed_responses_fail_one_url_without_raising_other_errors(self):
        class Broken(FakeSession):
            def __init__(self, error):
                super().__init__([])
                self.error = error

            def request(self, method, url, **_kwargs):
                raise self.error

        decode_error = UnicodeDecodeError("utf-8", b"\xb2", 0, 1, "invalid start byte")
        with self.assertRaises(PermanentFetchError) as caught:
            fetcher(Broken(decode_error)).fetch("https://example.com/a", allowed_hosts=HOSTS)
        self.assertEqual(caught.exception.code, "BAD_RESPONSE")
        with self.assertRaises(RetryableFetchError) as caught:
            fetcher(Broken(RuntimeError("boom"))).fetch("https://example.com/a",
                                                        allowed_hosts=HOSTS)
        self.assertEqual(caught.exception.code, "FETCH_ERROR:RuntimeError")

    def test_redirect_with_raw_bytes_is_requested_percent_encoded(self):
        self.assertEqual(redirect_target("https://example.com/a", "/provent²700.html"),
                         "https://example.com/provent%B2700.html")
        session = FakeSession([FakeResponse(302, b"", {"Location": "/provent²700.html"}),
                               FakeResponse()])
        fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS)
        self.assertEqual(session.requested[1][1], "https://example.com/provent%B2700.html")

    def test_cookies_and_headers_are_passed_per_request(self):
        class Recording(FakeSession):
            def request(self, method, url, **kwargs):
                self.kwargs = kwargs
                return super().request(method, url)

        session = Recording([FakeResponse()])
        fetcher(session).fetch("https://example.com/a", allowed_hosts=HOSTS,
                               cookies={"lang": "EN"}, headers={"Accept": "application/json"})
        self.assertEqual((session.kwargs["cookies"], session.kwargs["headers"]),
                         ({"lang": "EN"}, {"Accept": "application/json"}))


class OfflineTests(unittest.TestCase):
    class Unreachable(FakeSession):
        def request(self, method, url, **_kwargs):
            self.requested.append((method, url))
            raise requests.ConnectionError("Network is unreachable")

    def test_offline_errors_wait_and_never_trip_the_breaker(self):
        # 2026-11-21: one network drop tripped every source's breaker and paused them all.
        online = [False]
        client = fetcher(self.Unreachable([]), network_check=lambda: online[0],
                         breaker_threshold=3, breaker_trips_to_pause=1)
        for _ in range(10):
            with self.assertRaises(NetworkDown) as caught:
                client.fetch("https://example.com/a", allowed_hosts=HOSTS)
            self.assertEqual(caught.exception.code, "NETWORK")
        self.assertEqual(client.host("example.com").failures, 0)
        # Online again, the same errors are the host's and the breaker works as before.
        online[0] = True
        for _ in range(2):
            with self.assertRaises(RetryableFetchError):
                client.fetch("https://example.com/a", allowed_hosts=HOSTS)
        with self.assertRaises(SourcePaused):
            client.fetch("https://example.com/a", allowed_hosts=HOSTS)

    def test_default_route_from_proc_tables(self):
        ipv4_header = "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\n"
        ipv4_default = ipv4_header + "wlo1\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n"
        ipv4_local = ipv4_header + "wlo1\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n"
        lo_default = ("0" * 32 + " 00 " + "0" * 32 + " 00 " + "0" * 32
                      + " ffffffff 00000001 00000000 00200200 lo\n")
        wifi_default = lo_default.replace(" lo\n", " wlo1\n")
        self.assertTrue(has_default_route(ipv4_default, ""))
        self.assertFalse(has_default_route(ipv4_local, lo_default))
        self.assertTrue(has_default_route(ipv4_local, wifi_default))
        self.assertFalse(has_default_route("", ""))


class BackoffTests(unittest.TestCase):
    def test_backoff_grows_to_thirty_minutes(self):
        self.assertTrue(1.5 <= backoff(1) <= 2.5)
        self.assertTrue(6 <= backoff(2) <= 10)
        self.assertLessEqual(backoff(9), 1800 * 1.2)
        self.assertGreaterEqual(backoff(9), 1800 * 0.8)


if __name__ == "__main__":
    unittest.main()
