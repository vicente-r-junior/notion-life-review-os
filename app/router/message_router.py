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
        # Classify intent first — query and add_column always override any active session
        from app.agents.intent_classifier import classify_intent
        intent = await classify_intent(text)

        session_raw = redis_client.get(f"session:{phone}")

        # Route to session handler only for log intent with an active non-stuck session
        if intent == "log" and session_raw:
            from app.whatsapp.handler import handle_session_reply
            session = json.loads(session_raw)
            await handle_session_reply(phone, text, session)
            return

        # For query/add_column/bulk_update: clear any stale session and proceed with fresh intent
        if session_raw and intent in ("query", "add_column", "bulk_update"):
            redis_client.delete(f"session:{phone}")

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

        if intent == "bulk_update":
            logger.info("routing_to_bulk_update", phone=mask_phone(phone))
            await _handle_bulk_update_intent(phone, text)
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


_BULK_EXTRACT_SYSTEM = """Extract bulk update details from the user message. Reply with JSON only.
Fields:
  table: one of tasks, projects, daily_logs, learnings, weekly_reports — or null
  field: the Notion field name to update (e.g. "Who", "Status", "Due Date") — or null
  value: the new value to set — or null
  filter: object describing which records to update. Can include:
    - status: e.g. "Todo", "In Progress", "Done" (null = all statuses)
    - due_today: true if user says "due today" or "for today"
    - field_empty: field name that must be empty/blank (e.g. "Who")
    - all: true if user says "all records" with no other filter

Examples:
  "update all tasks Who to Vicente" → {"table":"tasks","field":"Who","value":"Vicente","filter":{"all":true}}
  "set Who = me on all open tasks" → {"table":"tasks","field":"Who","value":"me","filter":{"status":"Todo"}}
  "update all tasks due today to In Progress" → {"table":"tasks","field":"Status","value":"In Progress","filter":{"due_today":true}}
  "fill Who for tasks where Who is empty" → {"table":"tasks","field":"Who","value":null,"filter":{"field_empty":"Who"}}

If a field is not mentioned, use null."""


async def _handle_bulk_update_intent(phone: str, text: str):
    from app.notion.mcp_client import mcp_client
    from app.schema.schema_manager import get_schema, DATABASE_MAP
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")

    # Step 1: extract intent
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _BULK_EXTRACT_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=150,
            response_format={"type": "json_object"},
        )
        info = json.loads(resp.choices[0].message.content)
    except Exception as e:
        logger.error("bulk_update_extract_failed", error=str(e))
        await sender.send_message(phone, "Couldn't parse that — try being more specific, e.g. 'update all tasks Who to Vicente'")
        return

    table = info.get("table", "tasks")
    field = info.get("field")
    value = info.get("value")
    filter_info = info.get("filter", {})

    if not field:
        await sender.send_message(phone, "Which field would you like to update?")
        return

    # Ask for value if missing
    if value is None:
        session = {
            "state": "waiting_bulk_value",
            "payload": {"table": table, "field": field, "filter": filter_info},
            "created_at": time.time(),
        }
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await sender.send_message(phone, f"What value should *{field}* be set to?")
        return

    await _execute_bulk_query_and_confirm(phone, table, field, value, filter_info, today)


