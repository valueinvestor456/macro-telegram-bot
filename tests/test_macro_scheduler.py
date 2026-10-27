import copy
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

import macro_scheduler as scheduler


class Store:
    def __init__(self):
        self.state = scheduler.empty_state()
        self.revision = 0

    def load(self):
        return copy.deepcopy(self.state), self.revision

    def save(self, state, revision):
        if revision != self.revision:
            raise scheduler.StateConflict()
        self.state = copy.deepcopy(state)
        self.revision += 1
        return self.revision


class MacroSchedulerTests(TestCase):
    def setUp(self):
        self.store = Store()
        self.now = datetime(2026, 10, 14, 11, 20, tzinfo=scheduler.ZoneInfo(scheduler.TIMEZONE))
        self.builder = Mock(return_value="summary")
        self.sender = Mock(return_value={"status": "sent", "message_id": 123})

    def tick(self, **kwargs):
        return scheduler.tick(self.store, now_fn=lambda: self.now, builder=self.builder, sender=self.sender, **kwargs)

    def test_late_wakeup_coalesces_missed_rounds_and_restart_does_not_duplicate(self):
        self.assertEqual(self.tick(), "sent")
        self.assertEqual(self.tick(), "already_sent")
        self.sender.assert_called_once_with("summary")
        day = self.store.state["days"]["2026-10-14"]
        self.assertEqual(day["covered_through"], "11:00")
        self.assertEqual(day["attempts"]["11:00"]["message_id"], 123)
        self.now = self.now.replace(hour=12, minute=0)
        self.assertEqual(self.tick(), "sent")
        self.assertEqual(self.sender.call_count, 2)

    def test_timezone_holidays_and_no_overnight_catchup(self):
        for when in (self.now.replace(hour=9, minute=44), self.now.replace(hour=22),
                     self.now.replace(day=13), self.now.replace(day=17)):
            with self.subTest(when=when):
                self.assertIsNone(scheduler.due_slot(when))
        self.assertEqual(scheduler.due_slot(self.now.astimezone(scheduler.ZoneInfo("UTC"))), "11:00")

    def test_half_hour_schedule_sends_each_round_once_within_business_hours(self):
        opening = self.now.replace(hour=9, minute=45)
        half_hours = [
            self.now.replace(hour=10, minute=0) + timedelta(minutes=30 * step)
            for step in range(23)
        ]
        for when in [opening, *half_hours]:
            with self.subTest(when=when):
                self.now = when
                self.assertEqual(self.tick(), "sent")
                self.assertEqual(self.tick(), "already_sent")
        self.assertEqual(self.sender.call_count, 24)
        self.assertEqual(self.store.state["days"]["2026-10-14"]["covered_through"], "21:00")
        self.now = self.now.replace(hour=21, minute=31)
        self.assertIsNone(scheduler.due_slot(self.now))

    def test_retired_slot_history_remains_readable_and_next_half_hour_can_send(self):
        self.store.state["days"]["2026-10-14"] = {
            "covered_through": "16:15",
            "attempts": {"16:15": {"status": "sent", "message_id": 122}},
        }
        scheduler.validate_state(self.store.state)
        self.now = self.now.replace(hour=16, minute=20)
        self.assertEqual(self.tick(), "already_sent")
        self.sender.assert_not_called()
        self.now = self.now.replace(minute=30)
        self.assertEqual(self.tick(), "sent")
        scheduler.validate_state(self.store.state)
        day = self.store.state["days"]["2026-10-14"]
        self.assertEqual(day["attempts"]["16:15"]["message_id"], 122)
        self.assertEqual(day["covered_through"], "16:30")
        self.sender.assert_called_once()

    def test_failed_preparation_retries_without_sending_and_is_bounded(self):
        self.builder.side_effect = TimeoutError()
        self.assertEqual(self.tick(), "retry")
        self.assertEqual(self.tick(), "backoff")
        self.now += timedelta(minutes=2)
        self.assertEqual(self.tick(), "retry")
        self.now += timedelta(minutes=3)
        self.assertEqual(self.tick(), "failed")
        self.sender.assert_not_called()

    def test_explicit_rejection_can_retry_but_ambiguous_delivery_cannot(self):
        self.sender.return_value = {"status": "retry", "retry_after": 120}
        self.assertEqual(self.tick(), "retry")
        self.assertEqual(self.tick(), "backoff")
        self.now += timedelta(minutes=3)
        self.sender.return_value = {"status": "uncertain"}
        self.assertEqual(self.tick(), "uncertain")
        self.now += timedelta(minutes=3)
        self.assertEqual(self.tick(), "uncertain")
        self.assertEqual(self.sender.call_count, 2)

    def test_persist_before_send_and_crash_after_send_does_not_duplicate(self):
        original_save = self.store.save
        def save(state, revision):
            if state["days"]["2026-10-14"]["attempts"]["11:00"]["status"] == "sent":
                raise OSError("state store unavailable")
            return original_save(state, revision)
        self.store.save = save
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.store.state["days"]["2026-10-14"]["attempts"]["11:00"]["status"], "sending")
        self.now += timedelta(minutes=3)
        self.assertEqual(self.tick(), "uncertain")
        self.sender.assert_called_once()

    def test_prepare_lease_recovers_but_competing_worker_cannot_send(self):
        def competing_build():
            self.assertEqual(self.tick(), "busy")
            self.store.revision += 1
            return "summary"
        self.builder.side_effect = competing_build
        with self.assertRaises(scheduler.StateConflict):
            self.tick()
        self.sender.assert_not_called()
        self.now += timedelta(minutes=6)
        self.builder.side_effect = None
        self.assertEqual(self.tick(), "sent")

    def test_preview_does_not_write_even_with_abandoned_sending_record(self):
        self.store.state["days"]["2026-10-14"] = {
            "covered_through": "", "attempts": {"11:00": {"status": "sending", "sending_at": 0}}
        }
        before = copy.deepcopy(self.store.state)
        self.assertEqual(self.tick(preview=True), "preview")
        self.assertEqual(self.store.state, before)
        self.assertEqual(self.store.revision, 0)
        self.builder.assert_not_called()
        self.sender.assert_not_called()

    def test_sqlite_survives_restart_and_rejects_stale_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.sqlite3"
            self.store = scheduler.SQLiteStore(path)
            stale_state, stale_revision = self.store.load()
            self.assertEqual(self.tick(), "sent")
            self.store = scheduler.SQLiteStore(path)
            self.assertEqual(self.tick(), "already_sent")
            with self.assertRaises(scheduler.StateConflict):
                self.store.save(stale_state, stale_revision)

    def test_network_timeout_is_uncertain_and_token_not_in_result(self):
        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "private-token", "TELEGRAM_CHAT_ID": "123"}),
            patch.object(scheduler.requests, "post", side_effect=requests.Timeout("private-token")),
        ):
            result = scheduler.deliver("summary")
        self.assertEqual(result["status"], "uncertain")
        self.assertNotIn("private-token", str(result))

    def test_state_unavailable_fails_before_any_send(self):
        self.store.load = Mock(side_effect=OSError())
        with self.assertRaises(OSError):
            self.tick()
        self.builder.assert_not_called()
        self.sender.assert_not_called()

    def test_audit_reports_missed_day_after_activation_without_sending_at_night(self):
        self.store.state["activated_on"] = "2026-10-14"
        self.now = self.now.replace(hour=22)
        self.assertEqual(self.tick(), "missed")
        self.sender.assert_not_called()
        self.store.state["days"]["2026-10-14"] = {"covered_through": "21:00", "attempts": {}}
        self.assertEqual(self.tick(), "idle")

    def test_audit_does_not_report_dates_before_installation(self):
        self.store.state["activated_on"] = "2026-10-14"
        self.now = self.now.replace(hour=8)
        self.assertEqual(self.tick(), "idle")

    def test_preview_outside_window_validates_ledger_without_auditing_old_misses(self):
        self.store.state["activated_on"] = "2026-10-12"
        self.now = self.now.replace(hour=8)
        self.assertEqual(self.tick(preview=True), "preview_idle")
        self.assertEqual(self.store.revision, 0)
        self.builder.assert_not_called()
        self.sender.assert_not_called()

    def test_cutoff_during_data_preparation_does_not_send(self):
        self.now = self.now.replace(hour=21, minute=29)
        def build_late():
            self.now += timedelta(minutes=2)
            return "summary"
        self.builder.side_effect = build_late
        self.assertEqual(self.tick(), "idle")
        self.sender.assert_not_called()

    def test_telegram_confirmed_rejection_and_success(self):
        response = Mock(status_code=429)
        response.json.return_value = {"ok": False, "error_code": 429, "parameters": {"retry_after": 180}}
        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "123"}),
            patch.object(scheduler.requests, "post", return_value=response),
        ):
            self.assertEqual(scheduler.deliver("summary")["retry_after"], 180)
            response.json.return_value = {"ok": True, "result": {"message_id": 456}}
            self.assertEqual(scheduler.deliver("summary"), {"status": "sent", "message_id": 456})

    def test_github_ledger_uses_revision_and_missing_ledger_fails_closed(self):
        import base64
        import json
        store = scheduler.GitHubStore("owner/repo", "test-token")
        response = Mock(status_code=200)
        response.json.return_value = {"content": base64.b64encode(json.dumps(scheduler.empty_state()).encode()).decode(), "sha": "old-sha"}
        store.session = Mock()
        store.session.get.return_value = response
        state, revision = store.load()
        written = Mock(status_code=200)
        written.json.return_value = {"content": {"sha": "new-sha"}}
        store.session.put.return_value = written
        self.assertEqual(store.save(state, revision), "new-sha")
        self.assertEqual(store.session.put.call_args.kwargs["json"]["sha"], "old-sha")
        written.status_code = 409
        with self.assertRaises(scheduler.StateConflict):
            store.save(state, revision)
        response.status_code = 404
        with self.assertRaises(RuntimeError):
            store.load()

    def test_corrupt_coverage_is_not_silently_treated_as_delivered(self):
        state = scheduler.empty_state()
        state["days"]["2026-10-14"] = {"covered_through": "99:99", "attempts": {}}
        with self.assertRaises(ValueError):
            scheduler.validate_state(state)


