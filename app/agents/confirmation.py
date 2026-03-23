import json
from pathlib import Path

from openai import AsyncOpenAI

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)
openai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)


async def run_confirmation(payload: dict) -> str:
    prompt_path = Path("prompts/confirmation.md")
    template = prompt_path.read_text() if prompt_path.exists() else ""

    payload_str = json.dumps(payload, indent=2)
    system_prompt = template.replace("{payload}", payload_str)

    response = await openai_client.chat.completions.create(
        model=settings.OPENAI_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Generate the confirmation message."},
        ],
        temperature=0.3,
    )

    return response.choices[0].message.content.strip()
