"""
Unit tests for app/whatsapp/handler.py

Covers:
- extract_text / extract_audio / extract_phone
- detect_confirmation_intent (fast path + LLM path)
- _has_options
- _advance_column_flow state transitions
- handle_webhook routing (text, audio, fromMe, duplicate, paused)
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# extract_text / extract_audio / extract_phone
# ---------------------------------------------------------------------------

class TestExtractors:
    def test_extract_text_conversation(self):
        from app.whatsapp.handler import extract_text
        payload = {"data": {"message": {"conversation": "hello"}}}
        assert extract_text(payload) == "hello"

    def test_extract_text_extended(self):
        from app.whatsapp.handler import extract_text
        payload = {"data": {"message": {"extendedTextMessage": {"text": "hi there"}}}}
        assert extract_text(payload) == "hi there"

    def test_extract_text_audio_returns_none(self):
        from app.whatsapp.handler import extract_text
        payload = {"data": {"message": {"audioMessage": {"url": "x"}}}}
        assert extract_text(payload) is None

    def test_extract_text_empty_returns_none(self):
        from app.whatsapp.handler import extract_text
        assert extract_text({}) is None

    def test_extract_audio_returns_message_id(self):
        from app.whatsapp.handler import extract_audio
        payload = {
            "data": {
                "key": {"id": "MSG123", "remoteJid": "55...@s.whatsapp.net"},
                "message": {
                    "audioMessage": {
                        "url": "https://mmg.whatsapp.net/fake.enc",
                        "mimetype": "audio/ogg",
                        "seconds": 10,
                    }
                },
            }
        }
        result = extract_audio(payload)
        assert result["message_id"] == "MSG123"
        assert result["seconds"] == 10
        assert "url" not in result  # url should not be returned

    def test_extract_audio_no_audio_returns_none(self):
        from app.whatsapp.handler import extract_audio
        payload = {"data": {"message": {"conversation": "text"}}}
        assert extract_audio(payload) is None

    def test_extract_phone_strips_jid(self):
        from app.whatsapp.handler import extract_phone
        payload = {"data": {"key": {"remoteJid": "5511999999999@s.whatsapp.net"}}}
        assert extract_phone(payload) == "5511999999999"

    def test_extract_phone_group(self):
        from app.whatsapp.handler import extract_phone
        payload = {"data": {"key": {"remoteJid": "123456789@g.us"}}}
        assert extract_phone(payload) == "123456789"


# ---------------------------------------------------------------------------
# detect_confirmation_intent
# ---------------------------------------------------------------------------

class TestConfirmationIntent:
    @pytest.mark.asyncio
    async def test_fast_path_confirm(self):
        from app.whatsapp.handler import detect_confirmation_intent
        for word in ["yes", "y", "ok", "sim", "👍", "claro"]:
            assert await detect_confirmation_intent(word) == "confirm", f"Failed for: {word}"

    @pytest.mark.asyncio
    async def test_fast_path_cancel(self):
        from app.whatsapp.handler import detect_confirmation_intent
        for word in ["no", "cancel", "nope", "nao", "n"]:
            assert await detect_confirmation_intent(word) == "cancel", f"Failed for: {word}"

    @pytest.mark.asyncio
    async def test_fast_path_strips_punctuation(self):
        from app.whatsapp.handler import detect_confirmation_intent
        assert await detect_confirmation_intent("yes.") == "confirm"
        assert await detect_confirmation_intent("no!") == "cancel"

    @pytest.mark.asyncio
    async def test_llm_fallback_confirm(self):
        from app.whatsapp.handler import detect_confirmation_intent
        with patch("app.whatsapp.handler.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            mock_cls.return_value = instance
            instance.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content="confirm"))]
                )
            )
            result = await detect_confirmation_intent("sounds great to me")
        assert result == "confirm"

    @pytest.mark.asyncio
    async def test_llm_fallback_continue_on_ambiguous(self):
        from app.whatsapp.handler import detect_confirmation_intent
        with patch("app.whatsapp.handler.AsyncOpenAI") as mock_cls:
            instance = AsyncMock()
            mock_cls.return_value = instance
            instance.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content="continue"))]
                )
            )
            result = await detect_confirmation_intent("what about the project name?")
        assert result == "continue"


# ---------------------------------------------------------------------------
# _has_options
# ---------------------------------------------------------------------------

class TestHasOptions:
    def test_no_options(self):
        from app.whatsapp.handler import _has_options
        payload = {
            "column_type": {"type": "select", "select": {"options": []}},
            "column_type_num": "3",
        }
        assert _has_options(payload) is False

    def test_with_options(self):
        from app.whatsapp.handler import _has_options
        payload = {
            "column_type": {"type": "select", "select": {"options": [{"name": "A"}]}},
            "column_type_num": "3",
        }
        assert _has_options(payload) is True

    def test_multi_select_with_options(self):
        from app.whatsapp.handler import _has_options
        payload = {
            "column_type": {"type": "multi_select", "multi_select": {"options": [{"name": "X"}]}},
            "column_type_num": "4",
        }
        assert _has_options(payload) is True

    def test_non_select_returns_false(self):
        from app.whatsapp.handler import _has_options
        payload = {"column_type": {"type": "rich_text", "rich_text": {}}, "column_type_num": "1"}
        assert _has_options(payload) is False


# ---------------------------------------------------------------------------
# _advance_column_flow
# ---------------------------------------------------------------------------

class TestAdvanceColumnFlow:
    @pytest.mark.asyncio
    async def test_asks_for_db_when_missing(self, fake_redis):
        from app.whatsapp.handler import _advance_column_flow
        session = {"state": "waiting_column_db", "payload": {"column_name": "Who"}}
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await _advance_column_flow("5511", session)
        mock_send.assert_called_once()
        assert "database" in mock_send.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_asks_for_type_when_missing(self, fake_redis):
        from app.whatsapp.handler import _advance_column_flow
        session = {
            "state": "waiting_column_type",
            "payload": {"column_name": "Who", "chosen_db": "tasks"},
        }
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await _advance_column_flow("5511", session)
        assert "type" in mock_send.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_asks_for_options_when_select_empty(self, fake_redis):
        from app.whatsapp.handler import _advance_column_flow, COLUMN_TYPE_MAP
        session = {
            "state": "waiting_column_options",
            "payload": {
                "column_name": "Priority",
                "chosen_db": "tasks",
                "column_type": COLUMN_TYPE_MAP["3"],
                "column_type_num": "3",
                "required": True,
            },
        }
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await _advance_column_flow("5511", session)
        assert "options" in mock_send.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_skips_required_when_already_set(self, fake_redis):
        from app.whatsapp.handler import _advance_column_flow, COLUMN_TYPE_MAP
        payload = {
            "column_name": "Who",
            "chosen_db": "tasks",
            "column_type": {"type": "rich_text", "rich_text": {}},
            "column_type_num": "1",
            "required": True,
        }
        session = {"state": "waiting_column_confirm", "payload": payload}
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await _advance_column_flow("5511", session)
        # Should jump to confirmation, not ask "required?"
        msg = mock_send.call_args[0][1]
        assert "confirm" in msg.lower()
        assert "required field" not in msg.lower()

    @pytest.mark.asyncio
    async def test_promotes_required_prefill(self, fake_redis):
        from app.whatsapp.handler import _advance_column_flow
        payload = {
            "column_name": "Who",
            "chosen_db": "tasks",
            "column_type": {"type": "rich_text", "rich_text": {}},
            "column_type_num": "1",
            "required_prefill": True,  # set early, not yet promoted
        }
        session = {"state": "waiting_column_confirm", "payload": payload}
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await _advance_column_flow("5511", session)
        # required_prefill should be promoted → skip required question
        assert "confirm" in mock_send.call_args[0][1].lower()
        assert "required_prefill" not in session["payload"]
        assert session["payload"]["required"] is True


# ---------------------------------------------------------------------------
# handle_webhook routing
# ---------------------------------------------------------------------------

class TestHandleWebhook:
    @pytest.mark.asyncio
    async def test_ignores_from_me(self, fake_redis, whatsapp_text_payload):
        from app.whatsapp.handler import handle_webhook
        whatsapp_text_payload["data"]["key"]["fromMe"] = True
        with patch("app.whatsapp.handler.redis_client", fake_redis):
            # Should return without calling process_log
            with patch("app.router.message_router.process_log", new_callable=AsyncMock) as mock_pl:
                await handle_webhook(whatsapp_text_payload)
                mock_pl.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_duplicate_message(self, fake_redis, whatsapp_text_payload):
        from app.whatsapp.handler import handle_webhook
        # Mark as already processed
        fake_redis.setex("processed:MSGID001", 86400, "1")
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await handle_webhook(whatsapp_text_payload)
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_ignores_paused_user(self, fake_redis, whatsapp_text_payload):
        from app.whatsapp.handler import handle_webhook
        fake_redis.setex("onboarded:5511999999999", 86400, "1")
        fake_redis.setex("paused:5511999999999", 86400, "1")
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await handle_webhook(whatsapp_text_payload)
            mock_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_audio_calls_transcribe(self, fake_redis, whatsapp_audio_payload):
        from app.whatsapp.handler import handle_webhook
        fake_redis.setex("onboarded:5511999999999", 86400, "1")
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock),
            patch("app.whatsapp.handler.transcribe", new_callable=AsyncMock, return_value="test transcription") as mock_tr,
            patch("app.router.message_router.process_log", new_callable=AsyncMock),
        ):
            await handle_webhook(whatsapp_audio_payload)
            mock_tr.assert_called_once_with("AUDIOID001")

    @pytest.mark.asyncio
    async def test_audio_transcription_failure_sends_error(self, fake_redis, whatsapp_audio_payload):
        from app.whatsapp.handler import handle_webhook
        fake_redis.setex("onboarded:5511999999999", 86400, "1")
        with (
            patch("app.whatsapp.handler.redis_client", fake_redis),
            patch("app.whatsapp.handler.send_message", new_callable=AsyncMock) as mock_send,
            patch("app.whatsapp.handler.transcribe", new_callable=AsyncMock, side_effect=Exception("whisper error")),
        ):
            await handle_webhook(whatsapp_audio_payload)
            # Should send error message to user
            assert mock_send.called
            assert "audio" in mock_send.call_args[0][1].lower() or "catch" in mock_send.call_args[0][1].lower()
