"""
Builds and caches the rendered system prompt for the conversational agent.

The template (prompts/conversational_agent.md) has static placeholders that
reflect the current Notion schema. These are rendered once and cached in Redis.
The only placeholder resolved at message time is {today}.

Rendered prompt is invalidated and rebuilt whenever schemas change
(bootstrap, refresh, add_column).
"""
import json
from pathlib import Path

from app.session.redis_store import redis_client
from app.observability.logger import get_logger

logger = get_logger(__name__)

PROMPT_CACHE_KEY = "rendered_prompt:conversational_agent"
PROMPT_TEMPLATE_PATH = Path("prompts/conversational_agent.md")


def _build_schema_context(schemas: dict) -> tuple[str, str, str]:
    """
    Returns (schema_context, required_fields, task_extra_fields) from cached schemas.
    """
    schema_lines = []
    required_lines = []
    task_extra = []

    db_labels = {
        "tasks": "Tasks",
        "projects": "Projects",
        "daily_logs": "Daily Logs",
        "learnings": "Learnings",
        "weekly_reports": "Weekly Reports",
    }

    # Fields always handled natively by notion_writer — exclude from schema_context
    # so the LLM doesn't confuse them with custom fields
    _NATIVE_DB_FIELDS = {
        "tasks":        {"name", "status", "project", "due date", "daily log"},
        "daily_logs":   {"name", "date", "mood", "energy", "summary", "tags"},
        "projects":     {"name", "status", "progress note", "last mentioned"},
        "learnings":    {"name", "insight", "area", "date", "daily log"},
        "weekly_reports": {"name", "date", "summary", "tags"},
    }

    for db_name, schema in schemas.items():
        fields = schema.get("fields", {})
        if not fields:
            continue

        native = _NATIVE_DB_FIELDS.get(db_name, {"name"})
        label = db_labels.get(db_name, db_name)
        field_parts = []
        for field_name, meta in fields.items():
            if field_name.lower() in native:
                continue  # skip native fields — already known by the agent
            ftype = meta.get("type", "text")
            req = meta.get("required", False)
            marker = " *(required)*" if req else ""
            field_parts.append(f"{field_name} ({ftype}){marker}")

            # Collect required non-native fields per db
            if req:
                required_lines.append(f"- {label} → **{field_name}** ({ftype})")

            # Collect extra required fields for tasks
            if db_name == "tasks" and req:
                task_extra.append(field_name)

        if field_parts:
            schema_lines.append(f"**{label}**: {', '.join(field_parts)}")

    schema_context = "\n".join(schema_lines) if schema_lines else "_(schema not loaded yet)_"

    if required_lines:
        required_fields = (
            "For every record, you MUST collect these fields before saving:\n"
            + "\n".join(required_lines)
        )
    else:
        required_fields = "_(no custom required fields defined)_"

    task_extra_str = (", " + ", ".join(task_extra)) if task_extra else ""

    return schema_context, required_fields, task_extra_str


def render_system_prompt() -> str:
    """
    Reads the template, injects schema context, and caches the result.
    Returns the rendered prompt (without {today} — that's filled at message time).
    """
    from app.schema.schema_manager import get_schema, DATABASE_MAP

    schemas = {db: get_schema(db) for db in DATABASE_MAP}
    schema_context, required_fields, task_extra_fields = _build_schema_context(schemas)

    template = PROMPT_TEMPLATE_PATH.read_text()
    rendered = (
        template
        .replace("{schema_context}", schema_context)
        .replace("{required_fields}", required_fields)
        .replace("{task_extra_fields}", task_extra_fields)
        # Leave {today} and {today[:4]} untouched — resolved at message time
    )

    redis_client.set(PROMPT_CACHE_KEY, rendered)
    logger.info("system_prompt_rendered", required_fields_count=len(required_fields.splitlines()))
    return rendered


def get_system_prompt() -> str:
    """Returns cached rendered prompt, rebuilding if missing."""
    cached = redis_client.get(PROMPT_CACHE_KEY)
    if cached:
        return cached if isinstance(cached, str) else cached.decode()
    logger.warning("system_prompt_cache_miss_rebuilding")
    return render_system_prompt()


def invalidate_system_prompt():
    """Call after any schema change to force rebuild on next message."""
    redis_client.delete(PROMPT_CACHE_KEY)
    logger.info("system_prompt_invalidated")
