#!/usr/bin/env python3
"""Simple bot test - just receive and reply to /usd"""
import requests
import time
import json

TOKEN = "8992498329:AAFO5twl-mf1DZYGwazn3G-6STnhEfFqf1c"
CHAT_ID = "8601040026"

offset = 0

print("🤖 Simple Bot Test Started")
print("Send /usd in Telegram now...")
print()

while True:
    try:
        # Get updates
        url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
        resp = requests.get(url, params={"offset": offset, "timeout": 30}, timeout=35)
        updates = resp.json().get("result", [])

        if updates:
            print(f"✓ Got {len(updates)} message(s)")
            for update in updates:
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat = msg.get("chat", {}).get("id")

                print(f"  Message: {text} from {chat}")

                if text == "/usd":
                    print(f"  → Sending reply...")
                    reply = "✅ Bot is working! Type /usd for USD futures data."
                    requests.post(
                        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                        json={"chat_id": chat, "text": reply},
                        timeout=30
                    )
                    print(f"  → Reply sent!")
        else:
            print(".", end="", flush=True)

        time.sleep(2)
    except Exception as e:
        print(f"Error: {e}")
        time.sleep(5)
