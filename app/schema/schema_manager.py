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


async def bootstrap_schemas():
    for db_name, database_id in DATABASE_MAP.items():
        if not database_id:
            continue

        cache_key = f"schema:{db_name}"
        cached = redis_client.get(cache_key)
        if cached:
            continue

        try:
            db_info = await mcp_client.call_tool(
                "API-retrieve-a-database", {"database_id": database_id}
            )
            raw = db_info.get("content", [{}])[0].get("text", "{}")
            db_data = json.loads(raw)
            data_source_id = db_data["data_sources"][0]["id"]

            schema = await mcp_client.call_tool(
                "API-retrieve-a-data-source", {"data_source_id": data_source_id}
            )
            raw = schema.get("content", [{}])[0].get("text", "{}")
            schema_data = json.loads(raw)

            fields = {}
            for name, prop in schema_data.get("properties", {}).items():
                fields[name] = {
                    "type": prop.get("type"),
                    "required": prop.get("type") == "title",
                    "id": prop.get("id"),
                }

            cache_value = {
                "database_id": database_id,
                "data_source_id": data_source_id,
                "fields": fields,
            }

            redis_client.setex(
                cache_key,
                settings.SCHEMA_CACHE_TTL,
                json.dumps(cache_value),
            )
            logger.info("schema_cached", db=db_name, fields=len(fields))

        except Exception as e:
            logger.error("schema_bootstrap_failed", db=db_name, error=str(e))


def get_schema(db_name: str) -> dict:
    raw = redis_client.get(f"schema:{db_name}")
    if not raw:
        return {}
    return json.loads(raw)


def get_data_source_id(db_name: str) -> str:
    schema = get_schema(db_name)
    return schema.get("data_source_id", "")


def mark_field_required(db_name: str, field_name: str, required: bool):
    schema = get_schema(db_name)
    if field_name in schema.get("fields", {}):
        schema["fields"][field_name]["required"] = required
        redis_client.setex(
            f"schema:{db_name}",
            settings.SCHEMA_CACHE_TTL,
            json.dumps(schema),
        )


async def refresh_schemas():
    for db_name in DATABASE_MAP:
        redis_client.delete(f"schema:{db_name}")
    await bootstrap_schemas()


async def diff_schemas():
    new_fields = {}
    for db_name in DATABASE_MAP:
        cached = get_schema(db_name)
        cached_fields = set(cached.get("fields", {}).keys())

        data_source_id = cached.get("data_source_id")
        if not data_source_id:
            continue

        try:
            schema = await mcp_client.call_tool(
                "API-retrieve-a-database", {"database_id": data_source_id}
            )
            raw = schema.get("content", [{}])[0].get("text", "{}")
            schema_data = json.loads(raw)
            current_fields = set(schema_data.get("properties", {}).keys())
            added = current_fields - cached_fields
            if added:
                new_fields[db_name] = list(added)
        except Exception as e:
            logger.error("schema_diff_failed", db=db_name, error=str(e))

    return new_fields
