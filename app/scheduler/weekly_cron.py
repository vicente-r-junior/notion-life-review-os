import asyncio
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def run_weekly_report(phone: str = None):
    from app.agents.weekly_analyst import run_weekly_analyst
    from app.whatsapp.sender import send_message
    from app.schema.schema_manager import get_schema

    target_phone = phone or settings.WHATSAPP_NUMBER
    if not target_phone:
        logger.warning("weekly_report_no_phone")
        return

    try:
        schemas = {
            db: get_schema(db)
            for db in ["daily_logs", "tasks", "learnings", "weekly_reports"]
        }
        result = await run_weekly_analyst(schemas)
        await send_message(target_phone, result)
        logger.info("weekly_report_sent", phone=target_phone[:5] + "****")
    except Exception as e:
        logger.error("weekly_report_failed", error=str(e))
        await send_message(target_phone, "Failed to generate weekly report.")


def create_scheduler() -> AsyncIOScheduler:
    tz = ZoneInfo(settings.TIMEZONE)
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        run_weekly_report,
        "cron",
        day_of_week=settings.WEEKLY_REPORT_DAY,
        hour=settings.WEEKLY_REPORT_HOUR,
        minute=0,
    )

    return scheduler
