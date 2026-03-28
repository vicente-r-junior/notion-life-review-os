import json
from datetime import datetime
from zoneinfo import ZoneInfo

from openai import AsyncOpenAI

from app.config import settings
from app.notion.mcp_client import mcp_client
from app.observability.logger import get_logger
from app.schema.schema_manager import get_schema, bootstrap_schemas, DATABASE_MAP

logger = get_logger(__name__)

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_notion",
            "description": "Search across all Notion databases by keyword. Use for finding specific pages, tasks, or projects by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search keyword or phrase"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Query a Notion database with optional filters. "
                "Use this to list tasks, projects, learnings, daily logs, etc. "
                "Pass the data_source_id from the schema. "
                "Filter format follows Notion API filter syntax."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "data_source_id": {"type": "string", "description": "The database ID to query"},
                    "filter": {
                        "type": "object",
                        "description": "Optional Notion filter object, e.g. {\"property\": \"Status\", \"select\": {\"equals\": \"Todo\"}}",
                    },
                    "sorts": {
                        "type": "array",
                        "items": {"type": "object"},
                        "description": "Optional sort array, e.g. [{\"property\": \"Due Date\", \"direction\": \"ascending\"}]",
                    },
                },
                "required": ["data_source_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch the full content of a specific Notion page by its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "page_id": {"type": "string", "description": "The Notion page ID"},
                },
                "required": ["page_id"],
            },
        },
    },
]


async def _search_notion(query: str) -> str:
    result = await mcp_client.call_tool("API-post-search", {"query": query})
    return result.get("content", [{}])[0].get("text", "{}")


async def _query_database(data_source_id: str, filter: dict = None, sorts: list = None) -> str:
    args = {"data_source_id": data_source_id}
    if filter:
        args["filter"] = filter
    if sorts:
        args["sorts"] = sorts
    result = await mcp_client.call_tool("API-query-data-source", args)
    return result.get("content", [{}])[0].get("text", "{}")


async def _fetch_page(page_id: str) -> str:
    result = await mcp_client.call_tool("API-retrieve-a-page", {"page_id": page_id})
    return result.get("content", [{}])[0].get("text", "{}")


async def _dispatch_tool(name: str, args: dict) -> str:
    if name == "search_notion":
        return await _search_notion(**args)
    elif name == "query_database":
        return await _query_database(**args)
    elif name == "fetch_page":
        return await _fetch_page(**args)
    return "{}"


async def run_query_agent(question: str) -> str:
    # Lazy schema bootstrap: ensure schemas are loaded before answering
    schemas = {db: get_schema(db) for db in DATABASE_MAP}
    if not any(s.get("fields") for s in schemas.values()):
        logger.warning("query_agent_schemas_empty_bootstrapping")
        await bootstrap_schemas()
        schemas = {db: get_schema(db) for db in DATABASE_MAP}

    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")

    # Build schema context: only expose database_id and field names/types per db
    schema_summary = {}
    for db, schema in schemas.items():
        if schema.get("fields"):
            schema_summary[db] = {
                "data_source_id": schema.get("data_source_id", ""),
                "fields": {k: v.get("type") for k, v in schema["fields"].items()},
            }

    system_prompt = (
        "You are a personal productivity assistant with direct access to the user's Notion workspace.\n"
        f"Today's date: {today}\n\n"
        "Available databases and their data_source_ids:\n"
        f"{json.dumps(schema_summary, indent=2)}\n\n"
        "Rules:\n"
        "- Use query_database with the data_source_id to retrieve records — prefer this over search_notion for structured queries.\n"
        "- Never show raw JSON, page IDs, or technical fields to the user.\n"
        "- Format the answer for WhatsApp: short lines, no markdown headers, use · as separator.\n"
        "- Use 1-2 emoji sparingly where natural.\n"
        "- If no data found, respond with a friendly empty-state message.\n"
        "- Keep answers concise: 1 line per item max."
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    # Agentic loop: let the model call tools until it produces a final answer
    for _ in range(6):  # max 6 tool rounds
        response = await _client.chat.completions.create(
            model=settings.OPENAI_MODEL,
            messages=messages,
            tools=_TOOLS,
            tool_choice="auto",
        )

        msg = response.choices[0].message
        finish_reason = response.choices[0].finish_reason

        if finish_reason == "stop":
            return msg.content or "No data found."

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
                    logger.info("query_tool_called", tool=tc.function.name, args_preview=str(args)[:80])
                except Exception as e:
                    tool_result = json.dumps({"error": str(e)})
                    logger.error("query_tool_failed", tool=tc.function.name, error=str(e))

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })
        else:
            break

    return "Sorry, I couldn't retrieve that information right now."
