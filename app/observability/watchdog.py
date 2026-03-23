import asyncio

from app.observability.health import get_health
from app.observability.logger import get_logger
from app.session.redis_store import redis_client
from app.config import settings

logger = get_logger("watchdog")

ALERT_COOLDOWN = 3600
MAX_ALERTS_PER_HOUR = 10
_alert_counts: dict = {}


async def maybe_alert(service_name: str, status: dict):
    cooldown_key = f"rate_limit_notified:{service_name}"
    if redis_client.get(cooldown_key):
        return

    redis_client.setex(cooldown_key, ALERT_COOLDOWN, "1")
    logger.warning(
        "service_unhealthy_alert",
        service=service_name,
        error=status.get("error"),
    )

    if settings.WATCHDOG_PHONE:
        from app.whatsapp.sender import send_message
        await send_message(
            settings.WATCHDOG_PHONE,
            f"[Life Review OS] Service *{service_name}* is unhealthy: {status.get('error', 'unknown error')}",
        )


async def maybe_recover(service_name: str):
    cooldown_key = f"rate_limit_notified:{service_name}"
    if redis_client.get(cooldown_key):
        redis_client.delete(cooldown_key)
        logger.info("service_recovered", service=service_name)

        if settings.WATCHDOG_PHONE:
            from app.whatsapp.sender import send_message
            await send_message(
                settings.WATCHDOG_PHONE,
                f"[Life Review OS] Service *{service_name}* has recovered.",
            )


async def watchdog_loop():
    while True:
        await asyncio.sleep(60)
        try:
            health = await get_health()
            for service_name, status in health["services"].items():
                if status["status"] == "unhealthy":
                    await maybe_alert(service_name, status)
                elif status["status"] == "healthy":
                    await maybe_recover(service_name)
        except Exception as e:
            logger.error("watchdog_error", error=str(e))
