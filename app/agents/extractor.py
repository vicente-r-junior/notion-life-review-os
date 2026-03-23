import json
import re
from datetime import date
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


async def run_extractor(text: str, schemas: dict) -> dict:
    prompt_path = Path("prompts/extractor.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""

    today = date.today().isoformat()
    schemas_str = json.dumps(schemas, indent=2)

    system_prompt = template.replace("{today}", today).replace("{schemas}", schemas_str)

    response = await openai_client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.2,
    )

    raw = response.choices[0].message.content
    logger.info("extractor_response", length=len(raw))

    return parse_json_safely(raw)
