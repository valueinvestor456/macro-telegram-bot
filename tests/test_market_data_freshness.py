from datetime import datetime, timedelta, timezone
from unittest import TestCase
from unittest.mock import Mock, patch

import main


class MarketDataFreshnessTests(TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)

    def payload(self, age):
        return {"updated": (self.now - age).isoformat(), "th_rate": {"last": 1.0}}

    def response(self, payload):
        return Mock(json=Mock(return_value=payload))

    def test_age_boundary_and_invalid_timestamps(self):
        self.assertTrue(main._market_data_is_fresh(self.payload(timedelta(hours=5)), self.now))
        self.assertFalse(main._market_data_is_fresh(self.payload(timedelta(hours=7)), self.now))
        self.assertFalse(main._market_data_is_fresh({"updated": "2026-10-25T11:00:00"}, self.now))
        self.assertFalse(main._market_data_is_fresh({"updated": "invalid"}, self.now))
        self.assertFalse(main._market_data_is_fresh({}, self.now))
        self.assertFalse(main._market_data_is_fresh(self.payload(-timedelta(hours=1)), self.now))

    @patch.object(main.requests, "get")
    def test_prefers_fresh_github_snapshot(self, get):
        fresh = self.payload(timedelta(minutes=20))
        get.return_value = self.response(fresh)

        self.assertEqual(main.fetch_market_data_json(), fresh)
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[0], main.MARKET_DATA_URLS[0])

    @patch.object(main.requests, "get")
    def test_stale_github_uses_only_fresh_fallback(self, get):
        fresh = self.payload(timedelta(minutes=30))
        get.side_effect = [self.response(self.payload(timedelta(days=2))), self.response(fresh)]

        self.assertEqual(main.fetch_market_data_json(), fresh)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args.args[0], main.MARKET_DATA_URLS[1])

    @patch.object(main.requests, "get")
    def test_all_stale_returns_none(self, get):
        get.side_effect = [
            self.response(self.payload(timedelta(days=1))),
            self.response(self.payload(timedelta(days=9))),
        ]

        self.assertIsNone(main.fetch_market_data_json())

    @patch.object(main.requests, "get")
    def test_primary_failure_can_use_fresh_fallback(self, get):
        fresh = self.payload(timedelta(minutes=30))
        get.side_effect = [TimeoutError("upstream unavailable"), self.response(fresh)]

        self.assertEqual(main.fetch_market_data_json(), fresh)
