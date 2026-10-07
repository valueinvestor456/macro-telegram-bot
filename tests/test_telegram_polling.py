import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import Mock, patch

import requests

import main


class TelegramPollingTests(unittest.TestCase):
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
