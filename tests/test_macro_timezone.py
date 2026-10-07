from datetime import datetime, timezone
from unittest import TestCase

import main


class MacroTimestampTests(TestCase):
    def test_utc_runner_timestamp_is_formatted_as_bangkok_time(self):
        utc_time = datetime(2026, 10, 7, 3, 0, tzinfo=timezone.utc)

        self.assertEqual(main.format_th_timestamp(utc_time), "07/10/2026 10:00")
