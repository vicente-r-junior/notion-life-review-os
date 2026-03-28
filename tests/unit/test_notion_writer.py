"""
Unit tests for app/agents/notion_writer.py

Covers:
- _format_property for each field type
- _similar fuzzy matching
- run_notion_writer: daily log creation, tasks with custom fields,
  project deduplication, learnings, updates with page_id
"""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call


# ---------------------------------------------------------------------------
# _format_property
# ---------------------------------------------------------------------------

class TestFormatProperty:
    def _fp(self, field_type, value):
        from app.agents.notion_writer import _format_property
        return _format_property(field_type, value)

    def test_select(self):
        assert self._fp("select", "High") == {"select": {"name": "High"}}

    def test_multi_select_comma_separated(self):
        result = self._fp("multi_select", "work, personal")
        assert result == {"multi_select": [{"name": "work"}, {"name": "personal"}]}

    def test_date(self):
        assert self._fp("date", "2026-03-28") == {"date": {"start": "2026-03-28"}}

    def test_number_valid(self):
        assert self._fp("number", "7") == {"number": 7.0}

    def test_number_invalid_defaults_to_zero(self):
        assert self._fp("number", "abc") == {"number": 0}

    def test_checkbox_true_values(self):
        for v in ["true", "yes", "1", "done"]:
            assert self._fp("checkbox", v) == {"checkbox": True}, f"Failed for: {v}"

    def test_checkbox_false(self):
        assert self._fp("checkbox", "false") == {"checkbox": False}

    def test_url(self):
        assert self._fp("url", "https://example.com") == {"url": "https://example.com"}

    def test_email(self):
        assert self._fp("email", "a@b.com") == {"email": "a@b.com"}

    def test_title(self):
        assert self._fp("title", "My Title") == {"title": [{"text": {"content": "My Title"}}]}

    def test_rich_text_default(self):
        result = self._fp("rich_text", "some text")
        assert result == {"rich_text": [{"text": {"content": "some text"}}]}

    def test_unknown_type_falls_back_to_rich_text(self):
        result = self._fp("formula", "computed")
        assert "rich_text" in result


# ---------------------------------------------------------------------------
# _similar
# ---------------------------------------------------------------------------

class TestSimilar:
    def _s(self, a, b):
        from app.agents.notion_writer import _similar
        return _similar(a, b)

    def test_exact_match(self):
        assert self._s("Project Alpha", "Project Alpha") is True

    def test_case_insensitive(self):
        assert self._s("project alpha", "Project Alpha") is True

    def test_slight_typo(self):
        assert self._s("Proect Alpha", "Project Alpha") is True

    def test_very_different_returns_false(self):
        assert self._s("Alpha", "Completely Different") is False


# ---------------------------------------------------------------------------
# run_notion_writer
# ---------------------------------------------------------------------------

