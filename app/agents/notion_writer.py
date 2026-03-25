import asyncio
import difflib
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.notion.mcp_client import mcp_client
from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def run_notion_writer(payload: dict) -> str:
    today = datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")
    counts = {"projects": 0, "daily_log": 0, "tasks": 0, "learnings": 0}
    warnings = []

    # Collect all project names — explicit updates + auto-inferred from tasks
    explicit_names = {p["name"] for p in payload.get("project_updates", [])}
    auto_projects = [
        {"name": task["project"], "progress_note": f"Mentioned in task: {task['title']}"}
        for task in payload.get("tasks", [])
        if task.get("project") and task["project"] is not None and task["project"] not in explicit_names
    ]
    all_projects = payload.get("project_updates", []) + auto_projects

    # Fetch existing project names for dedup
    existing_project_names: set[str] = set()
    try:
        qr = await mcp_client.call_tool(
            "API-query-data-source",
            {"data_source_id": settings.NOTION_DB_PROJECTS},
        )
        raw = qr.get("content", [{}])[0].get("text", "{}")
        qr_data = json.loads(raw)
        for page in qr_data.get("results", []):
            titles = page.get("properties", {}).get("Name", {}).get("title", [])
            if titles:
                existing_project_names.add(titles[0].get("plain_text", "").lower())
    except Exception as e:
        logger.warning("project_dedup_fetch_failed", error=str(e))

    # 1. Projects
    for project in all_projects:
        name_lower = project["name"].lower()
        is_duplicate = any(
            difflib.SequenceMatcher(None, name_lower, existing).ratio() >= 0.8
            for existing in existing_project_names
        )
        if is_duplicate:
            logger.info("notion_project_skipped_duplicate", name=project["name"])
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

    daily_str = "✅ Daily log · " if counts["daily_log"] else ""
    result = f"Saved! {daily_str}{counts['tasks']} tasks · {counts['projects']} projects · {counts['learnings']} learnings"
    if warnings:
        result += " | ⚠️ " + ", ".join(warnings[:3])
    return result
