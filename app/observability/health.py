import asyncio
from datetime import datetime

import httpx
import redis as redis_lib

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


async def check_redis() -> dict:
    try:
        r = redis_lib.from_url(settings.REDIS_URL, decode_responses=True)
        r.ping()
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def check_evolution_api() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                f"{settings.EVOLUTION_API_URL}/",
                headers={"apikey": settings.EVOLUTION_API_KEY},
            )
            if response.status_code < 500:
                return {"status": "healthy"}
            return {"status": "unhealthy", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def check_openai() -> dict:
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        await client.models.list()
        return {"status": "healthy"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def check_notion_mcp() -> dict:
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(f"{settings.MCP_URL}/health")
            if response.status_code == 200:
                return {"status": "healthy"}
            return {"status": "unhealthy", "error": f"HTTP {response.status_code}"}
    except Exception as e:
        return {"status": "unhealthy", "error": str(e)}


async def get_health() -> dict:
    checks = await asyncio.gather(
        check_redis(),
        check_evolution_api(),
        check_openai(),
        check_notion_mcp(),
        return_exceptions=True,
    )

    services = {}
    all_healthy = True

    for name, result in zip(
        ["redis", "evolution_api", "openai", "notion_mcp"], checks
    ):
        if isinstance(result, Exception):
            services[name] = {"status": "unhealthy", "error": str(result)}
            all_healthy = False
        else:
            services[name] = result
            if result.get("status") != "healthy":
                all_healthy = False

    return {
        "status": "healthy" if all_healthy else "unhealthy",
        "timestamp": datetime.utcnow().isoformat(),
        "services": services,
    }
