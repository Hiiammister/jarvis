"""
bella.memory_extract — the post-turn memory curator.

After each agent turn a small, focused LLM call looks for durable facts the user
stated about themselves or their environment and appends them to MEMORY.md /
USER.md. Best-effort and silent: it never raises into the main loop.

Moved from jarvis.py unchanged in substance. Still LLM-driven and imperfect on
the 8B model (see the jarvis-architecture note) — the size gate, target
validation, and normalized dedup are the guardrails.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from memory.core import MEMORY_FILE, USER_FILE, _read_entries, memory_add

from bella.config import get_config


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower().rstrip(".")


# Messages that never carry a durable fact worth an extraction LLM call.
# Conservative on purpose — anything with real content must still pass.
_TRIVIAL_RE = re.compile(
    r"^\s*(hi|hii|hey|hello|helo|hiya|yo|sup|howdy|"
    r"thanks?|thank\s?you|thankyou|thx|ty|tysm|cheers|much\s+appreciated|"
    r"appreciate\s+it|"
    r"ok|okay|k|kk|kay|alright|aight|got\s?it|gotcha|understood|noted|"
    r"sounds?\s+good|makes\s+sense|fair\s+enough|"
    r"cool|nice|great|awesome|perfect|sweet|excellent|brilliant|amazing|"
    r"yes|yeah|yep|yup|sure|no|nope|nah|"
    r"lol|lmao|lmfao|rofl|haha+|hehe|heh|"
    r"bye|goodbye|see\s?ya|see\s+you|later|laters|cya|good\s?night|goodnight|night|"
    r"take\s+care|peace|gm|gn)"
    r"[\s.!?,]*$",
    re.I,
)


def should_extract_memory(user_input: str, assistant_reply: str) -> bool:
    """Whether a post-turn memory-extraction pass is worth running.

    Skips only *clearly* trivial exchanges (greetings, acks, sign-offs, very
    short noise). Anything that could carry a durable fact — a preference, an
    environment detail, "remember that ..." — passes through.
    """
    u = user_input.strip()
    if not u or u.startswith("/"):
        return False
    if _TRIVIAL_RE.match(u):
        return False
    # Very short *and* not obviously a statement of fact.
    if len(u) < 12 and not re.search(r"\b(i|my|i'm|im|we|our|use|using|prefer|always|never)\b", u, re.I):
        return False
    # Original floor: not enough combined signal for the curator to work with.
    if len(u) + len(assistant_reply.strip()) < 40:
        return False
    return True


def extract_and_save(
    user_input: str,
    reply_text: str,
    on_note: Callable[[str, str], None] | None = None,
) -> list[dict]:
    """Run the extraction pass. `on_note(target, content)` is called for each
    fact actually saved. Returns the list of saved {target, content} dicts."""
    config = get_config()

    # Not enough signal in a tiny exchange — skip the LLM call entirely.
    if len(user_input.strip()) + len(reply_text.strip()) < 40:
        return []
    if reply_text.lstrip().startswith("[") and "error" in reply_text[:40].lower():
        return []

    mem_entries = _read_entries(MEMORY_FILE)
    user_entries = _read_entries(USER_FILE)
    existing_norm = {_norm(e) for e in mem_entries + user_entries}
    existing = "\n".join(mem_entries + user_entries)

    extraction_prompt = f"""You are a strict memory curator. Extract ONLY durable facts the user stated about themselves or their environment in THIS exchange.

Hard rules:
- Extract only facts explicitly present in the conversation below. Never guess or invent.
- "user" store: the user's name, timezone, location, stated preferences, communication style.
- "memory" store: environment/setup facts, project context, conventions, tasks the user confirmed done.
- Do NOT save: greetings, small talk, questions, one-off requests, anything already listed under "Existing memory", anything you are not certain about.
- Default to NOTHING. Most exchanges should produce NOTHING.

Existing memory (never re-save these or close variants):
{existing if existing else "(empty)"}

Conversation:
User: {user_input}
Assistant: {reply_text}

Output one compact JSON object per NEW fact, one per line: {{"target": "user"|"memory", "content": "the fact"}}
If there is nothing new and durable, output exactly: NOTHING"""

    saved: list[dict] = []
    try:
        import ollama

        chat_kwargs = dict(
            model=config.ollama_model,
            messages=[{"role": "user", "content": extraction_prompt}],
            stream=False,
            keep_alive=config.keep_alive,
            # num_ctx stays at config.base_options' value — never varied per call.
            options={**config.base_options, "num_predict": 256, "temperature": 0},
        )
        try:
            resp = ollama.chat(think=config.think, **chat_kwargs)
        except (ollama.ResponseError, TypeError):
            resp = ollama.chat(**chat_kwargs)
        raw = resp.get("message", {}).get("content", "").strip()
        if not raw or raw.upper() == "NOTHING":
            return []

        for line in raw.splitlines():
            line = re.sub(r'^```[a-z]*|```$', '', line.strip()).strip()
            if not line or line.upper() == "NOTHING":
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            target = obj.get("target", "memory")
            content = (obj.get("content") or "").strip()
            if target not in ("user", "memory") or len(content) < 3:
                continue
            if _norm(content) in existing_norm:
                continue  # model ignored the "don't re-save" rule — enforce it
            result = memory_add(target, content)
            if result.get("success"):
                existing_norm.add(_norm(content))
                saved.append({"target": target, "content": content})
                if on_note is not None:
                    on_note(target, content)
    except Exception:
        pass  # Memory extraction is best-effort; never crash the main loop
    return saved
