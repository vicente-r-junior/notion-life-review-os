import asyncio
import json
import time

from app.observability.logger import get_logger, mask_phone
from app.session.redis_store import redis_client

logger = get_logger(__name__)


async def aggregation_worker():
    while True:
        await asyncio.sleep(3)
        try:
            keys = redis_client.keys("aggregating:*")
            for key in keys:
                raw = redis_client.get(key)
                if not raw:
                    continue

                session = json.loads(raw)
                now = time.time()
                silence = now - session["last_message_at"]
                total = now - session["started_at"]

                silence_threshold = session.get("silence_seconds", 15)
                window_threshold = session.get("window_seconds", 45)

                should_process = silence >= silence_threshold or total >= window_threshold

                if should_process:
                    redis_client.delete(key)
                    full_text = " ".join(session["messages"])
                    phone = session["phone"]

                    logger.info(
                        "aggregation_complete",
                        phone=mask_phone(phone),
                        messages=len(session["messages"]),
                    )

                    from app.whatsapp.sender import send_message
                    from app.router.message_router import process_log

                    await send_message(phone, "Got everything! Processing now...")
                    await process_log(phone, full_text)

        except Exception as e:
            logger.error("aggregation_worker_error", error=str(e))
