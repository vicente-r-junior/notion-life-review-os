import asyncio
import concurrent.futures
import json

from crewai.tools import tool

from app.notion.mcp_client import mcp_client


def run_async(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, coro)
                return future.result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


@tool("Search Notion")
def search_notion(query: str) -> str:
    """Search across the Notion workspace"""
    result = run_async(mcp_client.call_tool("API-post-search", {"query": query}))
    return str(result)


@tool("Fetch Notion Page or Database")
def fetch_notion(url_or_id: str) -> str:
    """Fetch content from a Notion page or database by URL or ID"""
    result = run_async(mcp_client.call_tool("API-retrieve-a-page", {"page_id": url_or_id}))
    return str(result)


@tool("Query Notion Data Source")
def query_data_source(data_source_id: str, filter: str = None) -> str:
    """Query a Notion database (data source) with optional filter as JSON string"""
    args = {"data_source_id": data_source_id}
    if filter:
        args["filter"] = json.loads(filter)
    result = run_async(mcp_client.call_tool("API-query-data-source", args))
    return str(result)


@tool("Create Notion Pages")
def create_notion_pages(pages: str) -> str:
    """Create one or more Notion pages. pages is a JSON string of page specs."""
    result = run_async(
        mcp_client.call_tool("API-post-page", json.loads(pages))
    )
    return str(result)


@tool("Update Notion Page")
def update_notion_page(page_id: str, properties: str) -> str:
    """Update a Notion page properties. properties is a JSON string."""
    result = run_async(
        mcp_client.call_tool(
            "API-patch-page",
            {"page_id": page_id, "properties": json.loads(properties)},
        )
    )
    return str(result)


@tool("Update Notion Data Source Schema")
def update_data_source(data_source_id: str, properties: str) -> str:
    """Add or modify columns in a Notion database. properties is a JSON string."""
    result = run_async(
        mcp_client.call_tool(
            "API-update-a-data-source",
            {"data_source_id": data_source_id, "properties": json.loads(properties)},
        )
    )
    return str(result)


@tool("Retrieve Notion Database")
def retrieve_database(database_id: str) -> str:
    """Retrieve a Notion database metadata including data source IDs"""
    result = run_async(
        mcp_client.call_tool("API-retrieve-a-database", {"database_id": database_id})
    )
    return str(result)


@tool("Retrieve Notion Data Source Schema")
def retrieve_data_source(data_source_id: str) -> str:
    """Retrieve the schema of a Notion data source"""
    result = run_async(
        mcp_client.call_tool(
            "API-retrieve-a-data-source", {"data_source_id": data_source_id}
        )
    )
    return str(result)
