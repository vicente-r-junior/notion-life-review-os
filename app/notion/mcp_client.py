import asyncio
import uuid

import httpx

from app.config import settings
from app.observability.logger import get_logger

logger = get_logger(__name__)

_SSE_ACCEPT = "application/json, text/event-stream"


def _parse_sse_data(body: str) -> dict:
    """Extract the JSON payload from an SSE response body.

    The server emits lines like:
        event: message
        data: {"result": ..., "jsonrpc": "2.0", "id": 1}
    """
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            return __import__("json").loads(line[len("data:"):].strip())
    raise ValueError(f"No SSE data line found in response body: {body[:200]}")


class MCPClient:
    def __init__(self):
        self.base_url = settings.MCP_URL
        self.auth_token = settings.MCP_AUTH_TOKEN
        # session_id is assigned by the server after initialize; None until then.
        self._session_id: str | None = None
        self.initialized = False
        self._lock = asyncio.Lock()

    def _headers(self, include_session: bool = True) -> dict:
        h = {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "Accept": _SSE_ACCEPT,
        }
        if include_session and self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    async def _rpc(self, method: str, params: dict = None, rpc_id: int = 1) -> dict:
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
                headers=self._headers(include_session=True),
            )
            response.raise_for_status()
            data = _parse_sse_data(response.text)
            if "error" in data:
                raise Exception(f"MCP error: {data['error']}")
            return data.get("result")

    async def _notify(self, method: str, params: dict = None):
        """Send a JSON-RPC notification (no id field, no response expected)."""
        payload: dict = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{self.base_url}/mcp",
                json=payload,
                headers=self._headers(include_session=True),
            )
            if response.status_code >= 400:
                raise Exception(
                    f"MCP notification '{method}' failed with HTTP "
                    f"{response.status_code}: {response.text}"
                )

    async def initialize(self):
        async with self._lock:
            if self.initialized:
                return

            # The initialize call must NOT carry an existing session ID.
            # The server creates the session and returns its ID in the response
            # header `mcp-session-id`.
            payload = {
                "jsonrpc": "2.0",
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "life-review-os", "version": "1.0.0"},
                },
                "id": 1,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    f"{self.base_url}/mcp",
                    json=payload,
                    headers=self._headers(include_session=False),
                )
                response.raise_for_status()

                # Capture the server-assigned session ID.
                server_session_id = response.headers.get("mcp-session-id")
                if not server_session_id:
                    raise Exception(
                        "MCP server did not return an mcp-session-id header "
                        "in the initialize response."
                    )
                self._session_id = server_session_id

                data = _parse_sse_data(response.text)
                if "error" in data:
                    raise Exception(f"MCP initialize error: {data['error']}")

            # Confirm initialization — notification uses the server session ID.
            await self._notify("notifications/initialized")

            self.initialized = True
            logger.info("mcp_initialized", session_id=self._session_id)

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
