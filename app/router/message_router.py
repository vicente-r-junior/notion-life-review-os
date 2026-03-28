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
        # If an active session exists, route directly to session handler
        session_raw = redis_client.get(f"session:{phone}")
        if session_raw:
            from app.whatsapp.handler import handle_session_reply
            session = json.loads(session_raw)
            await handle_session_reply(phone, text, session)
            return

        # Classify intent: query (retrieve from Notion) vs log (capture/update)
        from app.agents.intent_classifier import classify_intent
        intent = await classify_intent(text)

        if intent == "query":
            from app.agents.query_agent import run_query_agent
            logger.info("routing_to_query_agent", phone=mask_phone(phone))
            result = await run_query_agent(text)
            append_history(phone, "user", text)
            append_history(phone, "assistant", result)
            await sender.send_message(phone, result)
            return

        if intent == "add_column":
            logger.info("routing_to_add_column", phone=mask_phone(phone))
            await _handle_add_column_intent(phone, text)
            return

        append_history(phone, "user", text)
        history = get_history(phone)
        today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")

        from app.session.prompt_builder import get_system_prompt
        system_prompt = (
            get_system_prompt()
            .replace("{today}", today)
            .replace("{today[:4]}", today[:4])
        )

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
            payload_raw = parts[1].strip()

            # Extract exactly the JSON object using brace counting
            brace_count = 0
            json_end = 0
            for i, ch in enumerate(payload_raw):
                if ch == '{':
                    brace_count += 1
                elif ch == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        json_end = i + 1
                        break

            payload_str = payload_raw[:json_end] if json_end > 0 else payload_raw

            try:
                payload = json.loads(payload_str)
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
                logger.info("session_created", phone=mask_phone(phone), payload_keys=list(payload.keys()))
                append_history(phone, "assistant", user_message)
                await sender.send_message(phone, user_message)
            except Exception as e:
                logger.error("save_payload_parse_failed", error=str(e), raw=payload_str[:100])
                # Still send the message even if payload parsing failed
                msg_to_send = user_message if user_message else reply
                append_history(phone, "assistant", msg_to_send)
                await sender.send_message(phone, msg_to_send)
        else:
            append_history(phone, "assistant", reply)
            await sender.send_message(phone, reply)

    except Exception as e:
        logger.error("process_log_failed", error=str(e), phone=mask_phone(phone))
        await sender.send_message(phone, "Something went wrong, sorry! Try again 😅")


_DB_ALIASES = {
    "tasks": "tasks", "task": "tasks",
    "projects": "projects", "project": "projects",
    "daily logs": "daily_logs", "daily_log": "daily_logs", "daily log": "daily_logs",
    "learnings": "learnings", "learning": "learnings",
    "weekly reports": "weekly_reports", "weekly report": "weekly_reports",
}

_TYPE_ALIASES = {
    "text": "1", "rich text": "1", "string": "1",
    "number": "2", "numeric": "2",
    "select": "3", "dropdown": "3",
    "multi select": "4", "multi-select": "4", "multiselect": "4", "tags": "4",
    "date": "5",
    "checkbox": "6", "bool": "6", "boolean": "6",
    "url": "7", "link": "7",
    "email": "8",
}

_EXTRACT_SYSTEM = """Extract add-column details from the user message. Reply with JSON only.
Fields: db (one of: tasks, projects, daily_logs, learnings, weekly_reports or null),
        column_name (string or null),
        column_type (one of: text, number, select, multi_select, date, checkbox, url, email, or null),
        required (true/false/null).
Example: {"db": "tasks", "column_name": "Owner", "column_type": "text", "required": true}
If a field is not mentioned, use null."""


async def _handle_add_column_intent(phone: str, text: str):
    # Extract what the user already told us
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _EXTRACT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=80,
            response_format={"type": "json_object"},
        )
        info = json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error("add_column_extract_failed", error=str(e))
        info = {}

    db = info.get("db")
    column_name = info.get("column_name")
    column_type_str = (info.get("column_type") or "").lower().replace(" ", "_")
    required = info.get("required")

    # Normalize db name
    if db:
        db = _DB_ALIASES.get(db.lower().replace("_", " "), db)

    # Normalize type to numeric key used by session flow
    type_num = None
    if column_type_str:
        type_num = _TYPE_ALIASES.get(column_type_str.replace("_", " "))

    db_names = ["daily_logs", "tasks", "projects", "learnings", "weekly_reports"]
    from app.whatsapp.handler import COLUMN_TYPE_MAP

    # If we have everything, go straight to confirmation
    if db and column_name and type_num:
        column_type = COLUMN_TYPE_MAP[type_num]
        payload = {
            "chosen_db": db,
            "column_name": column_name,
            "column_type": column_type,
            "column_type_num": type_num,
            "required": bool(required),
        }
        session = {"state": "waiting_column_required", "payload": payload, "created_at": time.time()}
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        req_label = "required" if required else "not required"
        await sender.send_message(
            phone,
            f"Add *{column_name}* ({column_type_str or 'text'}) to *{db}*, {req_label}?\n"
            "Reply *yes* to confirm or *no* to cancel."
        )
        return

    # If we have db + name but no type, ask for type
    if db and column_name:
        payload = {"chosen_db": db, "column_name": column_name}
        if required is not None:
            payload["required_prefill"] = bool(required)
        session = {"state": "waiting_column_type", "payload": payload, "created_at": time.time()}
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await sender.send_message(
            phone,
            f"What type for *{column_name}* in *{db}*?\n"
            "1. Text  2. Number  3. Select  4. Multi-select\n"
            "5. Date  6. Checkbox  7. URL  8. Email"
        )
        return

    # If we have name but no db, ask for db
    if column_name:
        payload = {"column_name": column_name}
        if required is not None:
            payload["required_prefill"] = bool(required)
        session = {"state": "waiting_column_db", "payload": payload, "created_at": time.time()}
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await sender.send_message(
            phone,
            f"Which database for *{column_name}*?\n"
            "1. daily_logs  2. tasks  3. projects\n4. learnings  5. weekly_reports"
        )
        return

    # Fallback: ask everything from scratch
    await start_add_column_flow(phone)


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
