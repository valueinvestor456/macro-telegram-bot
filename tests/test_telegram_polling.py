import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

import requests

import main


class TelegramPollingTests(unittest.TestCase):
    def test_usd_aliases_call_the_same_handler(self):
        for command in ("/u", "/usd"):
            with (
                self.subTest(command=command),
                patch.object(main, "format_usd_futures", return_value="USD result") as format_reply,
                patch.object(main, "send_telegram_reply") as send,
            ):
                main.handle_command("123", command)
                format_reply.assert_called_once_with()
                send.assert_called_once_with("123", "USD result")

    def test_telegram_check_detects_conflict_without_acknowledging_updates(self):
        for conflict in (False, True):
            identity = Mock()
            identity.json.return_value = {"ok": True, "result": {"username": "usd_bot", "first_name": "Usd"}}
            webhook = Mock()
            webhook.json.return_value = {"ok": True, "result": {"url": ""}}
            poll = Mock(status_code=409 if conflict else 200)
            poll.json.return_value = {"ok": True, "result": [{"update_id": 101, "message": {"text": "private-message"}}]}
            with (
                self.subTest(conflict=conflict),
                patch.object(main, "TELEGRAM_BOT_TOKEN", "private-token"),
                patch.object(main.requests, "get", side_effect=[identity, webhook, poll, poll, poll]) as get,
                patch.object(main.requests, "post") as post,
                patch.object(main, "save_last_update_offset") as save,
                patch.object(main.time, "sleep"),
                redirect_stdout(io.StringIO()) as output,
            ):
                self.assertEqual(main.check_telegram(), not conflict)
                for call in get.call_args_list[2:]:
                    self.assertNotIn("offset", call.kwargs["params"])
                post.assert_not_called()
                save.assert_not_called()
                self.assertIn("CONFLICT" if conflict else "result: OK", output.getvalue())
                self.assertNotIn("private-token", output.getvalue())
                self.assertNotIn("private-message", output.getvalue())

    def test_telegram_check_does_not_poll_when_webhook_is_active(self):
        identity = Mock()
        identity.json.return_value = {"ok": True, "result": {"username": "usd_bot"}}
        webhook = Mock()
        webhook.json.return_value = {"ok": True, "result": {"url": "https://example.com/private"}}
        with (
            patch.object(main, "TELEGRAM_BOT_TOKEN", "private-token"),
            patch.object(main.requests, "get", side_effect=[identity, webhook]) as get,
            redirect_stdout(io.StringIO()) as output,
        ):
            self.assertFalse(main.check_telegram())
        self.assertEqual(get.call_count, 2)
        self.assertNotIn("https://example.com/private", output.getvalue())

    def test_poll_only_does_not_run_scheduled_sends(self):
        with (
            patch.object(main.sys, "argv", ["main.py", "--poll-only"]),
            patch.object(main.schedule, "every") as every,
            patch.object(main.schedule, "run_pending"),
            patch.object(main, "poll_commands", side_effect=KeyboardInterrupt),
            redirect_stdout(io.StringIO()),
        ):
            with self.assertRaises(KeyboardInterrupt):
                main.main()
            every.assert_not_called()

    def test_usd_reply_includes_current_usd_thb_rate(self):
        with (
            patch.object(
                main,
                "fetch_all",
                return_value={"USDTHB": {"last": 33.7, "pct": 0.03, "dp": 3, "unit": ""}},
            ),
            patch.object(main, "fetch_market_data_json", return_value=None),
            patch.object(main, "compute_thb_score", return_value=None),
            patch.object(main, "compute_cip_fair_fixed", return_value=None),
            patch.object(main, "fetch_tfex_usd_futures_dated", return_value=None),
        ):
            reply = main.format_usd_futures()

        self.assertIn("💱 USD/THB: 33.700", reply)

    def test_repeated_poll_conflicts_notify_configured_chat_once(self):
        response = Mock(status_code=409)
        response.json.return_value = {
            "description": "Conflict: terminated by other getUpdates request"
        }
        error = requests.HTTPError("409 conflict", response=response)
        with (
            patch.object(main, "TELEGRAM_BOT_TOKEN", "test-token"),
            patch.object(main, "TELEGRAM_CHAT_ID", "123"),
            patch.object(main, "_POLL_CONFLICT_COUNT", 0),
            patch.object(main, "_POLL_CONFLICT_NOTIFIED", False),
            patch.object(main.requests, "get") as get,
            patch.object(main, "send_telegram_reply") as send_reply,
            redirect_stderr(io.StringIO()) as stderr,
        ):
            get.return_value.raise_for_status.side_effect = error

            for _ in range(4):
                self.assertEqual(main.get_telegram_updates(), [])

            send_reply.assert_called_once()
            self.assertEqual(send_reply.call_args.args[0], "123")
            self.assertIn("เหลือ bot poller เพียงตัวเดียว", send_reply.call_args.args[1])
            self.assertIn("Conflict: terminated by other getUpdates request", stderr.getvalue())
            self.assertNotIn("test-token", stderr.getvalue())

    def test_successful_poll_resets_conflict_state(self):
        response = Mock()
        response.json.return_value = {"result": [{"update_id": 1}]}
        with (
            patch.object(main, "TELEGRAM_BOT_TOKEN", "test-token"),
            patch.object(main, "_POLL_CONFLICT_COUNT", 3),
            patch.object(main, "_POLL_CONFLICT_NOTIFIED", True),
            patch.object(main.requests, "get", return_value=response),
        ):
            self.assertEqual(main.get_telegram_updates(), [{"update_id": 1}])

        self.assertEqual(main._POLL_CONFLICT_COUNT, 0)
        self.assertFalse(main._POLL_CONFLICT_NOTIFIED)


if __name__ == "__main__":
    unittest.main()
