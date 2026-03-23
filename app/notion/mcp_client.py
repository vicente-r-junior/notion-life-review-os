import asyncio
import uuid

import httpx

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class MCPClient:
    def __init__(self):
        self.base_url = settings.MCP_URL
        self.auth_token = settings.MCP_AUTH_TOKEN
        self.session_id = str(uuid.uuid4())
        self.initialized = False
        self._lock = asyncio.Lock()

    @property
    def headers(self):
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "mcp-session-id": self.session_id,
        }

    async def _rpc(self, method: str, params: dict = None, rpc_id: int = 1):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": rpc_id,
        }
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=self.headers,
            )
            response.raise_for_status()
            data = response.json()
            if "error" in data:
                raise Exception(f"MCP error: {data['error']}")
            return data.get("result")

    async def _notify(self, method: str, params: dict = None):
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=self.headers,
            )
            # Notifications may return 200, 202, or 204 — all are acceptable.
            # Raise only on server errors so a bad response doesn't leave the
            # client silently stuck in an un-initialized state.
            if response.status_code >= 400:
                raise Exception(
                    f"MCP notification '{method}' failed with HTTP {response.status_code}: {response.text}"
                )

    async def initialize(self):
        async with self._lock:
            if self.initialized:
                return
            await self._rpc(
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "life-review-os", "version": "1.0.0"},
                },
                rpc_id=1,
            )
            # notifications/initialized is a JSON-RPC notification (no id, no params).
            await self._notify("notifications/initialized")
            self.initialized = True
            logger.info("mcp_initialized", session_id=self.session_id)

    async def call_tool(
        self, tool_name: str, arguments: dict, max_retries: int = 3
    ) -> dict:
        await self.initialize()

        for attempt in range(max_retries):
            try:
                result = await self._rpc(
                    "tools/call",
                    {"name": tool_name, "arguments": arguments},
                )
                return result
            except Exception as e:
                error_str = str(e).lower()
                if "rate_limit" in error_str or "429" in error_str:
                    wait = 2**attempt
                    logger.warning(
                        "mcp_rate_limited",
                        tool=tool_name,
                        attempt=attempt,
                        wait=wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise

        raise Exception(f"MCP tool {tool_name} failed after {max_retries} retries")

    async def list_tools(self) -> list:
        await self.initialize()
        result = await self._rpc("tools/list")
        return result.get("tools", [])


mcp_client = MCPClient()
