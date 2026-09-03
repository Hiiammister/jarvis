"""tools/recall.py — FTS5 full-text search across all past session history."""

from __future__ import annotations

from memory.core import search_history

from tools.base import PermissionLevel, Tool
from tools.registry import register


def run_recall(query: str, limit: int = 5) -> dict:
    results = search_history(query, limit=limit)
    return {"results": results, "count": len(results)}


class RecallTool(Tool):
    name = "recall"
    description = (
        "Search past conversation history using full-text search (FTS5). Use to "
        "recall what was discussed in previous sessions."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "Search terms."},
            "limit": {"type": "integer", "description": "Max results (default 5)."},
        },
        "required": ["query"],
    }
    permission = PermissionLevel.READ_ONLY

    def execute(self, query: str, limit: int = 5) -> dict:
        return run_recall(query, limit)


register(RecallTool())
