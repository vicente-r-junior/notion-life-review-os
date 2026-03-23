import httpx

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def send_message(phone: str, text: str):
    if not settings.EVOLUTION_API_URL or not settings.EVOLUTION_INSTANCE:
        logger.warning("evolution_api_not_configured")
        return

    url = f"{settings.EVOLUTION_API_URL}/message/sendText/{settings.EVOLUTION_INSTANCE}"
    payload = {
        "number": phone,
        "text": text,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"apikey": settings.EVOLUTION_API_KEY},
            )
            response.raise_for_status()
            logger.info("message_sent", phone=phone[:5] + "****")
    except Exception as e:
        logger.error("message_send_failed", error=str(e))
