from datetime import datetime, timezone
from unittest.mock import patch
from unittest import TestCase

import main


class MacroTimestampTests(TestCase):
    def test_utc_runner_timestamp_is_formatted_as_bangkok_time(self):
        utc_time = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)

        self.assertEqual(main.format_th_timestamp(utc_time), "07/10/2026 10:00")

    def test_bank_holiday_is_not_a_business_day(self):
        holiday = datetime(2026, 10, 13, 9, 0, tzinfo=main.ZoneInfo(main.TIMEZONE))

        self.assertFalse(main.is_thai_bank_business_day(holiday))

    def test_weekday_not_in_holiday_calendar_is_a_business_day(self):
        business_day = datetime(2026, 10, 12, 9, 0, tzinfo=main.ZoneInfo(main.TIMEZONE))

        self.assertTrue(main.is_thai_bank_business_day(business_day))

    def test_weekend_is_not_a_business_day(self):
        weekend = datetime(2026, 10, 17, 9, 0, tzinfo=main.ZoneInfo(main.TIMEZONE))

        self.assertFalse(main.is_thai_bank_business_day(weekend))

    def test_stale_scheduled_run_outside_configured_slot_is_rejected(self):
        stale_run = datetime(2026, 10, 12, 18, 36, tzinfo=main.ZoneInfo(main.TIMEZONE))

        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}):
            self.assertFalse(main.is_expected_scheduled_macro_time(stale_run))

    def test_scheduled_run_near_configured_slot_is_allowed(self):
        near_slot = datetime(2026, 10, 12, 19, 27, tzinfo=main.ZoneInfo(main.TIMEZONE))

        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}):
            self.assertTrue(main.is_expected_scheduled_macro_time(near_slot))

    def test_morning_run_delayed_by_25_minutes_is_allowed(self):
        late_run = datetime(2026, 10, 14, 10, 25, tzinfo=main.ZoneInfo(main.TIMEZONE))
        with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}):
            self.assertTrue(main.is_expected_scheduled_macro_time(late_run))

    def test_late_grace_has_a_limit_and_does_not_allow_overnight_sends(self):
        for hour, minute, expected in ((10, 30, True), (10, 31, False), (3, 6, False), (9, 20, False)):
            with self.subTest(hour=hour, minute=minute):
                now = datetime(2026, 10, 14, hour, minute, tzinfo=main.ZoneInfo(main.TIMEZONE))
                with patch.dict("os.environ", {"GITHUB_EVENT_NAME": "schedule"}):
                    self.assertEqual(main.is_expected_scheduled_macro_time(now), expected)
