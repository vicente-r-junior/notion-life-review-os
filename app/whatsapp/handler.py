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
    msg_id_raw = payload.get("data", {}).get("key", {}).get("id", "")
    from_me_raw = payload.get("data", {}).get("key", {}).get("fromMe", False)
    phone_raw = extract_phone(payload)
    logger.info("webhook_all", evt=event or "none", msg_id=msg_id_raw[:8] if msg_id_raw else "none", from_me=from_me_raw, phone=mask_phone(phone_raw) if phone_raw else "none")

    if event not in ("messages.upsert", "message.new", ""):
        return

    msg_id = msg_id_raw
    if not msg_id:
        return

    from_me = from_me_raw
    if from_me:
        return

    idempotency_key = f"processed:{msg_id}"
    if redis_client.get(idempotency_key):
        logger.info("webhook_duplicate", msg_id=msg_id[:8])
        return

    redis_client.setex(idempotency_key, 86400, "1")

    phone = extract_phone(payload)
    if not phone:
        return

    masked = mask_phone(phone)
    logger.info("webhook_received", phone=masked, msg_id=msg_id)

    # 1. Check if paused
    if redis_client.get(f"paused:{phone}"):
        text = extract_text(payload)
        if text and text.strip().lower() == "*resume*":
            await handle_resume_cmd(phone)
        return

    # 2. Extract text (audio or plain) — must happen before onboarding
    #    so that ACKs and delivery receipts (no content) are dropped first
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

    # 3. Onboarding — only fires for real messages (text or audio)
    onboarded_val = redis_client.get(f"onboarded:{phone}")
    logger.info("onboarding_check", phone=masked, onboarded=str(onboarded_val))
    if not onboarded_val:
        redis_client.setex(f"onboarded:{phone}", 86400 * 365, "1")
        logger.info("onboarding_triggered", phone=masked)
        await send_message(phone,
            "Hey! 👋 I'm your personal Notion assistant.\n\n"
            "I can help you with two things:\n"
            "📝 *Daily log* — capture tasks, projects, learnings and mood just by chatting\n"
            "📊 *Weekly review* — automatic summary sent to your Notion every week\n\n"
            "Try it: 'Worked on Project X today, need to deploy by Friday!'"
        )
        return

    # 4. Check for special commands
    for cmd, handler_name in COMMANDS.items():
        if text.lower() == cmd.lower() or text.lower() == cmd.lower().strip("*"):
            await dispatch_command(handler_name, phone)
            return

    # 5. Check for active session (confirmation/choice)
    session_raw = redis_client.get(f"session:{phone}")
    if session_raw:
        session = json.loads(session_raw)
        await handle_session_reply(phone, text, session)
        return

    # 6. Process immediately — conversation history handles multi-message context
    from app.router.message_router import process_log
    asyncio.create_task(process_log(phone, text))


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



async def detect_confirmation_intent(text: str) -> str:
    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": (
                "Classify intent as exactly one word: confirm, cancel, or continue.\n"
                "confirm = user wants to save/proceed (ok, okay, yes, sure, all set, "
                "deal, looks good, save it, let's go, go ahead, yep, 👍, perfect, done, "
                "great, sounds good, confirmed, etc.)\n"
                "cancel = user wants to discard (no, cancel, skip, forget it, nope, "
                "don't save, abort, etc.)\n"
                "continue = user is adding info, correcting, or asking something\n"
                "Reply with ONLY one word."
            )},
            {"role": "user", "content": text},
        ],
        temperature=0,
        max_tokens=5,
    )
    intent = response.choices[0].message.content.strip().lower()
    return intent if intent in ("confirm", "cancel", "continue") else "continue"


_CANCEL_WORDS = {"cancel", "exit", "stop", "ignore", "abort", "quit", "nevermind", "never mind", "forget it", "no thanks", "nope"}


