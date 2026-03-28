import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport
from fakeredis import FakeRedis


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_redis():
    """In-memory Redis — no external server needed."""
    r = FakeRedis(decode_responses=True)
    yield r
    r.flushall()


# ---------------------------------------------------------------------------
# OpenAI
# ---------------------------------------------------------------------------

def _make_openai_response(content: str, finish_reason: str = "stop"):
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = None
    choice = MagicMock()
    choice.message = msg
    choice.finish_reason = finish_reason
    resp = MagicMock()
    resp.choices = [choice]
    return resp


@pytest.fixture
def mock_openai_response():
    """Factory: create a fake OpenAI response with given content."""
    return _make_openai_response


@pytest.fixture
def mock_openai():
    """Patches AsyncOpenAI everywhere — returns a plain 'mocked response'."""
    with patch("openai.AsyncOpenAI") as mock_cls:
        instance = AsyncMock()
        mock_cls.return_value = instance
        instance.chat.completions.create = AsyncMock(
            return_value=_make_openai_response("mocked response")
        )
        instance.audio.transcriptions.create = AsyncMock(
            return_value=MagicMock(text="transcribed text")
        )
        yield instance


# ---------------------------------------------------------------------------
# MCP / Notion
# ---------------------------------------------------------------------------

def _mcp_ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


@pytest.fixture
def mock_mcp():
    """Patches mcp_client.call_tool — returns empty results by default."""
    with patch("app.notion.mcp_client.mcp_client") as mock:
        mock.call_tool = AsyncMock(return_value=_mcp_ok({"results": []}))
        yield mock


@pytest.fixture
def mcp_ok():
    """Helper to build a valid MCP response dict."""
    return _mcp_ok


# ---------------------------------------------------------------------------
# WhatsApp sender
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_sender():
    """Patches send_message so no real HTTP calls are made."""
    with patch("app.whatsapp.sender.send_message", new_callable=AsyncMock) as mock:
        yield mock


# ---------------------------------------------------------------------------
# FastAPI client
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client():
    """AsyncClient for the FastAPI app — no real I/O."""
    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# Common payloads
# ---------------------------------------------------------------------------

@pytest.fixture
def whatsapp_text_payload():
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"fromMe": False, "id": "MSGID001", "remoteJid": "5511999999999@s.whatsapp.net"},
            "message": {"conversation": "Worked on project today"},
        },
    }


@pytest.fixture
def whatsapp_audio_payload():
    return {
        "event": "messages.upsert",
        "data": {
            "key": {"fromMe": False, "id": "AUDIOID001", "remoteJid": "5511999999999@s.whatsapp.net"},
            "message": {
                "audioMessage": {
                    "url": "https://mmg.whatsapp.net/fake.enc",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 5,
                }
            },
        },
    }