class OvernightSchedulerTests(TestCase):
    setUp = MacroSchedulerTests.setUp
    tick = MacroSchedulerTests.tick
    def start_new_day(self, day=27):
        self.now = datetime(2026, 10, day, 8, tzinfo=scheduler.ZoneInfo(scheduler.TIMEZONE))
        self.store.state["activated_on"] = self.now.date().isoformat()

    def test_35_new_rounds_are_acknowledged_once_in_one_trading_day(self):
        self.start_new_day()
        start = self.now
        slots = scheduler.policy_for_day(start.date())["send_times"]
        self.assertEqual(len(slots), 35)
        for slot in slots:
            self.now = start.replace(hour=0) + timedelta(minutes=scheduler.slot_order(start.date(), slot))
            self.assertEqual(self.tick(), "sent", slot)
            self.assertEqual(self.tick(), "already_sent", slot)
        self.assertEqual(self.sender.call_count, 35)
        self.assertEqual(list(self.store.state["days"]), ["2026-10-27"])
        self.assertEqual(self.store.state["days"]["2026-10-27"]["covered_through"], "03:00")
        scheduler.validate_state(self.store.state)
        self.now += timedelta(minutes=30, seconds=1)
        self.assertEqual(self.tick(), "idle")
        self.assertEqual(self.sender.call_count, 35)

    def test_break_and_night_cutoff_reject_late_preparation(self):
        for start in (datetime(2026, 10, 27, 16, 54), datetime(2026, 10, 28, 3, 29)):
            self.store = Store()
            self.now = start.replace(tzinfo=scheduler.ZoneInfo(scheduler.TIMEZONE))
            def build_late():
                self.now += timedelta(minutes=2)
                return "summary"
            self.builder.side_effect = build_late
            self.assertEqual(self.tick(), "idle")
        self.sender.assert_not_called()

    def test_midnight_preparation_keeps_original_day_and_next_slot_is_not_suppressed(self):
        self.start_new_day()
        self.now = self.now.replace(hour=23, minute=59)
        def across_midnight():
            self.now += timedelta(minutes=2)
            return "summary"
        self.builder.side_effect = across_midnight
        self.assertEqual(self.tick(), "sent")
        self.builder.side_effect = None
        self.assertEqual(self.tick(), "sent")
        self.assertEqual(self.tick(), "already_sent")
        day = self.store.state["days"]["2026-10-27"]
        self.assertEqual(day["covered_through"], "00:00")
        self.assertEqual(set(day["attempts"]), {"23:30", "00:00"})

    def test_audit_waits_until_trading_day_really_finishes(self):
        self.start_new_day()
        self.now = self.now.replace(hour=17)
        self.assertEqual(self.tick(), "idle")
        self.now = self.now.replace(day=28, hour=3, minute=31)
        self.assertEqual(self.tick(), "missed")
        self.sender.assert_not_called()
        self.store.state["days"]["2026-10-27"] = {"covered_through": "03:00", "attempts": {}}
        self.assertEqual(self.tick(), "idle")

    def test_old_final_round_is_still_complete_after_transition(self):
        self.start_new_day()
        self.now = self.now.replace(hour=7)
        self.store.state["activated_on"] = "2026-10-26"
        self.store.state["days"]["2026-10-26"] = {"covered_through": "21:00", "attempts": {}}
        self.assertEqual(self.tick(), "idle")
        self.sender.assert_not_called()

    def test_fresh_overnight_ledger_audits_its_actual_first_trading_day(self):
        self.start_new_day()
        self.now = self.now.replace(day=28, hour=2)
        with patch.object(scheduler, "thai_now", return_value=self.now):
            self.store = Store()
        self.assertEqual(self.store.state["activated_on"], "2026-10-27")
        self.now = self.now.replace(hour=3, minute=31)
        self.assertEqual(self.tick(), "missed")
        self.sender.assert_not_called()

    def test_existing_0945_acknowledgement_prevents_morning_backfill(self):
        self.start_new_day()
        self.now = self.now.replace(hour=9, minute=59)
        self.store.state["days"]["2026-10-27"] = {
            "covered_through": "09:45", "attempts": {"09:45": {"status": "sent", "message_id": 1274}},
        }
        self.assertEqual(self.tick(), "already_sent")
        self.now = self.now.replace(hour=10, minute=0)
        self.assertEqual(self.tick(), "sent")
        self.sender.assert_called_once()
