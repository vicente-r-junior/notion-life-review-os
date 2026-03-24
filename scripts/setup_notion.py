#!/usr/bin/env python3
"""
Setup script to create all required Notion database columns via MCP.
Run once after creating your Notion databases.

Usage:
    python scripts/setup_notion.py
"""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.notion.mcp_client import mcp_client
from app.config import settings

SCHEMAS = {
    "daily_logs": {
        "db_id": settings.NOTION_DB_DAILY_LOGS,
        "properties": {
            "Mood": {"type": "number", "number": {"format": "number"}},
            "Energy": {"type": "select", "select": {"options": [
                {"name": "low"}, {"name": "medium"}, {"name": "high"}
            ]}},
            "Tags": {"type": "multi_select", "multi_select": {"options": []}},
            "Summary": {"type": "rich_text", "rich_text": {}},
            "Date": {"type": "date", "date": {}},
        },
    },
    "tasks": {
        "db_id": settings.NOTION_DB_TASKS,
        "properties": {
            "Status": {"type": "select", "select": {"options": [
                {"name": "Todo"}, {"name": "In Progress"}, {"name": "Done"}
            ]}},
            "Due Date": {"type": "date", "date": {}},
            "Project": {"type": "rich_text", "rich_text": {}},
            "Daily Log": {"type": "rich_text", "rich_text": {}},
        },
    },
    "projects": {
        "db_id": settings.NOTION_DB_PROJECTS,
        "properties": {
            "Status": {"type": "select", "select": {"options": [
                {"name": "Active"}, {"name": "Paused"}, {"name": "Done"}
            ]}},
            "Progress Note": {"type": "rich_text", "rich_text": {}},
            "Last Mentioned": {"type": "date", "date": {}},
        },
    },
    "learnings": {
        "db_id": settings.NOTION_DB_LEARNINGS,
        "properties": {
            "Insight": {"type": "rich_text", "rich_text": {}},
            "Area": {"type": "select", "select": {"options": [
                {"name": "tech"}, {"name": "personal"}, {"name": "business"}, {"name": "health"}
            ]}},
            "Date": {"type": "date", "date": {}},
            "Daily Log": {"type": "rich_text", "rich_text": {}},
        },
    },
    "weekly_reports": {
        "db_id": settings.NOTION_DB_WEEKLY_REPORTS,
        "properties": {
            "Week": {"type": "date", "date": {}},
            "Summary": {"type": "rich_text", "rich_text": {}},
            "Mood Trend": {"type": "select", "select": {"options": [
                {"name": "rising"}, {"name": "stable"}, {"name": "falling"}
            ]}},
            "Tasks Closed": {"type": "number", "number": {"format": "number"}},
            "Tasks Open": {"type": "number", "number": {"format": "number"}},
        },
    },
}


async def main():
    print("Setting up Notion database schemas...")
    await mcp_client.initialize()

    for db_name, config in SCHEMAS.items():
        db_id = config["db_id"]
        if not db_id:
            print(f"  [SKIP] {db_name}: no database ID configured, skipping")
            continue

        try:
            data_source_id = db_id

            await mcp_client.call_tool(
                "API-update-a-data-source",
                {
                    "data_source_id": data_source_id,
                    "properties": config["properties"],
                },
            )
            print(f"  [OK] {db_name}: schema updated")
        except Exception as e:
            print(f"  [FAIL] {db_name}: {e}")

    print("Done!")


if __name__ == "__main__":
    asyncio.run(main())
