"""Durable, send-only macro scheduler; never calls Telegram getUpdates.

GitHub workers use a shared state branch with compare-and-swap writes. A single
standalone host can use SQLite instead. An uncertain Telegram response is NOT
retried automatically: Telegram sendMessage has no idempotency key.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

from main import SCHEDULE_CONFIG, SEND_TIMES, TIMEZONE, is_thai_bank_business_day, record_macro_delivery

ROOT = Path(__file__).resolve().parent
PREPARE_LEASE_SECONDS = 300
MAX_ATTEMPTS = 3
# The previous 12-slot schedule included 16:15. Preserve its delivery history
# without scheduling new sends at that retired time.
LEDGER_SLOTS = frozenset(SEND_TIMES) | set(SCHEDULE_CONFIG["retired_slots"])


class StateConflict(Exception):
    pass


def validate_state(state):
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("days"), dict):
        raise ValueError("Invalid delivery ledger; refusing to send")
    activated = state.get("activated_on", "")
    if date.fromisoformat(activated).isoformat() != activated:
        raise ValueError("Invalid ledger activation date")
    for day_key, day in state["days"].items():
        if date.fromisoformat(day_key).isoformat() != day_key or not isinstance(day, dict):
            raise ValueError("Invalid ledger day")
        if day.get("covered_through") not in LEDGER_SLOTS | {""} or not isinstance(day.get("attempts"), dict):
            raise ValueError("Invalid ledger coverage")
        for slot, entry in day["attempts"].items():
            if slot not in LEDGER_SLOTS or not isinstance(entry, dict):
                raise ValueError("Invalid ledger slot")
            if entry.get("status") not in ("preparing", "sending", "sent", "retry", "failed", "uncertain"):
                raise ValueError("Invalid delivery status")
            required_time = {"preparing": "lease_until", "sending": "sending_at"}.get(entry["status"])
            if required_time and not isinstance(entry.get(required_time), (int, float)):
                raise ValueError("Invalid delivery lease")
    return state


def empty_state():
    return {"version": 1, "activated_on": datetime.now(ZoneInfo(TIMEZONE)).date().isoformat(), "days": {}}


class SQLiteStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, revision INTEGER, payload TEXT)")
            db.execute("INSERT OR IGNORE INTO ledger VALUES (1, 0, ?)", (json.dumps(empty_state()),))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def load(self):
        with self.connect() as db:
            revision, payload = db.execute("SELECT revision, payload FROM ledger WHERE id=1").fetchone()
        return validate_state(json.loads(payload)), revision

    def save(self, state, revision):
        validate_state(state)
        with self.connect() as db:
            changed = db.execute(
                "UPDATE ledger SET revision=revision+1, payload=? WHERE id=1 AND revision=?",
                (json.dumps(state), revision),
            ).rowcount
            if not changed:
                raise StateConflict("Another worker updated the delivery ledger")
        return revision + 1


class GitHubStore:
    """The state branch must be provisioned explicitly; missing state fails closed."""
    def __init__(self, repository, token, branch="macro-delivery-state"):
        if not repository or not token:
            raise ValueError("GitHub state requires repository and token")
        self.url = f"https://api.github.com/repos/{repository}/contents/macro_delivery.json"
        self.branch = branch
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def load(self):
        response = self.session.get(self.url, params={"ref": self.branch}, timeout=20)
        if response.status_code != 200:
            raise RuntimeError(f"Cannot read delivery ledger (HTTP {response.status_code}); refusing to send")
        data = response.json()
        return validate_state(json.loads(base64.b64decode(data["content"]))), data["sha"]

    def save(self, state, revision):
        validate_state(state)
        response = self.session.put(self.url, json={
            "message": "Update macro delivery ledger",
            "branch": self.branch,
            "sha": revision,
            "content": base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode(),
        }, timeout=20)
        if response.status_code in (409, 422):
            raise StateConflict("Another worker updated the delivery ledger")
        if response.status_code not in (200, 201):
            raise RuntimeError(f"Cannot persist delivery ledger (HTTP {response.status_code})")
        return response.json()["content"]["sha"]


def thai_now():
    return datetime.now(ZoneInfo(TIMEZONE))


def due_slot(now):
    now = now.astimezone(ZoneInfo(TIMEZONE))
    if not is_thai_bank_business_day(now):
        return None
    hh, mm = map(int, SEND_TIMES[-1].split(":"))
    cutoff = now.replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(minutes=30)
    if now > cutoff:
        return None
    # Catch up to the most recent due slot, not a burst of old snapshots.
    slots = [slot for slot in SEND_TIMES if slot <= now.strftime("%H:%M")]
    return slots[-1] if slots else None


def audit_completed_day(state, now):
    """Report a missed final round when the next worker eventually wakes up."""
    local = now.astimezone(ZoneInfo(TIMEZONE))
    last_hour, last_minute = map(int, SEND_TIMES[-1].split(":"))
    deadline = local.replace(hour=last_hour, minute=last_minute, second=0, microsecond=0) + timedelta(minutes=30)
    candidate = local if local > deadline else local - timedelta(days=1)
    for _ in range(14):
        if is_thai_bank_business_day(candidate):
            break
        candidate -= timedelta(days=1)
    day_key = candidate.date().isoformat()
    if day_key < state.get("activated_on", local.date().isoformat()):
        return "idle"
    if state["days"].get(day_key, {}).get("covered_through", "") < SEND_TIMES[-1]:
        print(f"[scheduler] missed final round: {day_key} {SEND_TIMES[-1]} (TH)", flush=True)
        return "missed"
    return "idle"


def build_summary():
    # A hard process deadline bounds slow upstream data providers.
    code = (
        "import json; from main import fetch_all, fetch_market_data_json, format_message; "
        "print(json.dumps({'text': format_message(fetch_all(), fetch_market_data_json())}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
        encoding="utf-8", timeout=150, check=True,
    )
    text = json.loads(result.stdout.splitlines()[-1])["text"]
    if not isinstance(text, str) or not text or len(text) > 4096:
        raise ValueError("Invalid Telegram message size")
    return text


def deliver(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or "PASTE_YOUR" in token or not chat:
        return {"status": "failed", "reason": "Telegram credentials are not configured"}
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text}, timeout=(10, 30),
        )
    except requests.RequestException:
        return {"status": "uncertain", "reason": "Network failure; Telegram may have accepted the message"}
    try:
        payload = response.json()
    except ValueError:
        return {"status": "uncertain", "reason": "Telegram response was not valid JSON"}
    if not isinstance(payload, dict):
        return {"status": "uncertain", "reason": "Unexpected Telegram response"}
    if payload.get("ok") is True and isinstance(payload.get("result"), dict) and payload["result"].get("message_id"):
        return {"status": "sent", "message_id": payload["result"]["message_id"]}
    if payload.get("ok") is False:
        code = payload.get("error_code", response.status_code)
        if code == 429 or (isinstance(code, int) and code >= 500):
            return {"status": "retry", "retry_after": max(60, int(payload.get("parameters", {}).get("retry_after", 60))), "reason": f"Telegram rejected request: {code}"}
        return {"status": "failed", "reason": f"Telegram rejected request: {code}"}
    return {"status": "uncertain", "reason": "Telegram did not confirm acceptance or rejection"}


def tick(store, *, now_fn=thai_now, builder=build_summary, sender=deliver, preview=False):
    now = now_fn().astimezone(ZoneInfo(TIMEZONE))
    slot = due_slot(now)
    state, revision = store.load()
    if not slot:
        if preview:
            print(f"[scheduler] no due slot {now.isoformat()}; preview only, ledger readable")
            return "preview_idle"
        return audit_completed_day(state, now)
    day_key = now.date().isoformat()
    day = state["days"].setdefault(day_key, {"covered_through": "", "attempts": {}})
    if day["covered_through"] >= slot:
        return "already_sent"
    if preview:
        print(f"[scheduler] latest due slot {day_key} {slot} (TH); preview only, no send or state change")
        return "preview"
    # One active preparation per day, including when a newer slot becomes due.
    for entry in day["attempts"].values():
        if entry["status"] == "preparing" and entry["lease_until"] > now.timestamp():
            return "busy"
        if entry["status"] == "sending":
            if now.timestamp() - entry["sending_at"] < 120:
                return "busy"
            # A killed worker may already have sent. Require explicit review.
            entry["status"] = "uncertain"
            entry["reason"] = "Worker stopped before recording Telegram response"
            store.save(state, revision)
            return "uncertain"
    previous = day["attempts"].get(slot, {})
    if previous.get("status") in ("sent", "uncertain", "failed"):
        return previous["status"] if previous["status"] != "sent" else "already_sent"
    if previous.get("retry_at", 0) > now.timestamp():
        return "backoff"
    attempt = previous.get("attempt", 0) + 1
    if attempt > MAX_ATTEMPTS:
        previous.update(status="failed", reason="Maximum preparation attempts exceeded")
        store.save(state, revision)
        return "failed"
    owner = uuid4().hex
    entry = {"status": "preparing", "owner": owner, "attempt": attempt,
             "lease_until": now.timestamp() + PREPARE_LEASE_SECONDS}
    day["attempts"][slot] = entry
    # Bounded live ledger; Git retains the complete audit history remotely.
    cutoff = (now.date() - timedelta(days=35)).isoformat()
    state["days"] = {key: value for key, value in state["days"].items() if key >= cutoff}
    revision = store.save(state, revision)
    try:
        text = builder()
    except Exception as error:
        entry.update(status="retry" if attempt < MAX_ATTEMPTS else "failed",
                     retry_at=now_fn().timestamp() + 60 * attempt,
                     reason=f"Data preparation failed: {type(error).__name__}")
        store.save(state, revision)
        return entry["status"]
    current = now_fn().astimezone(ZoneInfo(TIMEZONE))
    if current.date() != now.date() or not due_slot(current) or current.timestamp() >= entry["lease_until"]:
        entry.update(status="retry", retry_at=current.timestamp(), reason="Preparation exceeded send window")
        store.save(state, revision)
        return "idle"
    entry.update(status="sending", sending_at=current.timestamp())
    # Persist BEFORE sending. If this CAS fails or times out, do not send.
    revision = store.save(state, revision)
    try:
        result = sender(text)
    except Exception:
        result = {"status": "uncertain", "reason": "Unexpected delivery error"}
    entry.update(result, updated_at=now_fn().isoformat())
    if result["status"] == "sent":
        entry["covers_after"] = day["covered_through"]
        day["covered_through"] = slot
    elif result["status"] == "retry":
        entry["retry_at"] = now_fn().timestamp() + max(60 * attempt, result.get("retry_after", 60))
        if attempt >= MAX_ATTEMPTS:
            entry["status"] = "failed"
    # If saving fails, leave durable 'sending' to prevent duplicate delivery.
    store.save(state, revision)
    return entry["status"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="run a send-only scheduler continuously")
    parser.add_argument("--preview", action="store_true", help="inspect due work without sending or writing state")
    parser.add_argument("--store", choices=("github", "sqlite"), default=os.environ.get("MACRO_STATE_STORE", "github"))
    parser.add_argument("--state-path", default=str(ROOT / ".state" / "macro-delivery.sqlite3"))
    args = parser.parse_args()
    store = (GitHubStore(os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
             if args.store == "github" else SQLiteStore(args.state_path))
    while True:
        try:
            status = tick(store, preview=args.preview)
        except StateConflict:
            status = "busy"
        except Exception as error:
            # Do not expose URLs containing credentials or upstream bodies.
            record_macro_delivery("FAILED", f"Scheduler error: {type(error).__name__}")
            status = "failed"
        if status in ("sent", "retry", "failed", "uncertain", "missed"):
            record_macro_delivery(status.upper(), "Durable macro scheduler; inspect delivery ledger for details")
        else:
            print(f"[scheduler] {status} {thai_now().isoformat()}", flush=True)
        if not args.loop:
            return 1 if status in ("failed", "uncertain", "retry", "missed") else 0
        time.sleep(30)


if __name__ == "__main__":
    raise SystemExit(main())
