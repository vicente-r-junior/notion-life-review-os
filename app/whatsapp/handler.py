import asyncio
import json
import time

from openai import AsyncOpenAI

from app.config import settings
from app.observability.logger import get_logger, mask_phone
from app.session.redis_store import redis_client
from app.whatsapp.sender import send_message
from app.audio.transcriber import transcribe

logger = get_logger(__name__)


async def send_welcome(phone: str):
    from app.session.conversation import append_history
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": (
                "You are a warm personal productivity assistant on WhatsApp. "
                "This is your first message to a new user.\n\n"
                "Write a SHORT welcome message (3-4 lines max) that:\n"
                "- Feels like a real person texting, not a bot\n"
                "- Explains you help capture daily logs, tasks and learnings to Notion\n"
                "- Invites them to just tell you about their day naturally\n"
                "- Uses 1-2 emoji max\n"
                "- Does NOT list commands or features — keep it simple and human\n\n"
                "Example tone (don't copy exactly):\n"
                "Hey! I'm your Notion productivity assistant 👋\n"
                "Just tell me about your day — tasks, projects, how you're feeling —\n"
                "and I'll take care of saving everything to Notion for you.\n"
                "What's on your mind?"
            )},
        ],
        temperature=0.7,
    )
    msg = response.choices[0].message.content.strip()
    append_history(phone, "assistant", msg)
    await send_message(phone, msg)

COMMANDS = {
    "*help*": "handle_help",
    "*status*": "handle_status",
    "*week*": "handle_week",
    "*undo*": "handle_undo",
    "*pause*": "handle_pause",
    "*resume*": "handle_resume",
    "*refresh*": "handle_refresh",
}

SILENCE_SECONDS = 15
WINDOW_SECONDS = 45

COLUMN_TYPE_MAP = {
    "1": {"type": "rich_text", "rich_text": {}},
    "2": {"type": "number", "number": {"format": "number"}},
    "3": {"type": "select", "select": {"options": []}},
    "4": {"type": "multi_select", "multi_select": {"options": []}},
    "5": {"type": "date", "date": {}},
    "6": {"type": "checkbox", "checkbox": {}},
    "7": {"type": "url", "url": {}},
    "8": {"type": "email", "email": {}},
}


def extract_text(payload: dict) -> str | None:
    message = payload.get("data", {}).get("message", {})
    if "conversation" in message:
        return message["conversation"]
    if "extendedTextMessage" in message:
        return message["extendedTextMessage"].get("text")
    if "audioMessage" in message:
        return None
    return None


def extract_audio(payload: dict) -> dict | None:
    message = payload.get("data", {}).get("message", {})
    if "audioMessage" in message:
        return {
            "url": message["audioMessage"].get("url"),
            "mimetype": message["audioMessage"].get("mimetype", "audio/ogg"),
            "seconds": message["audioMessage"].get("seconds", 0),
        }
    return None


def extract_phone(payload: dict) -> str:
    remote_jid = payload.get("data", {}).get("key", {}).get("remoteJid", "")
    return remote_jid.replace("@s.whatsapp.net", "").replace("@g.us", "")


async def handle_webhook(payload: dict):
    event = payload.get("event", "")
    if event not in ("messages.upsert", "message.new", ""):
        return

    msg_id = payload.get("data", {}).get("key", {}).get("id")
    if not msg_id:
        return

    from_me = payload.get("data", {}).get("key", {}).get("fromMe", False)
    if from_me:
        return

    idempotency_key = f"processed:{msg_id}"
    if redis_client.get(idempotency_key):
        return

    redis_client.setex(idempotency_key, 86400, "1")

    phone = extract_phone(payload)
    if not phone:
        return

    masked = mask_phone(phone)
    logger.info("webhook_received", phone=masked, msg_id=msg_id)

    # Check if paused
    if redis_client.get(f"paused:{phone}"):
        text = extract_text(payload)
        if text and text.strip().lower() == "*resume*":
            await handle_resume_cmd(phone)
        return

    # Check for audio
    audio = extract_audio(payload)
    if audio:
        await send_message(phone, "🎙️ Give me a sec...")
        try:
            text = await transcribe(audio["url"], audio["mimetype"])
            text = f"[Voice message]: {text}"
            logger.info("audio_transcribed", phone=masked, seconds=audio["seconds"])
        except Exception as e:
            logger.error("transcription_failed", error=str(e))
            await send_message(phone, "Couldn't catch that audio, try again? 🎙️")
            return
    else:
        text = extract_text(payload)

    if not text:
        return

    text = text.strip()

    # Onboarding — send welcome to new users then continue normally
    if not redis_client.get(f"onboarded:{phone}"):
        redis_client.set(f"onboarded:{phone}", "1")
        await send_welcome(phone)

    # Check for special commands
    for cmd, handler_name in COMMANDS.items():
        if text.lower() == cmd.lower() or text.lower() == cmd.lower().strip("*"):
            await dispatch_command(handler_name, phone)
            return

    # Check for active session (confirmation/choice)
    session_raw = redis_client.get(f"session:{phone}")
    if session_raw:
        session = json.loads(session_raw)
        await handle_session_reply(phone, text, session)
        return

    # Add to aggregation buffer
    await add_to_aggregation_buffer(phone, text)


