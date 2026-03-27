import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from app.observability.logger import get_logger, setup_logging
from app.observability.health import get_health
from app.whatsapp.handler import handle_webhook
from app.schema.schema_manager import bootstrap_schemas
from app.scheduler.weekly_cron import create_scheduler
from app.scheduler.aggregation_worker import aggregation_worker

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("app_starting")

    # Bootstrap Notion schemas
    try:
        await bootstrap_schemas()
        logger.info("schemas_bootstrapped")
    except Exception as e:
        logger.error("schema_bootstrap_failed", error=str(e))

    # Start aggregation worker
    agg_task = asyncio.create_task(aggregation_worker())

    # Start weekly scheduler
    scheduler = create_scheduler()
    scheduler.start()
    logger.info("scheduler_started")

    yield

    # Cleanup
    agg_task.cancel()
    scheduler.shutdown()
    logger.info("app_stopped")


app = FastAPI(title="Notion Life Review OS", lifespan=lifespan)


@app.post("/webhook")
async def webhook(request: Request):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    asyncio.create_task(handle_webhook(payload))
    return {"status": "accepted"}


@app.get("/health")
async def health():
    result = await get_health()
    status_code = 200 if result["status"] == "healthy" else 503
    return JSONResponse(content=result, status_code=status_code)


@app.get("/")
async def root():
    return {"name": "Notion Life Review OS", "version": "1.0.0"}
