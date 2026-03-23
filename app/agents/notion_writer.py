import asyncio
import json
from pathlib import Path

from crewai import Agent, Task, Crew, Process

from app.agents.crew import run_crew_async
from app.agents.tools import (
    create_notion_pages,
    update_notion_page,
    search_notion,
    query_data_source,
)
from app.config import settings
from app.observability.logger import get_logger
from app.schema.schema_manager import get_schema

logger = get_logger(__name__)

WRITE_DELAY = 0.4


async def run_notion_writer(payload: dict) -> str:
    schemas = {
        db: get_schema(db)
        for db in ["daily_logs", "tasks", "projects", "learnings"]
    }

    prompt_path = Path("prompts/notion_writer.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""
    task_description = (
        template
        .replace("{payload}", json.dumps(payload, indent=2))
        .replace("{schema}", json.dumps(schemas, indent=2))
    )

    writer_agent = Agent(
        role="Notion Data Writer",
        goal="Save structured productivity data to Notion in the correct order",
        backstory="You are an expert at writing structured data to Notion databases via MCP tools.",
        tools=[create_notion_pages, update_notion_page, search_notion, query_data_source],
        llm=settings.OPENAI_MODEL,
        verbose=False,
    )

    write_task = Task(
        description=task_description,
        agent=writer_agent,
        expected_output="Confirmation message with counts of created/updated items",
    )

    crew = Crew(
        agents=[writer_agent],
        tasks=[write_task],
        process=Process.sequential,
        verbose=False,
    )

    result = await run_crew_async(crew, {"payload": json.dumps(payload)})
    return str(result)
