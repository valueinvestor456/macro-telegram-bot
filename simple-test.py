#!/usr/bin/env python3
"""Diagnose Telegram without starting a second command receiver.

Stop the normal poller first. On GitHub, choose mode=check in the polling
workflow so its concurrency group stops the existing receiver for you.
"""
from main import check_telegram

if __name__ == "__main__":
    raise SystemExit(0 if check_telegram() else 1)