async def _execute_bulk_query_and_confirm(phone: str, table: str, field: str, value: str, filter_info: dict, today: str):
    from app.notion.mcp_client import mcp_client
    from app.schema.schema_manager import get_schema, DATABASE_MAP

    schema = get_schema(table)
    data_source_id = schema.get("data_source_id", "")
    if not data_source_id:
        await sender.send_message(phone, f"Schema for *{table}* not loaded — try sending *refresh* first.")
        return

    # Build Notion filter
    notion_filter = _build_notion_filter(filter_info, field, today)

    # Query the database
    try:
        args = {"data_source_id": data_source_id}
        if notion_filter:
            args["filter"] = notion_filter
        raw = await mcp_client.call_tool("API-query-data-source", args)
        content = raw.get("content", [{}])[0].get("text", "{}")
        data = json.loads(content)
    except Exception as e:
        logger.error("bulk_update_query_failed", error=str(e))
        await sender.send_message(phone, "Couldn't fetch records from Notion. Try again?")
        return

    results = data.get("results", [])
    if not results:
        await sender.send_message(phone, f"No records found in *{table}* matching your criteria.")
        return

    # Build updates list with page_id to skip re-search in notion_writer
    updates = []
    names = []
    for page in results:
        title_list = page.get("properties", {}).get("Name", {}).get("title", [])
        name = title_list[0].get("text", {}).get("content", "—") if title_list else "—"
        updates.append({
            "table": table,
            "name": name,
            "page_id": page["id"],
            "field": field,
            "value": value,
        })
        names.append(name)

    logger.info("bulk_update_records_found", count=len(updates), table=table)

    # Show summary and ask for confirmation
    lines = [f"*{n}*" for n in names[:10]]
    if len(names) > 10:
        lines.append(f"_(+{len(names) - 10} more)_")

    summary = (
        f"Setting *{field}* → *{value}* on {len(updates)} record(s) in *{table}*:\n"
        + "\n".join(f"· {l}" for l in lines)
        + "\n\nConfirm?"
    )

    session = {
        "state": "waiting_bulk_confirm",
        "payload": {"updates": updates, "table": table, "field": field, "value": value},
        "created_at": time.time(),
    }
    redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
    await sender.send_message(phone, summary)


def _build_notion_filter(filter_info: dict, field: str, today: str) -> dict | None:
    if not filter_info or filter_info.get("all"):
        return None

    conditions = []

    if filter_info.get("due_today"):
        conditions.append({"property": "Due Date", "date": {"equals": today}})

    if filter_info.get("status"):
        conditions.append({"property": "Status", "select": {"equals": filter_info["status"]}})

    if filter_info.get("field_empty"):
        conditions.append({"property": filter_info["field_empty"], "rich_text": {"is_empty": True}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"and": conditions}


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
Fields:
  db: one of tasks, projects, daily_logs, learnings, weekly_reports — or null
  column_name: the field/column name as a string — or null
  column_type: one of text, number, select, multi_select, date, checkbox, url, email — or null
  required: true if the user says "required", "mandatory", "must", "obligatory" — false if "optional" — null if not mentioned

Examples:
  "add Who column to tasks, required" → {"db":"tasks","column_name":"Who","column_type":null,"required":true}
  "create a Priority select field on projects" → {"db":"projects","column_name":"Priority","column_type":"select","required":null}
  "new optional text column called Notes on learnings" → {"db":"learnings","column_name":"Notes","column_type":"text","required":false}

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

    # If we have everything, show confirmation summary
    if db and column_name and type_num:
        column_type = COLUMN_TYPE_MAP[type_num]
        req_label = "required" if required else "optional"
        payload = {
            "chosen_db": db,
            "column_name": column_name,
            "column_type": column_type,
            "column_type_num": type_num,
            "required": bool(required),
        }
        session = {"state": "waiting_column_confirm", "payload": payload, "created_at": time.time()}
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await sender.send_message(
            phone,
            f"Adding *{column_name}* ({column_type_str}, {req_label}) to *{db}* — confirm?"
        )
        return

    # If we have db + name but no type, ask only for type
    if db and column_name:
        payload = {"chosen_db": db, "column_name": column_name}
        if required is not None:
            payload["required_prefill"] = bool(required)
        session = {"state": "waiting_column_type", "payload": payload, "created_at": time.time()}
        redis_client.setex(f"session:{phone}", settings.SESSION_TTL, json.dumps(session))
        await sender.send_message(phone, f"What type should *{column_name}* be? text, number, select, date, checkbox, url or email")
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
