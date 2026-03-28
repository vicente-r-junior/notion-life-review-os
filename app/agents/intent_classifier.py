from openai import AsyncOpenAI

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)

_SYSTEM = (
    "Classify the user message as exactly one word: query, add_column, bulk_update, or log.\n"
    "query = user wants to retrieve, search, or summarize existing data "
    "(e.g. 'what tasks are due?', 'show my projects', 'how was my week?', "
    "'am I behind on anything?', 'list open tasks', 'what did I learn this week?').\n"
    "add_column = user wants to CREATE a new field/column in a database "
    "(e.g. 'add Who column to tasks', 'create a priority field on projects', "
    "'new column called Owner on Tasks', 'add a deadline field').\n"
    "IMPORTANT: 'update/set/fill a field on existing records' is NOT add_column. "
    "add_column is only for structural changes (adding a new column definition).\n"
    "bulk_update = user wants to update a field across MULTIPLE records at once "
    "(e.g. 'update all tasks Who to Vicente', 'set Who = me on all open tasks', "
    "'fill Who column for all tasks', 'update all tasks due today to In Progress', "
    "'set Who to Vicente where it is empty').\n"
    "log = anything else: logging the day, capturing tasks/learnings/mood, "
    "updating a SINGLE specific record by name, or a mix of logging and updating.\n"
    "Reply with ONLY one word: query, add_column, bulk_update, or log."
)


async def classify_intent(text: str) -> str:
    try:
        response = await _client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=5,
        )
        intent = response.choices[0].message.content.strip().lower()
        result = intent if intent in ("query", "add_column", "bulk_update", "log") else "log"
        logger.info("intent_classified", intent=result, text_preview=text[:60])
        return result
    except Exception as e:
        logger.error("intent_classification_failed", error=str(e))
        return "log"
