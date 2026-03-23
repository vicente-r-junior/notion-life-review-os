import json
import re
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)
openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


def parse_json_safely(text: str) -> dict:
    text = re.sub(r"```json\s*", "", text)
    text = re.sub(r"```\s*", "", text)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            return json.loads(match.group())
        raise ValueError(f"Could not parse JSON from: {text[:200]}")


async def run_matcher(mentioned_name: str, projects_list: list) -> dict:
    prompt_path = Path("prompts/matcher.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""

    projects_str = json.dumps(projects_list)
    system_prompt = (
        template.replace("{projects_list}", projects_str)
        .replace("{mentioned_name}", mentioned_name)
    )

    response = await openai_client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Match: {mentioned_name}"},
        ],
        temperature=0,
    )

    raw = response.choices[0].message.content
    return parse_json_safely(raw)
