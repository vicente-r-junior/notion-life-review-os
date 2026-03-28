from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # OpenAI
    OPENAI_API_KEY: str = ""
    OPENAI_MODEL: str = "gpt-4o"
    WHISPER_MODEL: str = "whisper-1"
    WHISPER_LANGUAGE: str = ""  # empty = auto-detect; "en", "pt", etc. to force

    # Notion
    NOTION_API_KEY: str = ""
    NOTION_DB_DAILY_LOGS: str = ""
    NOTION_DB_TASKS: str = ""
    NOTION_DB_PROJECTS: str = ""
    NOTION_DB_LEARNINGS: str = ""
    NOTION_DB_WEEKLY_REPORTS: str = ""

    # Notion MCP
    MCP_URL: str = "http://notion-mcp:3000"
    MCP_AUTH_TOKEN: str = ""

    # WhatsApp / Evolution API
    EVOLUTION_API_URL: str = "http://localhost:8080"
    EVOLUTION_API_KEY: str = ""
    EVOLUTION_INSTANCE: str = ""
    WHATSAPP_NUMBER: str = ""
    WATCHDOG_PHONE: str = ""

    # Security
    WEBHOOK_SECRET: str = ""

    # App
    APP_PORT: int = 8000
    REDIS_URL: str = "redis://redis:6379"
    TIMEZONE: str = "America/Sao_Paulo"

    # Behaviour
    MESSAGE_AGGREGATION_SILENCE: int = 15
    MESSAGE_AGGREGATION_WINDOW: int = 45
    SESSION_TTL: int = 600
    SCHEMA_CACHE_TTL: int = 3600
    WEEKLY_REPORT_DAY: str = "monday"
    WEEKLY_REPORT_HOUR: int = 8

    class Config:
        env_file = ".env"
        extra = "ignore"


settings = Settings()