async def add_to_aggregation_buffer(phone: str, text: str):
    key = f"aggregating:{phone}"
    existing = redis_client.get(key)
    now = time.time()

    if existing:
        session = json.loads(existing)
        session["messages"].append(text)
        session["last_message_at"] = now
    else:
        session = {
            "messages": [text],
            "started_at": now,
            "last_message_at": now,
            "phone": phone,
        }

    redis_client.setex(key, WINDOW_SECONDS + 10, json.dumps(session))



async def handle_session_reply(phone: str, text: str, session: dict):
    state = session.get("state")
    payload = session.get("payload", {})

    if state == "waiting_confirmation":
        if text.lower() in ("confirm", "yes", "y", "sim"):
            pending = session.get("pending_after_confirm")
            redis_client.delete(f"session:{phone}")
            await send_message(phone, "Saving everything to Notion... 🗂️")
            await process_confirmed_log(phone, payload)
            if pending:
                await add_to_aggregation_buffer(phone, pending)
        elif text.lower() in ("cancel", "no", "n", "nao"):
            pending = session.get("pending_after_confirm")
            redis_client.delete(f"session:{phone}")
            from app.session.conversation import clear_history
            clear_history(phone)
            await send_message(phone, "No worries, nothing was saved! Send me a new message whenever you're ready 😊")
            if pending:
                await add_to_aggregation_buffer(phone, pending)
        else:
            # Let the conversational agent handle corrections with history context
            from app.router.message_router import process_log
            redis_client.delete(f"session:{phone}")
            await process_log(phone, text)

    elif state == "waiting_project_choice":
        candidates = payload.get("candidates", [])
        try:
            idx = int(text.strip()) - 1
            if 0 <= idx < len(candidates):
                chosen = candidates[idx]
                payload["resolved_project"] = chosen
                session["state"] = "waiting_confirmation"
                session["payload"] = payload
                redis_client.setex(
                    f"session:{phone}",
                    settings.SESSION_TTL,
                    json.dumps(session),
                )
                await send_message(phone, f"Got it! Using project: *{chosen['name']}*\nReply *confirm* to save or *cancel* to discard.")
            else:
                await send_message(phone, f"Please choose a number between 1 and {len(candidates)}.")
        except ValueError:
            await send_message(phone, "Please reply with a number.")

    elif state == "waiting_column_db":
        db_names = list(["daily_logs", "tasks", "projects", "learnings", "weekly_reports"])
        try:
            idx = int(text.strip()) - 1
            if 0 <= idx < len(db_names):
                session["payload"]["chosen_db"] = db_names[idx]
                session["state"] = "waiting_column_name"
                redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                await send_message(phone, "What should the new column be called?")
            else:
                await send_message(phone, f"Choose a number 1-{len(db_names)}.")
        except ValueError:
            await send_message(phone, "Reply with a number.")

    elif state == "waiting_column_name":
        session["payload"]["column_name"] = text
        session["state"] = "waiting_column_type"
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        msg = (
            "What type?\n"
            "1. Text\n2. Number\n3. Select\n4. Multi-select\n"
            "5. Date\n6. Checkbox\n7. URL\n8. Email"
        )
        await send_message(phone, msg)

    elif state == "waiting_column_type":
        if text in COLUMN_TYPE_MAP:
            session["payload"]["column_type"] = COLUMN_TYPE_MAP[text]
            session["payload"]["column_type_num"] = text
            if text in ("3", "4"):
                session["state"] = "waiting_column_options"
                redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                await send_message(phone, "List the options separated by commas (e.g. Done, In Progress, Backlog):")
            else:
                session["state"] = "waiting_column_required"
                redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                await send_message(phone, "Should this field be required? (yes/no)")
        else:
            await send_message(phone, "Choose a number 1-8.")

    elif state == "waiting_column_options":
        options = [o.strip() for o in text.split(",") if o.strip()]
        col_type = session["payload"]["column_type"]
        type_key = list(col_type.keys())[1] if len(col_type) > 1 else None
        if type_key:
            col_type[type_key]["options"] = [{"name": o} for o in options]
        session["payload"]["column_type"] = col_type
        session["state"] = "waiting_column_required"
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await send_message(phone, "Should this field be required? (yes/no)")

    elif state == "waiting_column_required":
        required = text.lower() in ("yes", "y", "sim", "s")
        session["payload"]["required"] = required
        redis_client.delete(f"session:{phone}")
        await add_column_to_notion(phone, session["payload"])



