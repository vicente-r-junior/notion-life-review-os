import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from crewai import Agent, Task, Crew, Process

from app.agents.crew import run_crew_async
from app.agents.tools import query_data_source, create_notion_pages
from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def run_weekly_analyst(schemas: dict) -> str:
    prompt_path = Path("prompts/weekly_analyst.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""
    task_description = (
        template
        .replace("{today}", datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d"))
        .replace("{schema}", json.dumps(schemas, indent=2))
    )

    analyst_agent = Agent(
        role="Personal Productivity Coach",
        goal="Generate insightful weekly productivity reviews from Notion data",
        backstory="You analyze weekly data to help users understand patterns and improve.",
        tools=[query_data_source, create_notion_pages],
        llm=settings.OPENAI_MODEL,
        verbose=False,
    )

    analysis_task = Task(
        description=task_description,
        agent=analyst_agent,
        expected_output="WhatsApp-friendly weekly report with insights and motivational note",
    )

    crew = Crew(
        agents=[analyst_agent],
        tasks=[analysis_task],
        process=Process.sequential,
        verbose=False,
    )

    result = await run_crew_async(crew, {"today": datetime.now(ZoneInfo(settings.TIMEZONE)).strftime("%Y-%m-%d")})
    return str(result)
