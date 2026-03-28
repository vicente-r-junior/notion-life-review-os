import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from app.config import settings
from app.notion.mcp_client import mcp_client
from app.observability.logger import get_logger
from app.schema.schema_manager import get_schema, DATABASE_MAP

logger = get_logger(__name__)

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": "Query a Notion database with optional filters and sorts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "data_source_id": {"type": "string"},
                    "filter": {"type": "object"},
                    "sorts": {"type": "array", "items": {"type": "object"}},
                },
                "required": ["data_source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_page",
            "description": "Create a new page in a Notion database.",
            "parameters": {
                "type": "object",
                "properties": {
                    "database_id": {"type": "string", "description": "The Notion database ID (not data_source_id)"},
                    "properties": {"type": "object", "description": "Notion properties object"},
                },
                "required": ["database_id", "properties"],
            },
        },
    },
]


async def _query_database(data_source_id: str, filter: dict = None, sorts: list = None) -> str:
    args = {"data_source_id": data_source_id}
    if filter:
        args["filter"] = filter
    if sorts:
        args["sorts"] = sorts
    result = await mcp_client.call_tool("API-query-data-source", args)
    return result.get("content", [{}])[0].get("text", "{}")


async def _create_page(database_id: str, properties: dict) -> str:
    result = await mcp_client.call_tool("API-post-page", {
        "parent": {"database_id": database_id},
        "properties": properties,
    })
    return result.get("content", [{}])[0].get("text", "{}")


async def _dispatch_tool(name: str, args: dict) -> str:
    if name == "query_database":
        return await _query_database(**args)
    elif name == "create_page":
        return await _create_page(**args)
    return "{}"


async def run_weekly_analyst(schemas: dict) -> str:
    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
    week_start = (datetime.now(ZoneInfo(settings.TIMEZONE)) - timedelta(days=7)).strftime("%Y-%m-%d")

    schema_summary = {}
    for db, schema in schemas.items():
        if schema.get("fields"):
            schema_summary[db] = {
                "data_source_id": schema.get("data_source_id", ""),
                "database_id": schema.get("database_id", ""),
                "fields": {k: v.get("type") for k, v in schema["fields"].items()},
            }

    # Also add weekly_reports database_id for page creation
    weekly_schema = get_schema("weekly_reports")
    weekly_db_id = settings.NOTION_DB_WEEKLY_REPORTS

    system_prompt = (
        "You are a personal productivity coach generating a weekly review.\n"
        f"Today: {today}. Analyze data from {week_start} to {today}.\n\n"
        "Available databases:\n"
        f"{json.dumps(schema_summary, indent=2)}\n\n"
        f"Weekly reports database_id for creating the page: {weekly_db_id}\n\n"
        "Steps:\n"
        "1. Query daily_logs filtered by Date >= {week_start}.\n"
        "2. Query tasks filtered by Date/created this week.\n"
        "3. Query learnings from this week.\n"
        "4. Analyze and generate the report.\n"
        "5. Create a page in weekly_reports with the summary.\n"
        "6. Return a WhatsApp-friendly summary.\n\n"
        "Report must include:\n"
        "- Mood trend (rising/stable/falling)\n"
        "- Average energy\n"
        "- Most active project\n"
        "- Tasks: created vs completed\n"
        "- Best learning\n"
        "- Short motivational note (2-3 sentences, warm, not cheesy)\n\n"
        "Format: WhatsApp-friendly, no markdown headers, emoji sparingly.\n"
        "If no data for the week, return: "
        "\"Hey! Looks like it was a quiet week. No worries, I'm here when you're ready.\"\n"
        "Do NOT create a page if there's no data."
    ).replace("{week_start}", week_start)

    messages = [{"role": "system", "content": system_prompt}]

    for _ in range(10):
        response = await _client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "stop":
            return msg.content or "Hey! Looks like it was a quiet week. No worries, I'm here when you're ready."

        if finish_reason == "tool_calls" and msg.tool_calls:
            messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ]})

            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    tool_result = await _dispatch_tool(tc.function.name, args)
                    logger.info("weekly_tool_called", tool=tc.function.name)
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})
                    logger.error("weekly_tool_failed", tool=tc.function.name, error=str(e))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })
        else:
            break

    return "Couldn't generate the weekly report right now. Try again later."
