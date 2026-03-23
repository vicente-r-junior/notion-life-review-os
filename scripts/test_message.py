#!/usr/bin/env python3
"""
Test the webhook locally without WhatsApp.
Usage:
  python scripts/test_message.py --text "worked on the app today"
  python scripts/test_message.py --confirm
  python scripts/test_message.py --cancel
  python scripts/test_message.py --query "what do I have today?"
  python scripts/test_message.py --command "*help*"
"""

import argparse
import httpx
import json
import os

WEBHOOK_URL = "http://localhost:8000/webhook"
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "test-secret")
TEST_PHONE = os.getenv("WHATSAPP_NUMBER", "5511999999999")


def make_text_payload(text: str) -> dict:
    return {
        "event": "messages.upsert",
        "instance": "life-review",
        "data": {
            "key": {
                "remoteJid": f"{TEST_PHONE}@s.whatsapp.net",
                "fromMe": False,
                "id": f"TEST_{abs(hash(text)) % 100000}",
            },
            "message": {"conversation": text},
            "messageTimestamp": 1234567890,
            "pushName": "Test User",
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", help="Text message to send")
    parser.add_argument("--confirm", action="store_true", help="Send confirm")
    parser.add_argument("--cancel", action="store_true", help="Send cancel")
    parser.add_argument("--query", help="Query message")
    parser.add_argument("--command", help="Special command (e.g. *help*)")
    args = parser.parse_args()

    text = None
    if args.text:
        text = args.text
    elif args.confirm:
        text = "confirm"
    elif args.cancel:
        text = "cancel"
    elif args.query:
        text = args.query
    elif args.command:
        text = args.command
    else:
        print("Provide --text, --confirm, --cancel, --query, or --command")
        return

    payload = make_text_payload(text)

    response = httpx.post(
        WEBHOOK_URL,
        json=payload,
        headers={"X-Webhook-Secret": WEBHOOK_SECRET},
    )

    print(f"Status: {response.status_code}")
    try:
        print(json.dumps(response.json(), indent=2))
    except Exception:
        print(response.text)


if __name__ == "__main__":
    main()
