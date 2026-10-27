from datetime import datetime, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

import schedule_policy as policy


class SchedulePolicyTests(TestCase):
    def due(self, timestamp):
        return policy.due_context(datetime.fromisoformat(timestamp).replace(tzinfo=ZoneInfo(policy.TIMEZONE)))

    def test_new_window_boundaries_and_utc_conversion(self):
        for stamp, expected in (
            ("2026-10-27T07:59:59", None), ("2026-10-27T08:00:00", "08:00"),
            ("2026-10-27T09:45:00", "09:30"), ("2026-10-27T12:30:00", "12:30"),
            ("2026-10-27T16:55:00", "16:30"), ("2026-10-27T16:55:01", None),
            ("2026-10-27T18:59:59", None), ("2026-10-27T19:00:00", "19:00"),
            ("2026-10-27T23:59:59", "23:30"), ("2026-10-28T00:00:00", "00:00"),
            ("2026-10-28T03:00:00", "03:00"), ("2026-10-28T03:30:00", "03:00"),
            ("2026-10-28T03:30:01", None), ("2026-10-28T04:00:00", None),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.due(stamp)[1], expected)
                local = datetime.fromisoformat(stamp).replace(tzinfo=ZoneInfo(policy.TIMEZONE))
                self.assertEqual(policy.due_context(local.astimezone(ZoneInfo("UTC"))), self.due(stamp))

    def test_weekend_and_bank_holiday_nights_belong_to_previous_business_day(self):
        for stamp, day, slot in (
            ("2026-10-31T02:30:00", "2026-10-30", "02:30"),
            ("2026-10-31T08:00:00", "2026-10-31", None),
            ("2026-11-01T02:30:00", "2026-10-31", None),
            ("2026-11-02T02:30:00", "2026-11-01", None),
            ("2026-12-10T02:30:00", "2026-12-09", "02:30"),
            ("2026-12-10T08:00:00", "2026-12-10", None),
            ("2026-12-11T02:30:00", "2026-12-10", None),
            ("2026-12-11T08:00:00", "2026-12-11", "08:00"),
        ):
            with self.subTest(stamp=stamp):
                actual_day, actual_slot, _ = self.due(stamp)
                self.assertEqual((actual_day.isoformat(), actual_slot), (day, slot))

    def test_activation_does_not_retroactively_extend_last_legacy_night(self):
        self.assertIsNone(self.due("2026-10-23T02:00:00")[1])
        self.assertIsNone(self.due("2026-10-23T08:00:00")[1])  # BOT holiday
        self.assertIsNone(self.due("2026-10-24T02:00:00")[1])
        self.assertEqual(self.due("2026-10-22T09:45:00")[1], "09:45")
        self.assertIsNone(self.due("2026-10-22T08:00:00")[1])
        self.assertIsNone(self.due("2026-10-27T02:00:00")[1])
        self.assertEqual(self.due("2026-10-26T21:30:00")[1], "21:00")
        self.assertIsNone(self.due("2026-10-26T22:00:00")[1])

    def test_each_policy_has_unique_increasing_slots_inside_nonoverlapping_windows(self):
        for p in policy.POLICIES:
            day = p.get("effective_from", "2026-10-22")
            ranks = [policy.slot_order(day, s) for s in p["send_times"]]
            self.assertEqual(ranks, sorted(set(ranks)))
            windows = [(policy.slot_order(day, w["start"]), policy.slot_order(day, w["end"])) for w in p["windows"]]
            for a, b in windows:
                self.assertLess(a, b)
            for a, b in zip(windows, windows[1:]):
                self.assertLess(a[1], b[0])
            for rank in ranks:
                self.assertEqual(sum(a <= rank <= b for a, b in windows), 1)

    def test_unconfigured_year_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "calendar"):
            self.due("2028-01-04T08:00:00")

    def test_year_end_and_new_year_use_verified_bot_calendar(self):
        for stamp, expected in (
            ("2026-12-31T03:00:00", "03:00"),  # December 30 night
            ("2026-12-31T08:00:00", None),
            ("2027-01-01T03:00:00", None),
            ("2027-01-01T08:00:00", None),
            ("2027-01-04T03:00:00", None),
            ("2027-01-04T08:00:00", "08:00"),
            ("2027-02-22T08:00:00", None),
            ("2027-10-25T08:00:00", None),
        ):
            with self.subTest(stamp=stamp):
                self.assertEqual(self.due(stamp)[1], expected)
