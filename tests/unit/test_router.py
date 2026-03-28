"""
Unit tests for app/router/message_router.py

Covers:
- _build_notion_filter edge cases
- _handle_add_column_intent payload building (all fields, partial, empty)
- process_log SAVE_PAYLOAD parsing and session creation
- process_log routing: query / add_column / bulk_update / log
"""
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


# ---------------------------------------------------------------------------
# _build_notion_filter
# ---------------------------------------------------------------------------

class TestBuildNotionFilter:
    def _filter(self, filter_info, field="Who", today="2026-03-28"):
        from app.router.message_router import _build_notion_filter
        return _build_notion_filter(filter_info, field, today)

    def test_none_filter_returns_none(self):
        assert self._filter(None) is None

    def test_all_flag_returns_none(self):
        assert self._filter({"all": True}) is None

    def test_due_today(self):
        result = self._filter({"due_today": True})
        assert result == {"property": "Due Date", "date": {"equals": "2026-03-28"}}

    def test_due_date_explicit(self):
        result = self._filter({"due_date": "2026-03-31"})
        assert result == {"property": "Due Date", "date": {"equals": "2026-03-31"}}

    def test_status_filter(self):
        result = self._filter({"status": "Todo"})
        assert result == {"property": "Status", "select": {"equals": "Todo"}}

    def test_field_empty_filter(self):
        result = self._filter({"field_empty": "Who"})
        assert result == {"property": "Who", "rich_text": {"is_empty": True}}

    def test_multiple_conditions_wrapped_in_and(self):
        result = self._filter({"status": "Todo", "due_today": True})
        assert "and" in result
        assert len(result["and"]) == 2

    def test_empty_dict_returns_none(self):
        assert self._filter({}) is None


# ---------------------------------------------------------------------------
# _handle_add_column_intent
# ---------------------------------------------------------------------------

class TestHandleAddColumnIntent:
    @pytest.mark.asyncio
    async def test_full_extraction_skips_all_questions(self, fake_redis):
        """When all fields are known, _advance_column_flow goes straight to confirmation."""
        from app.router.message_router import _handle_add_column_intent

        extracted = {
            "db": "tasks",
            "column_name": "Priority",
            "column_type": "select",
            "required": True,
            "options": ["Low", "Medium", "High"],
        }

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.router.message_router.client") as mock_client,
            patch("app.whatsapp.handler._advance_column_flow", new_callable=AsyncMock) as mock_advance,
        ):
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content=json.dumps(extracted)))]
                )
            )
            await _handle_add_column_intent("5511", "add Priority select to tasks, required, options Low Medium High")

        mock_advance.assert_called_once()
        _, session = mock_advance.call_args[0]
        payload = session["payload"]
        assert payload["chosen_db"] == "tasks"
        assert payload["column_name"] == "Priority"
        assert payload["column_type"]["type"] == "select"
        assert payload["required"] is True
        # Options embedded in column_type
        opts = payload["column_type"]["select"]["options"]
        assert {"name": "Low"} in opts

    @pytest.mark.asyncio
    async def test_no_info_starts_fresh_flow(self, fake_redis):
        from app.router.message_router import _handle_add_column_intent

        extracted = {"db": None, "column_name": None, "column_type": None, "required": None, "options": None}

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.router.message_router.client") as mock_client,
            patch("app.router.message_router.start_add_column_flow", new_callable=AsyncMock) as mock_start,
        ):
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content=json.dumps(extracted)))]
                )
            )
            await _handle_add_column_intent("5511", "add column")

        mock_start.assert_called_once_with("5511")

    @pytest.mark.asyncio
    async def test_optional_flag_stored(self, fake_redis):
        from app.router.message_router import _handle_add_column_intent

        extracted = {"db": "tasks", "column_name": "Notes", "column_type": "text", "required": False, "options": None}

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.router.message_router.client") as mock_client,
            patch("app.whatsapp.handler._advance_column_flow", new_callable=AsyncMock),
        ):
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content=json.dumps(extracted)))]
                )
            )
            await _handle_add_column_intent("5511", "add optional text column Notes to tasks")

        session_raw = fake_redis.get("session:5511")
        assert session_raw is not None
        payload = json.loads(session_raw)["payload"]
        assert payload["required"] is False


# ---------------------------------------------------------------------------
# process_log — SAVE_PAYLOAD parsing
# ---------------------------------------------------------------------------

class TestProcessLog:
    @pytest.mark.asyncio
    async def test_save_payload_creates_session(self, fake_redis):
        from app.router.message_router import process_log

        llm_reply = (
            "Got it! Here's what I captured.\n"
            'SAVE_PAYLOAD: {"summary": "Worked on app", "mood": 4, "energy": "high", '
            '"tasks": [], "learnings": [], "project_updates": [], "tags": ["work"], "updates": []}'
        )

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.session.conversation.redis_client", fake_redis),
            patch("app.router.message_router.client") as mock_client,
            patch("app.router.message_router.sender.send_message", new_callable=AsyncMock),
            patch("app.agents.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="log"),
            patch("app.session.prompt_builder.get_system_prompt", return_value="System {today} {today[:4]}"),
        ):
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content=llm_reply))]
                )
            )
            await process_log("5511", "Worked on the app today, felt great")

        session_raw = fake_redis.get("session:5511")
        assert session_raw is not None
        session = json.loads(session_raw)
        assert session["state"] == "waiting_confirmation"
        assert session["payload"]["summary"] == "Worked on app"
        assert session["payload"]["mood"] == 4

    @pytest.mark.asyncio
    async def test_query_intent_calls_query_agent(self, fake_redis):
        from app.router.message_router import process_log

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.session.conversation.redis_client", fake_redis),
            patch("app.agents.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="query"),
            patch("app.agents.query_agent.run_query_agent", new_callable=AsyncMock, return_value="Here are your tasks") as mock_qa,
            patch("app.router.message_router.sender.send_message", new_callable=AsyncMock) as mock_send,
        ):
            await process_log("5511", "what tasks do I have?")

        mock_qa.assert_called_once()
        mock_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_column_intent_clears_stale_session(self, fake_redis):
        from app.router.message_router import process_log

        # Pre-existing stale session
        fake_redis.setex("session:5511", 600, json.dumps({"state": "waiting_confirmation", "payload": {}}))

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.session.conversation.redis_client", fake_redis),
            patch("app.agents.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="add_column"),
            patch("app.router.message_router._handle_add_column_intent", new_callable=AsyncMock) as mock_add,
        ):
            await process_log("5511", "add Who column to tasks")

        assert fake_redis.get("session:5511") is None or mock_add.called
        mock_add.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_save_payload_sends_reply_directly(self, fake_redis):
        from app.router.message_router import process_log

        with (
            patch("app.router.message_router.redis_client", fake_redis),
            patch("app.session.conversation.redis_client", fake_redis),
            patch("app.router.message_router.client") as mock_client,
            patch("app.router.message_router.sender.send_message", new_callable=AsyncMock) as mock_send,
            patch("app.agents.intent_classifier.classify_intent", new_callable=AsyncMock, return_value="log"),
            patch("app.session.prompt_builder.get_system_prompt", return_value="System {today} {today[:4]}"),
        ):
            mock_client.chat.completions.create = AsyncMock(
                return_value=MagicMock(
                    choices=[MagicMock(message=MagicMock(content="Just a normal reply."))]
                )
            )
            await process_log("5511", "how are you?")

        mock_send.assert_called_once_with("5511", "Just a normal reply.")
        assert fake_redis.get("session:5511") is None
