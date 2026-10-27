"""Bangkok bank-day schedules, including night slots owned by the prior day.

Historical policies remain immutable so old delivery ledgers keep their meaning.
Cloudflare implements the same rules against the same JSON configuration.
"""
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEDULE_CONFIG = json.loads(Path(__file__).with_name("macro_schedule.json").read_text(encoding="utf-8"))
TIMEZONE = SCHEDULE_CONFIG["timezone"]
POLICIES = [SCHEDULE_CONFIG, *SCHEDULE_CONFIG["schedule_changes"]]
ALL_SLOTS = frozenset(slot for policy in POLICIES for slot in policy["send_times"])
LEDGER_SLOTS = ALL_SLOTS | set(SCHEDULE_CONFIG["retired_slots"])
HOLIDAYS = frozenset(date.fromisoformat(day) for day in SCHEDULE_CONFIG["bank_holidays"])


def policy_for_day(day):
    key = day.isoformat() if isinstance(day, date) else day
    policy = SCHEDULE_CONFIG
    for change in SCHEDULE_CONFIG["schedule_changes"]:
        if key >= change["effective_from"]:
            policy = change
    return policy


def minutes(slot):
    hour, minute = map(int, slot.split(":"))
    return hour * 60 + minute


def slot_order(day, slot):
    if not slot:
        return -1
    value = minutes(slot)
    return value + (1440 if value < minutes(policy_for_day(day)["day_rollover"]) else 0)


def day_start(day):
    return datetime.combine(day, datetime.min.time(), ZoneInfo(TIMEZONE))


def trading_day(now):
    local = now.astimezone(ZoneInfo(TIMEZONE))
    previous = local.date() - timedelta(days=1)
    rollover = minutes(policy_for_day(previous)["day_rollover"])
    return previous if local.hour * 60 + local.minute < rollover else local.date()


def bank_business_day(day):
    if day.year not in SCHEDULE_CONFIG["calendar_years"]:
        raise ValueError("Thai bank holiday calendar needs updating")
    return day.weekday() < 5 and day not in HOLIDAYS


def due_context(now):
    local = now.astimezone(ZoneInfo(TIMEZONE))
    day = trading_day(local)
    if not bank_business_day(day):
        return day, None, "bank_closed"
    elapsed = (local - day_start(day)).total_seconds() / 60
    policy = policy_for_day(day)
    for window in policy["windows"]:
        start, end = (slot_order(day, window[key]) for key in ("start", "end"))
        if start <= elapsed <= end:
            slots = [slot for slot in policy["send_times"] if start <= slot_order(day, slot) <= elapsed]
            if slots:
                return day, slots[-1], None
    return day, None, "outside_window"


def day_deadline(day):
    last = max(slot_order(day, window["end"]) for window in policy_for_day(day)["windows"])
    return day_start(day) + timedelta(minutes=last)
