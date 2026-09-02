"""
llm/claude.py — Claude client with streaming + tool-use agentic loop.
"""

import os
import json
from typing import Callable, Generator

import anthropic

from tools.executor import TOOL_SCHEMAS, execute_tool

MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-4-5")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8096"))
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "12"))


def get_client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to your .env file.")
    return anthropic.Anthropic(api_key=api_key)


def run_agent_turn(
    messages: list[dict],
    system: str,
    on_text: Callable[[str], None],
    on_tool_start: Callable[[str, dict], None],
    on_tool_end: Callable[[str, str], None],
) -> tuple[str, list[dict]]:
    """
    Run one full agentic turn (potentially multiple tool calls) until
    Claude returns a stop_reason of 'end_turn'.

    Returns:
      - final_text: the concatenated assistant text for this turn
      - updated messages: the messages list with assistant + tool results appended
    """
    client = get_client()
    final_text_parts = []

    for _iteration in range(MAX_TOOL_ITERATIONS):
        # Stream the response
        current_text = []

        with client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=TOOL_SCHEMAS,
        ) as stream:
            for event in stream:
                if event.type == "content_block_delta" and hasattr(event.delta, "text"):
                    on_text(event.delta.text)
                    current_text.append(event.delta.text)

            final_message = stream.get_final_message()

        # Build assistant message from the final response
        assistant_msg = {"role": "assistant", "content": final_message.content}
        messages = messages + [assistant_msg]

        text_so_far = "".join(current_text)
        if text_so_far:
            final_text_parts.append(text_so_far)

        # If no tool calls, we're done
        if final_message.stop_reason == "end_turn":
            break

        # Process tool calls
        if final_message.stop_reason == "tool_use":
            tool_results = []
            for block in final_message.content:
                if block.type == "tool_use":
                    tool_name = block.name
                    tool_input = block.input  # already parsed by SDK
                    tool_id = block.id

                    on_tool_start(tool_name, tool_input)
                    result_str = execute_tool(tool_name, tool_input)
                    on_tool_end(tool_name, result_str)

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": result_str,
                    })

            tool_result_msg = {"role": "user", "content": tool_results}
            messages = messages + [tool_result_msg]
            # Loop — Claude will continue after seeing tool results
            continue

        # Any other stop reason — bail out
        break

    return "".join(final_text_parts), messages
