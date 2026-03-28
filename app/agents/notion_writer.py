import asyncio
import json
from datetime import datetime
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from app.notion.mcp_client import mcp_client
from app.config import settings
from app.observability.logger import get_logger
from app.schema.schema_manager import get_schema, DATABASE_MAP

logger = get_logger(__name__)


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.65


def _format_property(field_type: str, value: str) -> dict:
    """Convert a string value to the correct Notion property format based on field type."""
    if field_type == "select":
        return {"select": {"name": value}}
    elif field_type == "multi_select":
        options = [v.strip() for v in value.split(",") if v.strip()]
        return {"multi_select": [{"name": o} for o in options]}
    elif field_type == "date":
        return {"date": {"start": value}}
    elif field_type == "number":
        try:
            return {"number": float(value)}
        except ValueError:
            return {"number": 0}
    elif field_type == "checkbox":
        return {"checkbox": value.lower() in ("true", "yes", "1", "done")}
    elif field_type == "url":
        return {"url": value}
    elif field_type == "email":
        return {"email": value}
    elif field_type == "title":
        return {"title": [{"text": {"content": value}}]}
    else:
        return {"rich_text": [{"text": {"content": value}}]}


async def run_notion_writer(payload: dict) -> str:
    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
    counts = {"projects": 0, "daily_log": 0, "tasks": 0, "learnings": 0}
    warnings = []

    # Collect all project names — explicit updates + auto-inferred from tasks (deduplicated)
    explicit_names = {p["name"] for p in payload.get("project_updates", [])}
    seen_names = set(explicit_names)
    auto_projects = []
    for task in payload.get("tasks", []):
        name = task.get("project")
        if name and name not in seen_names:
            auto_projects.append({"name": name, "progress_note": f"Mentioned in task: {task['title']}"})
            seen_names.add(name)
    all_projects = payload.get("project_updates", []) + auto_projects

    # 1. Projects
    existing_project_names: list[str] = []
    for project in all_projects:
        # Search Notion for existing projects with similar name
        try:
            search_raw = await mcp_client.call_tool(
                "API-post-search",
                {"query": project["name"]},
            )
            content = search_raw.get("content", [{}])[0].get("text", "{}")
            search_data = json.loads(content)
            for result in search_data.get("results", []):
                if result.get("object") == "page":
                    props = result.get("properties", {})
                    title_list = props.get("Name", {}).get("title", [])
                    if title_list:
                        name = title_list[0].get("text", {}).get("content", "")
                        if name:
                            existing_project_names.append(name)
                            logger.info("found_existing_project", name=name)
        except Exception as e:
            logger.warning("project_search_failed", error=str(e))

        already_exists = any(_similar(project["name"], n) for n in existing_project_names)
        if already_exists:
            logger.info("project_skipped_duplicate", name=project["name"])
            continue
        try:
            await mcp_client.call_tool("API-post-page", {
                "parent": {"database_id": settings.NOTION_DB_PROJECTS},
                "properties": {
                    "Name": {"title": [{"text": {"content": project["name"]}}]},
                    "Status": {"select": {"name": "Active"}},
                    "Progress Note": {"rich_text": [{"text": {"content": project.get("progress_note", "")}}]},
                    "Last Mentioned": {"date": {"start": today}},
                },
            })
            counts["projects"] += 1
            existing_project_names.append(project["name"])
            logger.info("notion_project_created", name=project["name"])
        except Exception as e:
            logger.error("notion_project_failed", name=project["name"], error=str(e))
            warnings.append(f"Project '{project['name']}': {str(e)[:50]}")
        await asyncio.sleep(0.4)

    # 2. Daily Log
    try:
        tags = [{"name": t} for t in payload.get("tags", [])]
        await mcp_client.call_tool("API-post-page", {
            "parent": {"database_id": settings.NOTION_DB_DAILY_LOGS},
            "properties": {
                "Name": {"title": [{"text": {"content": f"Daily Log for {today}"}}]},
                "Date": {"date": {"start": today}},
                "Mood": {"number": payload.get("mood", 3)},
                "Energy": {"select": {"name": payload.get("energy", "medium")}},
                "Summary": {"rich_text": [{"text": {"content": payload.get("summary", "")}}]},
                "Tags": {"multi_select": tags},
            },
        })
        counts["daily_log"] = 1
        logger.info("notion_daily_log_created", date=today)
    except Exception as e:
        logger.error("notion_daily_log_failed", error=str(e))
        warnings.append(f"Daily log: {str(e)[:50]}")
    await asyncio.sleep(0.4)

    # 3. Tasks
    _NATIVE_TASK_KEYS = {"title", "project", "due_date", "status"}
    task_schema = get_schema("tasks")
    task_fields = task_schema.get("fields", {})

    for task in payload.get("tasks", []):
        try:
            props = {
                "Name": {"title": [{"text": {"content": task["title"]}}]},
                "Status": {"select": {"name": "Todo"}},
                "Project": {"rich_text": [{"text": {"content": task.get("project") or ""}}]},
                "Daily Log": {"rich_text": [{"text": {"content": f"Daily Log for {today}"}}]},
            }
            if task.get("due_date"):
                props["Due Date"] = {"date": {"start": task["due_date"]}}

            # Write any custom fields present in task dict using schema type
            for key, val in task.items():
                if key.lower() in _NATIVE_TASK_KEYS or not val:
                    continue
                # Find matching field in schema (case-insensitive)
                field_key = None
                for k in task_fields:
                    if k.lower() == key.lower():
                        field_key = k
                        break
                if field_key:
                    ftype = task_fields[field_key].get("type", "rich_text")
                    props[field_key] = _format_property(ftype, str(val))
                    logger.info("notion_task_custom_field", field=field_key, value=str(val)[:40])

            await mcp_client.call_tool("API-post-page", {
                "parent": {"database_id": settings.NOTION_DB_TASKS},
                "properties": props,
            })
            counts["tasks"] += 1
            logger.info("notion_task_created", title=task["title"])
        except Exception as e:
            logger.error("notion_task_failed", title=task["title"], error=str(e))
            warnings.append(f"Task '{task['title']}': {str(e)[:50]}")
        await asyncio.sleep(0.4)

    # 4. Learnings
    for learning in payload.get("learnings", []):
        try:
            await mcp_client.call_tool("API-post-page", {
                "parent": {"database_id": settings.NOTION_DB_LEARNINGS},
                "properties": {
                    "Name": {"title": [{"text": {"content": learning["insight"]}}]},
                    "Insight": {"rich_text": [{"text": {"content": learning["insight"]}}]},
                    "Area": {"select": {"name": learning.get("area", "tech")}},
                    "Date": {"date": {"start": today}},
                    "Daily Log": {"rich_text": [{"text": {"content": f"Daily Log for {today}"}}]},
                },
            })
            counts["learnings"] += 1
            logger.info("notion_learning_created")
        except Exception as e:
            logger.error("notion_learning_failed", error=str(e))
            warnings.append(f"Learning: {str(e)[:50]}")
        await asyncio.sleep(0.4)

    # 5. Updates to existing records
    counts["updates"] = 0
    raw_updates = payload.get("updates", [])
    logger.info("updates_received", count=len(raw_updates), updates=str(raw_updates)[:200])

    # Lazy schema refresh: if any update table schema is missing, bootstrap now
    if raw_updates:
        from app.schema.schema_manager import bootstrap_schemas
        tables_needed = {u.get("table", "tasks") for u in raw_updates}
        for t in tables_needed:
            if not get_schema(t).get("fields"):
                logger.warning("schema_missing_triggering_bootstrap", table=t)
                await bootstrap_schemas()
                break

    for update in raw_updates:
        name = update.get("name", "")
        table = update.get("table", "tasks")
        field_name = update.get("field", "")
        value = update.get("value", "")
        if not name or not field_name or value == "":
            continue
        try:
            # Resolve field type from cached schema
            schema = get_schema(table)
            fields = schema.get("fields", {})
            # Exact match first, then case-insensitive, then fuzzy
            field_key = None
            if field_name in fields:
                field_key = field_name
            else:
                for k in fields:
                    if k.lower() == field_name.lower():
                        field_key = k
                        break
            if not field_key:
                for k in fields:
                    if _similar(field_name, k):
                        field_key = k
                        break
            if not fields:
                logger.warning("schema_missing_for_update", table=table, name=name)
            if not field_key:
                logger.warning("field_not_in_schema", table=table, field=field_name, available=list(fields.keys()))
            field_type = fields[field_key]["type"] if field_key else "rich_text"
            notion_value = _format_property(field_type, str(value))

            # Use page_id directly if provided (bulk update), otherwise search by name
            page_id = update.get("page_id")
            if not page_id:
                db_id = DATABASE_MAP.get(table, "")
                search_raw = await mcp_client.call_tool("API-post-search", {"query": name})
                content = search_raw.get("content", [{}])[0].get("text", "{}")
                search_data = json.loads(content)
                for result in search_data.get("results", []):
                    if result.get("object") != "page":
                        continue
                    parent_db = result.get("parent", {}).get("database_id", "").replace("-", "")
                    if db_id and parent_db != db_id.replace("-", ""):
                        continue
                    title_list = result.get("properties", {}).get("Name", {}).get("title", [])
                    if title_list:
                        page_name = title_list[0].get("text", {}).get("content", "")
                        if _similar(name, page_name):
                            page_id = result["id"]
                            break

            if not page_id:
                warnings.append(f"'{name}' not found in Notion")
                continue

            await mcp_client.call_tool("API-patch-page", {
                "page_id": page_id,
                "properties": {field_key or field_name: notion_value},
            })
            counts["updates"] += 1
            logger.info("notion_record_updated", name=name, field=field_key, value=value)
        except Exception as e:
            logger.error("notion_update_failed", name=name, error=str(e))
            warnings.append(f"Update '{name}': {str(e)[:50]}")
        await asyncio.sleep(0.4)

    daily_str = "✅ Daily log · " if counts["daily_log"] else ""
    update_str = f" · {counts['updates']} updated" if counts["updates"] else ""
    result = f"Saved! {daily_str}{counts['tasks']} tasks · {counts['projects']} projects · {counts['learnings']} learnings{update_str}"
    if warnings:
        result += " | ⚠️ " + ", ".join(warnings[:3])
    return result
