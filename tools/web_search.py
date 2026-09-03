"""tools/web_search.py — web search via SerpAPI (Google) or DuckDuckGo."""

from __future__ import annotations

import os

from tools.base import PermissionLevel, Tool
from tools.registry import register


def run_web_search(query: str, max_results: int = 5) -> dict:
    """Google search via SerpAPI if key is set, otherwise DuckDuckGo."""
    serpapi_key = os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY")
    if serpapi_key:
        try:
            from serpapi import GoogleSearch
            search = GoogleSearch({
                "q": query,
                "num": min(max_results, 10),
                "api_key": serpapi_key,
            })
            data = search.get_dict()
            results = []
            for r in data.get("organic_results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                })
            return {"results": results, "count": len(results), "engine": "google"}
        except Exception:
            # Fall through to DDG
            pass

    # Fallback: DuckDuckGo (ddgs, formerly duckduckgo-search)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(max_results, 10)):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", "") or r.get("link", ""),
                    "snippet": r.get("body", ""),
                })
        return {"results": results, "count": len(results), "engine": "duckduckgo"}
    except Exception as e:
        return {"error": str(e)}


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Search the web. Uses Google (via SerpAPI) if configured, otherwise "
        "DuckDuckGo. Use for current events, docs, anything outside training data."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search query."},
            "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)."},
        },
        "required": ["query"],
    }
    permission = PermissionLevel.READ_ONLY

    def execute(self, query: str, max_results: int = 5) -> dict:
        return run_web_search(query, max_results)


register(WebSearchTool())