async def dispatch_command(handler_name: str, phone: str):
    handlers = {
        "handle_help": handle_help_cmd,
        "handle_status": handle_status_cmd,
        "handle_week": handle_week_cmd,
        "handle_undo": handle_undo_cmd,
        "handle_pause": handle_pause_cmd,
        "handle_resume": handle_resume_cmd,
        "handle_refresh": handle_refresh_cmd,
    }
    handler = handlers.get(handler_name)
    if handler:
        await handler(phone)


async def handle_help_cmd(phone: str):
    msg = (
        "*Life Review OS Commands*\n\n"
        "*help* - show this message\n"
        "*status* - check service health\n"
        "*week* - generate weekly report now\n"
        "*undo* - delete last entry\n"
        "*pause* - pause the bot for 24h\n"
        "*resume* - resume the bot\n"
        "*refresh* - refresh Notion schema cache\n\n"
        "Just send a message about your day to log it!"
    )
    await send_message(phone, msg)


async def handle_status_cmd(phone: str):
    from app.observability.health import get_health
    health = await get_health()
    lines = [f"*Status:* {health['status']}"]
    for svc, info in health["services"].items():
        icon = "OK" if info["status"] == "healthy" else "FAIL"
        lines.append(f"[{icon}] {svc}")
    await send_message(phone, "\n".join(lines))


async def handle_week_cmd(phone: str):
    await send_message(phone, "Generating your weekly report...")
    from app.scheduler.weekly_cron import run_weekly_report
    asyncio.create_task(run_weekly_report(phone))


async def handle_undo_cmd(phone: str):
    await send_message(phone, "Undo is not yet implemented.")


async def handle_pause_cmd(phone: str):
    redis_client.setex(f"paused:{phone}", 86400, "1")
    await send_message(phone, "Got it! I'll be quiet for 24h. Send *resume* anytime to wake me up ⏸️")


async def handle_resume_cmd(phone: str):
    redis_client.delete(f"paused:{phone}")
    await send_message(phone, "I'm back! Tell me about your day 🚀")


async def handle_refresh_cmd(phone: str):
    from app.schema.schema_manager import refresh_schemas
    await send_message(phone, "Refreshing Notion schemas...")
    await refresh_schemas()
    await send_message(phone, "Schemas refreshed!")


async def process_confirmed_log(phone: str, payload: dict):
    from app.agents.notion_writer import run_notion_writer
    from app.session.conversation import clear_history
    try:
        result = await run_notion_writer(payload)
        clear_history(phone)
        await send_message(phone, result)
    except Exception as e:
        logger.error("notion_write_failed", error=str(e))
        await send_message(phone, "Failed to save to Notion. Please try again.")


async def add_column_to_notion(phone: str, payload: dict):
    from app.agents.tools import update_data_source
    from app.schema.schema_manager import get_data_source_id
    import json as json_lib

    db_name = payload.get("chosen_db")
    column_name = payload.get("column_name")
    column_type = payload.get("column_type")

    data_source_id = get_data_source_id(db_name)
    if not data_source_id:
        await send_message(phone, "Could not find database schema. Try *refresh* first.")
        return

    properties = {column_name: column_type}
    try:
        update_data_source(data_source_id, json_lib.dumps(properties))
        await send_message(phone, f"Column *{column_name}* added to *{db_name}*!")
        if payload.get("required"):
            from app.schema.schema_manager import mark_field_required
            mark_field_required(db_name, column_name, True)
    except Exception as e:
        logger.error("add_column_failed", error=str(e))
        await send_message(phone, f"Failed to add column: {str(e)}")
