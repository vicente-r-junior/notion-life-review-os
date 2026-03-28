import json

from app.notion.mcp_client import mcp_client
from app.session.redis_store import redis_client
from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

DATABASE_MAP = {
    "daily_logs": settings.NOTION_DB_DAILY_LOGS,
    "tasks": settings.NOTION_DB_TASKS,
    "projects": settings.NOTION_DB_PROJECTS,
    "learnings": settings.NOTION_DB_LEARNINGS,
    "weekly_reports": settings.NOTION_DB_WEEKLY_REPORTS,
}


async def _fetch_schema_for_db(db_name: str, database_id: str) -> dict:
    """
    Two-step schema fetch following the Notion MCP v2 protocol:
    1. API-retrieve-a-database  → get data_sources[0].id (the real data_source_id)
    2. API-retrieve-a-data-source → get properties using that id
    """
    # Step 1: get the data_source_id from the database object
    resp = await mcp_client.call_tool(
        "API-retrieve-a-database", {"database_id": database_id}
    )
    raw = resp.get("content", [{}])[0].get("text", "{}")
    db_data = json.loads(raw)

    data_sources = db_data.get("data_sources", [])
    if not data_sources:
        logger.warning("schema_no_data_sources", db=db_name, keys=list(db_data.keys()))
        return {}

    data_source_id = data_sources[0].get("id", "")
    if not data_source_id:
        logger.warning("schema_empty_data_source_id", db=db_name)
        return {}

    logger.info("schema_data_source_found", db=db_name, data_source_id=data_source_id[:8])

    # Step 2: retrieve schema using the real data_source_id
    resp2 = await mcp_client.call_tool(
        "API-retrieve-a-data-source", {"data_source_id": data_source_id}
    )
    raw2 = resp2.get("content", [{}])[0].get("text", "{}")
    schema_data = json.loads(raw2)

    properties = schema_data.get("properties", {})
    fields = {}

    # Properties can be a dict (keyed by name) or a list of objects with a "name" field
    if isinstance(properties, dict):
        for name, prop in properties.items():
            fields[name] = {
                "type": prop.get("type"),
                "required": prop.get("type") == "title",
                "id": prop.get("id"),
            }
    elif isinstance(properties, list):
        for prop in properties:
            name = prop.get("name", "")
            if name:
                fields[name] = {
                    "type": prop.get("type"),
                    "required": prop.get("type") == "title",
                    "id": prop.get("id"),
                }

    return {"data_source_id": data_source_id, "fields": fields}


async def _rebuild_prompt():
    """Rebuild the rendered system prompt after schema changes."""
    try:
        from app.session.prompt_builder import render_system_prompt
        render_system_prompt()
    except Exception as e:
        logger.warning("prompt_rebuild_failed", error=str(e))


async def bootstrap_schemas():
    for db_name, database_id in DATABASE_MAP.items():
        if not database_id:
            continue

        cache_key = f"schema:{db_name}"
        if redis_client.get(cache_key):
            continue

        try:
            result = await _fetch_schema_for_db(db_name, database_id)
            if not result.get("fields"):
                logger.warning("schema_empty_fields", db=db_name)
                continue

            cache_value = {
                "database_id": database_id,
                "data_source_id": result["data_source_id"],
                "fields": result["fields"],
            }
            redis_client.setex(cache_key, settings.SCHEMA_CACHE_TTL, json.dumps(cache_value))
            logger.info("schema_cached", db=db_name, fields=len(result["fields"]), field_names=list(result["fields"].keys()))

        except Exception as e:
            logger.error("schema_bootstrap_failed", db=db_name, error=str(e))

    await _rebuild_prompt()


def get_schema(db_name: str) -> dict:
    raw = redis_client.get(f"schema:{db_name}")
    if not raw:
        return {}
    return json.loads(raw)


def get_data_source_id(db_name: str) -> str:
    return get_schema(db_name).get("data_source_id", "")


def mark_field_required(db_name: str, field_name: str, required: bool):
    schema = get_schema(db_name)
    if field_name in schema.get("fields", {}):
        schema["fields"][field_name]["required"] = required
        redis_client.setex(f"schema:{db_name}", settings.SCHEMA_CACHE_TTL, json.dumps(schema))


async def refresh_schemas():
    for db_name in DATABASE_MAP:
        redis_client.delete(f"schema:{db_name}")
    from app.session.prompt_builder import invalidate_system_prompt
    invalidate_system_prompt()
    await bootstrap_schemas()


async def diff_schemas():
    new_fields = {}
    for db_name, database_id in DATABASE_MAP.items():
        cached = get_schema(db_name)
        cached_fields = set(cached.get("fields", {}).keys())
        try:
            result = await _fetch_schema_for_db(db_name, database_id)
            current_fields = set(result.get("fields", {}).keys())
            added = current_fields - cached_fields
            if added:
                new_fields[db_name] = list(added)
        except Exception as e:
            logger.error("schema_diff_failed", db=db_name, error=str(e))
    return new_fields