async def handle_session_reply(phone: str, text: str, session: dict):
    state = session.get("state")
    payload = session.get("payload", {})

    # Global cancel escape for all non-confirmation states
    if state not in ("waiting_confirmation",) and text.strip().lower() in _CANCEL_WORDS:
        redis_client.delete(f"session:{phone}")
        await send_message(phone, "Got it, no changes made!")
        return

    if state == "waiting_confirmation":
        intent = await detect_confirmation_intent(text)
        if intent == "confirm":
            pending = session.get("pending_after_confirm")
            redis_client.delete(f"session:{phone}")
            await send_message(phone, "Saving everything to Notion... 🗂️")
            await process_confirmed_log(phone, payload)
            if pending:
                await add_to_aggregation_buffer(phone, pending)
        elif intent == "cancel":
            pending = session.get("pending_after_confirm")
            redis_client.delete(f"session:{phone}")
            from app.session.conversation import clear_history
            clear_history(phone)
            await send_message(phone, "No worries, nothing saved! 😊")
            if pending:
                await add_to_aggregation_buffer(phone, pending)
        else:
            # User adding more info — pass to conversational agent with history context
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
        db_names = ["daily_logs", "tasks", "projects", "learnings", "weekly_reports"]
        _db_alias = {
            "daily logs": "daily_logs", "daily log": "daily_logs",
            "tasks": "tasks", "task": "tasks",
            "projects": "projects", "project": "projects",
            "learnings": "learnings", "learning": "learnings",
            "weekly reports": "weekly_reports", "weekly report": "weekly_reports",
        }
        normalized = text.strip().lower()
        chosen = _db_alias.get(normalized)
        if not chosen:
            try:
                idx = int(normalized) - 1
                chosen = db_names[idx] if 0 <= idx < len(db_names) else None
            except ValueError:
                pass
        if chosen:
            session["payload"]["chosen_db"] = chosen
            session["state"] = "waiting_column_name"
            redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
            await send_message(phone, "What should the new column be called?")
        else:
            await send_message(phone, "Which one — tasks, projects, daily logs, learnings, or weekly reports?")

    elif state == "waiting_column_name":
        session["payload"]["column_name"] = text
        session["state"] = "waiting_column_type"
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await send_message(phone, "What type? text, number, select, date, checkbox, url or email")

    elif state == "waiting_column_type":
        _TEXT_TO_NUM = {
            "text": "1", "rich text": "1", "string": "1",
            "number": "2", "numeric": "2",
            "select": "3", "dropdown": "3",
            "multi select": "4", "multi-select": "4", "multiselect": "4",
            "date": "5",
            "checkbox": "6", "bool": "6", "boolean": "6",
            "url": "7", "link": "7",
            "email": "8",
        }
        text = _TEXT_TO_NUM.get(text.lower().strip(), text.strip())
        if text in COLUMN_TYPE_MAP:
            session["payload"]["column_type"] = COLUMN_TYPE_MAP[text]
            session["payload"]["column_type_num"] = text
            if text in ("3", "4"):
                session["state"] = "waiting_column_options"
                redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                await send_message(phone, "List the options separated by commas (e.g. Done, In Progress, Backlog):")
            else:
                col_name = session["payload"].get("column_name", "field")
                col_db = session["payload"].get("chosen_db", "")
                type_label = {
                    "1": "text", "2": "number", "3": "select", "4": "multi-select",
                    "5": "date", "6": "checkbox", "7": "url", "8": "email",
                }.get(text, "text")

                if "required_prefill" in session["payload"]:
                    # Required was already specified — show confirmation summary
                    required = session["payload"].pop("required_prefill")
                    session["payload"]["required"] = required
                    req_label = "required" if required else "optional"
                    session["state"] = "waiting_column_confirm"
                    redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                    await send_message(phone, f"Adding *{col_name}* ({type_label}, {req_label}) to *{col_db}* — confirm?")
                else:
                    session["state"] = "waiting_column_required"
                    redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
                    await send_message(phone, f"Should *{col_name}* be a required field?")
        else:
            await send_message(phone, "What type? text, number, select, date, checkbox, url or email")

    elif state == "waiting_column_options":
        options = [o.strip() for o in text.split(",") if o.strip()]
        col_type = session["payload"]["column_type"]
        type_key = list(col_type.keys())[1] if len(col_type) > 1 else None
        if type_key:
            col_type[type_key]["options"] = [{"name": o} for o in options]
        session["payload"]["column_type"] = col_type
        session["state"] = "waiting_column_required"
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await send_message(phone, "Should this be a required field? (yes / no)")

    elif state == "waiting_column_required":
        required = text.lower() in ("yes", "y", "sim", "s", "true", "1")
        session["payload"]["required"] = required
        col_name = session["payload"].get("column_name", "field")
        col_db = session["payload"].get("chosen_db", "")
        type_num = session["payload"].get("column_type_num", "1")
        type_label = {
            "1": "text", "2": "number", "3": "select", "4": "multi-select",
            "5": "date", "6": "checkbox", "7": "url", "8": "email",
        }.get(type_num, "text")
        req_label = "required" if required else "optional"
        session["state"] = "waiting_column_confirm"
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await send_message(phone, f"Adding *{col_name}* ({type_label}, {req_label}) to *{col_db}* — confirm?")

    elif state == "waiting_bulk_value":
        # User provided the value for bulk update
        table = payload.get("table", "tasks")
        field = payload.get("field", "")
        filter_info = payload.get("filter", {})
        redis_client.delete(f"session:{phone}")
        from datetime import datetime
        from zoneinfo import ZoneInfo
        today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
        from app.router.message_router import _execute_bulk_query_and_confirm
        await _execute_bulk_query_and_confirm(phone, table, field, text.strip(), filter_info, today)

    elif state == "waiting_bulk_confirm":
        confirmed = text.lower() in ("yes", "y", "sim", "s", "confirm", "ok", "sure", "yep", "👍")
        if confirmed:
            redis_client.delete(f"session:{phone}")
            updates = payload.get("updates", [])
            await send_message(phone, f"Updating {len(updates)} record(s)... 🗂️")
            from app.agents.notion_writer import run_notion_writer
            result = await run_notion_writer({"updates": updates, "tasks": [], "learnings": [], "project_updates": []})
            await send_message(phone, result)
        else:
            redis_client.delete(f"session:{phone}")
            await send_message(phone, "No problem, nothing changed!")

    elif state == "waiting_column_confirm":
        confirmed = text.lower() in ("yes", "y", "sim", "s", "confirm", "ok", "sure", "yep", "👍")
        if confirmed:
            redis_client.delete(f"session:{phone}")
            await add_column_to_notion(phone, session["payload"])
        else:
            redis_client.delete(f"session:{phone}")
            await send_message(phone, "No problem, nothing changed!")



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
    from app.notion.mcp_client import mcp_client
    from app.schema.schema_manager import get_data_source_id, mark_field_required

    db_name = payload.get("chosen_db")
    column_name = payload.get("column_name")
    column_type = payload.get("column_type")

    data_source_id = get_data_source_id(db_name)
    if not data_source_id:
        await send_message(phone, "Could not find database schema. Try *refresh* first.")
        return

    try:
        await mcp_client.call_tool(
            "API-update-a-data-source",
            {"data_source_id": data_source_id, "properties": {column_name: column_type}},
        )
        # Refresh schema first so new column is fetched from Notion
        from app.schema.schema_manager import refresh_schemas
        await refresh_schemas()
        # Mark required after refresh so it's not overwritten
        if payload.get("required"):
            mark_field_required(db_name, column_name, True)
        await send_message(phone, f"Column *{column_name}* added to *{db_name}*! ✅")
    except Exception as e:
        logger.error("add_column_failed", error=str(e))
        await send_message(phone, f"Failed to add column: {str(e)}")
