"""Observable click analytics and synchronous flush semantics."""
import concurrent.futures
import datetime
import threading
import unittest

from shortener.app import create_app
from .wsgi_client import Client


class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(":memory:")
        self.client = Client(self.app)
        response = self.client.request("POST", "/api/v1/links", body={"url": "https://example.com/", "custom_alias": "analytics_link"})
        self.assertEqual(response.status, 201, response.body)
        self.code = response.json()["code"]

    def tearDown(self):
        self.app.close()

    def click(self, ip="203.0.113.9", ua="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", referrer="https://example.org/source"):
        response = self.client.request("GET", "/" + self.code, ip=ip, headers={"User-Agent": ua, "Referer": referrer})
        self.assertEqual(response.status, 302, response.body)
        return response

    def stats(self, query="", code=None):
        self.app.flush_analytics()
        response = self.client.request("GET", "/api/v1/links/" + (code or self.code) + "/stats" + query)
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(response.headers.get("x-request-id"))
        self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
        data = response.json()
        self.assertEqual(data["code"], code or self.code)
        for field in ("total_clicks", "unique_visitors", "window_days"):
            self.assertIs(type(data[field]), int)
        for field in ("clicks_by_day", "top_referrers", "devices"):
            self.assertIsInstance(data[field], list)
        return data

    def assert_groups(self, data, expected_clicks):
        for row in data["clicks_by_day"]:
            self.assertIsInstance(row["day"], str)
            datetime.date.fromisoformat(row["day"])
            self.assertIs(type(row["clicks"]), int)
            self.assertGreaterEqual(row["clicks"], 0)
        self.assertEqual(sum(row["clicks"] for row in data["clicks_by_day"]), expected_clicks)
        self.assertEqual(len({row["day"] for row in data["clicks_by_day"]}), len(data["clicks_by_day"]))
        for row in data["devices"]:
            self.assertIn(row["device"], {"mobile", "tablet", "desktop", "bot", "unknown"})
            self.assertIs(type(row["clicks"]), int)
            self.assertGreaterEqual(row["clicks"], 0)
        self.assertEqual(sum(row["clicks"] for row in data["devices"]), expected_clicks)
        self.assertEqual(len({row["device"] for row in data["devices"]}), len(data["devices"]))
        for row in data["top_referrers"]:
            self.assertIsInstance(row["referrer"], str)
            self.assertIs(type(row["clicks"]), int)
            self.assertGreaterEqual(row["clicks"], 0)

    def test_ac6_flushed_clicks_report_counts_series_referrers_and_devices(self):
        self.click()
        self.click()
        self.click(ip="203.0.113.10")
        self.click(ip="203.0.113.11", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1", referrer="https://example.net/news")
        data = self.stats()
        self.assertEqual(data["total_clicks"], 4)
        self.assertEqual(data["unique_visitors"], 3)
        self.assertEqual(data["window_days"], 7)
        self.assert_groups(data, 4)
        refs = {row["referrer"]: row["clicks"] for row in data["top_referrers"]}
        self.assertEqual(refs.get("https://example.org/source"), 3)
        self.assertEqual(refs.get("https://example.net/news"), 1)

    def test_new_link_has_zero_clicks_and_visitors(self):
        data = self.stats()
        self.assertEqual(data["total_clicks"], 0)
        self.assertEqual(data["unique_visitors"], 0)
        self.assertEqual(data["window_days"], 7)
        self.assert_groups(data, 0)

    def test_days_boundaries_and_default(self):
        self.click()
        for query, expected in (("", 7), ("?days=1", 1), ("?days=7", 7), ("?days=90", 90)):
            with self.subTest(query=query):
                data = self.stats(query)
                self.assertEqual(data["window_days"], expected)
                self.assertEqual(data["total_clicks"], 1)
                self.assertEqual(data["unique_visitors"], 1)
                self.assert_groups(data, 1)

    def test_invalid_days_return_json_400(self):
        self.app.flush_analytics()
        for days in ("0", "-1", "91", "100000000000000000000000", "1.5", "7.0", "abc", "true"):
            with self.subTest(days=days):
                response = self.client.request("GET", "/api/v1/links/" + self.code + "/stats?days=" + days)
                self.assertEqual(response.status, 400, response.body)
                self.assertTrue(response.headers.get("x-request-id"))
                self.assertEqual(response.headers.get("x-content-type-options"), "nosniff")
                payload = response.json()
                self.assertEqual(set(payload), {"error"})
                self.assertEqual(set(payload["error"]), {"code", "message"})
                self.assertRegex(payload["error"]["code"], r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
                self.assertIsInstance(payload["error"]["message"], str)
                self.assertTrue(payload["error"]["message"])

    def test_visitor_identity_uses_both_ip_and_user_agent(self):
        self.click(ip="203.0.113.30", ua="test-agent-one")
        self.click(ip="203.0.113.30", ua="test-agent-one", referrer="https://example.net/different")
        self.click(ip="203.0.113.30", ua="test-agent-two")
        self.click(ip="203.0.113.31", ua="test-agent-one")
        data = self.stats()
        self.assertEqual(data["total_clicks"], 4)
        self.assertEqual(data["unique_visitors"], 3)

    def test_metadata_and_stats_reads_do_not_record_clicks(self):
        for _ in range(3):
            response = self.client.request("GET", "/api/v1/links/" + self.code)
            self.assertEqual(response.status, 200, response.body)
            self.stats()
        self.assertEqual(self.stats()["total_clicks"], 0)

    def test_analytics_are_isolated_between_links(self):
        response = self.client.request("POST", "/api/v1/links", body={"url": "https://example.org/", "custom_alias": "other_analytics"})
        self.assertEqual(response.status, 201, response.body)
        other = response.json()["code"]
        self.click()
        self.click()
        self.assertEqual(self.stats()["total_clicks"], 2)
        other_stats = self.stats(code=other)
        self.assertEqual(other_stats["total_clicks"], 0)
        self.assertEqual(other_stats["unique_visitors"], 0)

    def test_repeated_flush_is_idempotent_and_read_your_writes(self):
        for expected in range(1, 5):
            self.click()
            self.assertEqual(self.stats()["total_clicks"], expected)
            self.assertIsNone(self.app.flush_analytics())
            self.assertEqual(self.stats()["total_clicks"], expected)
        self.assertEqual(self.stats()["unique_visitors"], 1)

    def test_parallel_flushes_preserve_completed_clicks(self):
        self.click()
        self.click()
        barrier = threading.Barrier(4)

        def flush_once(_):
            barrier.wait(timeout=10)
            return self.app.flush_analytics()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(flush_once, range(4)))
        self.assertEqual(results, [None] * 4)
        self.assertEqual(self.stats()["total_clicks"], 2)
        self.assertEqual(self.stats()["unique_visitors"], 1)


if __name__ == "__main__":
    unittest.main()
