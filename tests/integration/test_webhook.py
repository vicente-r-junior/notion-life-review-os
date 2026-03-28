"""
Integration tests for the /webhook endpoint.

Uses the real FastAPI app via httpx AsyncClient + FakeRedis.
No real OpenAI / Notion / Evolution calls are made.

Covers:
- 200 OK on valid message
- fromMe messages are silently ignored
- Duplicate messages (same ID) are ignored
- Paused users are ignored
- Onboarding flow starts for new users
- Audio path invokes transcribe

Note: /webhook returns 200 immediately and dispatches handle_webhook via
asyncio.create_task. Tests must `await asyncio.sleep(0)` to let the task run.
"""
import asyncio
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport
from fakeredis import FakeRedis


# ---------------------------------------------------------------------------
# App-level fixture with all heavy dependencies patched
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def app_client(fake_redis):
    """
    AsyncClient with the FastAPI app.
    Patches redis_client globally so all modules share the same FakeRedis.
    Also patches send_message to avoid real HTTP calls.
    """
    from app.main import app

    with (
        patch("app.whatsapp.handler.redis_client", fake_redis),
        patch("app.router.message_router.redis_client", fake_redis),
        patch("app.whatsapp.handler.send_message", new_callable=AsyncMock),
        patch("app.router.message_router.sender.send_message", new_callable=AsyncMock),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac, fake_redis


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _text_payload(msg_id="MSGID001", from_me=False, text="Worked on project today"):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {
                "fromMe": from_me,
                "id": msg_id,
                "remoteJid": "5511999999999@s.whatsapp.net",
            },
            "message": {"conversation": text},
        },
    }


def _audio_payload(msg_id="AUDIOID001"):
    return {
        "event": "messages.upsert",
        "data": {
            "key": {
                "fromMe": False,
                "id": msg_id,
                "remoteJid": "5511999999999@s.whatsapp.net",
            },
            "message": {
                "audioMessage": {
                    "url": "https://mmg.whatsapp.net/fake.enc",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 5,
                }
            },
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestWebhookEndpoint:
    @pytest.mark.asyncio
    async def test_returns_200_ok(self, app_client):
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")

        with patch("app.router.message_router.process_log", new_callable=AsyncMock):
            resp = await client.post("/webhook", json=_text_payload())
            await asyncio.sleep(0)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_from_me_ignored(self, app_client):
        client, _ = app_client

        with patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl:
            resp = await client.post("/webhook", json=_text_payload(from_me=True))
            await asyncio.sleep(0)

        assert resp.status_code == 200
        mock_pl.assert_not_called()

    @pytest.mark.asyncio
    async def test_duplicate_message_ignored(self, app_client):
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")
        # Mark message as already processed
        redis.setex("processed:MSGID001", 86400, "1")

        with patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl:
            resp = await client.post("/webhook", json=_text_payload())
            await asyncio.sleep(0)

        assert resp.status_code == 200
        mock_pl.assert_not_called()

    @pytest.mark.asyncio
    async def test_paused_user_ignored(self, app_client):
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")
        redis.setex("paused:5511999999999", 86400, "1")

        with patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl:
            resp = await client.post("/webhook", json=_text_payload())
            await asyncio.sleep(0)

        assert resp.status_code == 200
        mock_pl.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_user_triggers_onboarding(self, app_client):
        client, redis = app_client
        # No "onboarded" key for this phone

        with (
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
            patch("app.router.message_router.process_log", new_callable=AsyncMock),
        ):
            resp = await client.post("/webhook", json=_text_payload())
            await asyncio.sleep(0)

        assert resp.status_code == 200
        # Should have sent an onboarding message
        mock_send.assert_called()
        sent_text = mock_send.call_args[0][1].lower()
        assert any(word in sent_text for word in ["welcome", "hello", "hi", "hey", "started", "ready", "notion"])

    @pytest.mark.asyncio
    async def test_audio_calls_transcribe_and_process_log(self, app_client):
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")

        with (
            patch("app.whatsapp.handler.transcribe", new_callable=AsyncMock, return_value="I finished the report") as mock_tr,
            patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl,
        ):
            resp = await client.post("/webhook", json=_audio_payload())
            await asyncio.sleep(0)

        assert resp.status_code == 200
        mock_tr.assert_called_once_with("AUDIOID001")
        mock_pl.assert_called_once_with("5511999999999", "[Voice message]: I finished the report")

    @pytest.mark.asyncio
    async def test_message_marked_as_processed_after_handling(self, app_client):
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")

        with patch("app.router.message_router.process_log", new_callable=AsyncMock):
            await client.post("/webhook", json=_text_payload(msg_id="UNIQUE_MSG"))
            await asyncio.sleep(0)

        assert redis.get("processed:UNIQUE_MSG") is not None

    @pytest.mark.asyncio
    async def test_second_identical_message_ignored(self, app_client):
        """Idempotency: posting the same payload twice only processes once."""
        client, redis = app_client
        redis.setex("onboarded:5511999999999", 86400, "1")

        with patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl:
            await client.post("/webhook", json=_text_payload(msg_id="IDEM001"))
            await asyncio.sleep(0)
            await client.post("/webhook", json=_text_payload(msg_id="IDEM001"))
            await asyncio.sleep(0)

        assert mock_pl.call_count == 1
