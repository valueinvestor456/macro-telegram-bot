import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import main


class MacroDeliveryTests(TestCase):
    def test_one_shot_delivery_failure_has_nonzero_exit(self):
        with (
            patch.object(main.sys, "argv", ["main.py", "--once"]),
            patch.object(main, "job", return_value=False),
        ):
            with self.assertRaises(SystemExit) as error:
                main.main()
            self.assertEqual(error.exception.code, 1)

    def test_one_shot_skip_is_not_a_delivery_failure(self):
        with (
            patch.object(main.sys, "argv", ["main.py", "--once"]),
            patch.object(main, "job", return_value=None),
        ):
            main.main()

    def test_holiday_or_stale_run_never_fetches_or_sends(self):
        for business_day, allowed_time in ((False, True), (True, False)):
            with (
                self.subTest(business_day=business_day, allowed_time=allowed_time),
                patch.object(main, "is_thai_bank_business_day", return_value=business_day),
                patch.object(main, "is_expected_scheduled_macro_time", return_value=allowed_time),
                patch.object(main, "fetch_all") as fetch,
                patch.object(main, "send_telegram") as send,
                patch.object(main, "record_macro_delivery") as record,
            ):
                self.assertIsNone(main.job())
                fetch.assert_not_called()
                send.assert_not_called()
                self.assertEqual(record.call_args.args[0], "SKIPPED")

    def test_job_records_telegram_delivery_result(self):
        for delivered, status in ((True, "SENT"), (False, "FAILED")):
            with (
                self.subTest(delivered=delivered),
                patch.object(main, "is_thai_bank_business_day", return_value=True),
                patch.object(main, "is_expected_scheduled_macro_time", return_value=True),
                patch.object(main, "fetch_all", return_value={}),
                patch.object(main, "fetch_market_data_json", return_value=None),
                patch.object(main, "format_message", return_value="summary"),
                patch.object(main, "send_telegram", return_value=delivered) as send,
                patch.object(main, "record_macro_delivery") as record,
            ):
                self.assertEqual(main.job(), delivered)
                send.assert_called_once_with("summary")
                self.assertEqual(record.call_args.args[0], status)

    def test_actions_summary_shows_skip_and_thai_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.md"
            with (
                patch.dict("os.environ", {"GITHUB_STEP_SUMMARY": str(summary)}),
                patch.object(main, "format_th_timestamp", return_value="14/10/2026 03:06"),
            ):
                main.record_macro_delivery("SKIPPED", "Outside configured macro slot; no message sent")
            content = summary.read_text(encoding="utf-8")
            self.assertIn("SKIPPED — 14/10/2026 03:06 (TH)", content)
            self.assertIn("no message sent", content)