def _mcp_ok(payload: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _make_mcp_mock():
    mock = MagicMock()
    mock.call_tool = AsyncMock(return_value=_mcp_ok({"results": []}))
    return mock


class TestRunNotionWriter:
    @pytest.mark.asyncio
    async def test_daily_log_always_created(self):
        from app.agents.notion_writer import run_notion_writer

        payload = {
            "summary": "Good day",
            "mood": 4,
            "energy": "high",
            "tags": ["work"],
            "tasks": [],
            "learnings": [],
            "project_updates": [],
            "updates": [],
        }

        mock_mcp = _make_mcp_mock()
        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value={"fields": {}, "data_source_id": "ds1"}),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_notion_writer(payload)

        assert "Daily log" in result or "daily" in result.lower()

    @pytest.mark.asyncio
    async def test_daily_log_sends_summary_and_tags(self):
        from app.agents.notion_writer import run_notion_writer

        payload = {
            "summary": "Shipped feature X",
            "mood": 5,
            "energy": "high",
            "tags": ["coding", "release"],
            "tasks": [],
            "learnings": [],
            "project_updates": [],
            "updates": [],
        }

        mock_mcp = _make_mcp_mock()
        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value={"fields": {}, "data_source_id": "ds1"}),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            await run_notion_writer(payload)

        # Find the daily log create_page call
        daily_call = None
        for c in mock_mcp.call_tool.call_args_list:
            args = c[0]
            if args[0] == "API-post-page":
                props = args[1].get("properties", {})
                if "Summary" in props:
                    daily_call = props
                    break

        assert daily_call is not None, "Daily log create_page not called"
        assert daily_call["Summary"]["rich_text"][0]["text"]["content"] == "Shipped feature X"
        tags = {t["name"] for t in daily_call["Tags"]["multi_select"]}
        assert tags == {"coding", "release"}

    @pytest.mark.asyncio
    async def test_task_custom_field_written(self):
        from app.agents.notion_writer import run_notion_writer

        task_schema = {
            "fields": {
                "Who": {"type": "select"},
            },
            "data_source_id": "ds1",
        }

        payload = {
            "summary": "",
            "mood": 3,
            "energy": "medium",
            "tags": [],
            "tasks": [{"title": "Fix bug", "project": "Alpha", "Who": "Vicente"}],
            "learnings": [],
            "project_updates": [],
            "updates": [],
        }

        mock_mcp = _make_mcp_mock()
        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value=task_schema),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            await run_notion_writer(payload)

        task_call = None
        for c in mock_mcp.call_tool.call_args_list:
            args = c[0]
            if args[0] == "API-post-page":
                props = args[1].get("properties", {})
                if "Who" in props:
                    task_call = props
                    break

        assert task_call is not None, "Task with custom field not written"
        assert task_call["Who"] == {"select": {"name": "Vicente"}}

    @pytest.mark.asyncio
    async def test_project_deduplication_skips_existing(self):
        from app.agents.notion_writer import run_notion_writer

        existing_page = {
            "object": "page",
            "id": "page-id-1",
            "properties": {
                "Name": {"title": [{"text": {"content": "Alpha Project"}}]}
            },
        }

        mock_mcp = _make_mcp_mock()
        # Search returns the existing project
        mock_mcp.call_tool = AsyncMock(side_effect=lambda tool, args: (
            _mcp_ok({"results": [existing_page]}) if tool == "API-post-search"
            else _mcp_ok({})
        ))

        payload = {
            "summary": "",
            "mood": 3,
            "energy": "medium",
            "tags": [],
            "tasks": [],
            "learnings": [],
            "project_updates": [{"name": "Alpha Project", "progress_note": "ongoing"}],
            "updates": [],
        }

        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value={"fields": {}, "data_source_id": "ds1"}),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_notion_writer(payload)

        # Project create call should NOT have been made
        project_creates = [
            c for c in mock_mcp.call_tool.call_args_list
            if c[0][0] == "API-post-page"
            and c[0][1].get("parent", {}).get("database_id") == ""  # projects DB id is empty in test settings
        ]
        assert "0 projects" in result

    @pytest.mark.asyncio
    async def test_bulk_update_uses_page_id_directly(self):
        from app.agents.notion_writer import run_notion_writer

        task_schema = {
            "fields": {"Who": {"type": "select"}},
            "data_source_id": "ds1",
        }

        payload = {
            "summary": "",
            "mood": 3,
            "energy": "medium",
            "tags": [],
            "tasks": [],
            "learnings": [],
            "project_updates": [],
            "updates": [
                {"table": "tasks", "name": "Fix bug", "page_id": "page-abc", "field": "Who", "value": "Vicente"}
            ],
        }

        mock_mcp = _make_mcp_mock()
        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value=task_schema),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_notion_writer(payload)

        patch_call = next(
            (c for c in mock_mcp.call_tool.call_args_list if c[0][0] == "API-patch-page"),
            None,
        )
        assert patch_call is not None, "PATCH not called"
        assert patch_call[0][1]["page_id"] == "page-abc"
        assert "1 updated" in result

    @pytest.mark.asyncio
    async def test_result_string_format(self):
        from app.agents.notion_writer import run_notion_writer

        payload = {
            "summary": "Done",
            "mood": 3,
            "energy": "medium",
            "tags": [],
            "tasks": [{"title": "T1", "project": None}],
            "learnings": [{"insight": "Learned X", "area": "tech"}],
            "project_updates": [],
            "updates": [],
        }

        mock_mcp = _make_mcp_mock()
        with (
            patch("app.agents.notion_writer.mcp_client", mock_mcp),
            patch("app.agents.notion_writer.get_schema", return_value={"fields": {}, "data_source_id": "ds1"}),
            patch("app.agents.notion_writer.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await run_notion_writer(payload)

        assert "1 tasks" in result
        assert "1 learnings" in result
        assert "Saved!" in result
