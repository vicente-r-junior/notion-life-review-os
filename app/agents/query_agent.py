import json
from datetime import date
from pathlib import Path

from crewai import Agent, Task, Crew, Process

from app.agents.crew import run_crew_async
from app.agents.tools import query_data_source, search_notion, fetch_notion
from app.config import settings
from app.observability.logger import get_logger
from app.schema.schema_manager import get_schema

logger = get_logger(__name__)


async def run_query_agent(question: str) -> str:
    schemas = {
        db: get_schema(db)
        for db in ["daily_logs", "tasks", "projects", "learnings", "weekly_reports"]
    }

    prompt_path = Path("prompts/query_agent.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""
    task_description = (
        template
        .replace("{question}", question)
        .replace("{today}", date.today().isoformat())
        .replace("{schema}", json.dumps(schemas, indent=2))
    )

    query_agent = Agent(
        role="Personal Productivity Assistant",
        goal="Answer questions about the user's Notion workspace data",
        backstory="You help users understand their productivity data stored in Notion.",
        tools=[query_data_source, search_notion, fetch_notion],
        llm=settings.OPENAI_MODEL,
        verbose=False,
    )

    query_task = Task(
        description=task_description,
        agent=query_agent,
        expected_output="A friendly, conversational answer to the user's question",
    )

    crew = Crew(
        agents=[query_agent],
        tasks=[query_task],
        process=Process.sequential,
        verbose=False,
    )

    result = await run_crew_async(crew, {"question": question})
    return str(result)
