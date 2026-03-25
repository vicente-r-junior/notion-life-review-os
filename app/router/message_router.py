import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from app.config import settings
from app.observability.logger import get_logger, mask_phone
from app.session.redis_store import redis_client
from app.session.conversation import get_history, append_history
from app.whatsapp import sender

logger = get_logger(__name__)
client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def process_log(phone: str, text: str):
    try:
        append_history(phone, "user", text)
        history = get_history(phone)
        today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")

        prompt_path = Path("prompts/conversational_agent.md")
        system_prompt = prompt_path.read_text().replace("{today}", today)

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history[:-1])
        messages.append({"role": "user", "content": text})

        response = await client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            temperature=0.4,
        )

        reply = response.choices[0].message.content.strip()

        if "SAVE_PAYLOAD:" in reply:
            parts = reply.split("SAVE_PAYLOAD:", 1)
            user_message = parts[0].strip()
            payload_str = parts[1].strip()

            try:
                payload = json.loads(payload_str)
            except json.JSONDecodeError:
                match = re.search(r'\{.*\}', payload_str, re.DOTALL)
                if match:
                    payload = json.loads(match.group())
                else:
                    raise

            session = {
                "state": "waiting_confirmation",
                "payload": payload,
                "created_at": time.time(),
            }
            redis_client.setex(
                f"session:{phone}",
                settings.SESSION_TTL,
                json.dumps(session),
            )
            append_history(phone, "assistant", user_message)
            await sender.send_message(phone, user_message)
        else:
            append_history(phone, "assistant", reply)
            await sender.send_message(phone, reply)

    except Exception as e:
        logger.error("process_log_failed", error=str(e), phone=mask_phone(phone))
        await sender.send_message(phone, "Something went wrong, sorry! Try again 😅")


async def start_add_column_flow(phone: str):
    session = {
        "state": "waiting_column_db",
        "payload": {},
        "created_at": time.time(),
    }
    redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
    msg = (
        "Which database?\n"
        "1. daily_logs\n2. tasks\n3. projects\n"
        "4. learnings\n5. weekly_reports"
    )
    await sender.send_message(phone, msg)
