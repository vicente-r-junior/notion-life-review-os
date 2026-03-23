import asyncio
import json
import time

from app.config import settings
from app.observability.logger import get_logger, mask_phone
from app.session.redis_store import redis_client
from app.whatsapp import sender

logger = get_logger(__name__)

SILENCE_SECONDS = settings.MESSAGE_AGGREGATION_SILENCE
WINDOW_SECONDS = settings.MESSAGE_AGGREGATION_WINDOW


async def classify_intent(text: str) -> str:
    from openai import AsyncOpenAI
    from pathlib import Path

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    prompt_path = Path("prompts/intent_classifier.md")
    system_prompt = prompt_path.read_text() if prompt_path.exists() else "Classify as: log, query, or add_column"

    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=10,
    )
    return response.choices[0].message.content.strip().lower()


async def process_log(phone: str, text: str):
    from app.agents.extractor import run_extractor
    from app.agents.confirmation import run_confirmation
    from app.schema.schema_manager import get_schema

    try:
        intent = await classify_intent(text)
        logger.info("intent_classified", intent=intent, phone=mask_phone(phone))

        if intent == "query":
            from app.agents.query_agent import run_query_agent
            result = await run_query_agent(text)
            await sender.send_message(phone, result)
            return

        if intent == "add_column":
            await start_add_column_flow(phone)
            return

        # Default: log
        schemas = {
            db: get_schema(db)
            for db in ["daily_logs", "tasks", "projects", "learnings"]
        }
        payload = await run_extractor(text, schemas)

        confirmation_msg = await run_confirmation(payload)

        # Save session
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
        await sender.send_message(phone, confirmation_msg)

    except Exception as e:
        logger.error("process_log_failed", error=str(e), phone=mask_phone(phone))
        await sender.send_message(phone, "Sorry, something went wrong. Please try again.")


async def start_add_column_flow(phone: str):
    session = {
        "state": "waiting_column_db",
        "payload": {},
        "created_at": time.time(),
    }
    redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))

    msg = (
        "Which database do you want to add a column to?\n"
        "1. daily_logs\n"
        "2. tasks\n"
        "3. projects\n"
        "4. learnings\n"
        "5. weekly_reports"
    )
    await sender.send_message(phone, msg)
