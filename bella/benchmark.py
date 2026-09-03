"""
bella.benchmark — `/benchmark` / `bella benchmark`.

Measures the representative request paths with real timings. Read-only: it
never runs a destructive tool, never writes memory, never plays media.

Cases:
  Greeting          classify() + canned response  (LOCAL_RESPONSE path)
  /status           classify() of a slash command
  Direct tool       classify() + a read-only tool (volume get) end to end
  Ollama warm       time to load / reach the model (ps + 1-token generate)
  Fast chat         a real FAST_CHAT call — total and time-to-first-token
"""

from __future__ import annotations

import time

from bella.config import BellaConfig, get_config
from bella.latency import Timings


def _fmt(seconds: float) -> str:
    return Timings.fmt(seconds)


def run_benchmark(config: BellaConfig | None = None) -> list[tuple[str, str, str]]:
    config = config or get_config()
    rows: list[tuple[str, str, str]] = []

    from bella.router import Route, classify

    # 1. Greeting — pure routing + canned reply.
    t0 = time.perf_counter()
    for _ in range(50):
        d = classify("hi")
    dt = (time.perf_counter() - t0) / 50
    rows.append(("Greeting", _fmt(dt), f"→ {d.route.value}: {d.response!r}"))

    # 2. Slash command routing.
    t0 = time.perf_counter()
    for _ in range(50):
        d = classify("/status")
    dt = (time.perf_counter() - t0) / 50
    rows.append(("/status route", _fmt(dt), f"→ {d.route.value}"))

    # 3. A read-only direct tool, end to end.
    from tools.registry import get_registry

    reg = get_registry()
    d = classify("what's the volume")
    if d.route is Route.DIRECT_TOOL:
        t0 = time.perf_counter()
        reg.execute(d.tool_name, d.tool_args)
        dt = time.perf_counter() - t0
        rows.append(("Direct tool", _fmt(dt), f"{d.tool_name} {d.tool_args}"))
    else:
        rows.append(("Direct tool", "—", f"routed {d.route.value}"))

    # 4. Ollama reachability + model residency / warm cost.
    try:
        import ollama

        t0 = time.perf_counter()
        ps = ollama.ps()
        resident = any(
            (m.get("model") or "").split(":")[0] == config.ollama_model.split(":")[0]
            for m in getattr(ps, "models", []) or []
        )
        ollama.chat(
            model=config.ollama_model,
            messages=[{"role": "user", "content": "hi"}],
            stream=False, keep_alive=config.keep_alive,
            options={"num_ctx": config.ollama_ctx_size, "num_predict": 1},
        )
        dt = time.perf_counter() - t0
        rows.append(("Ollama warm", _fmt(dt),
                     "already resident" if resident else "loaded on demand"))
    except Exception as e:
        rows.append(("Ollama warm", "—", f"unreachable: {e}"))

    # 5. A real FAST_CHAT call.
    try:
        from llm.ollama_client import run_fast_chat
        from bella.prompt import build_system_prompt

        tm = Timings()
        system = build_system_prompt(minimal=True)
        reply = run_fast_chat(
            [{"role": "user", "content": "say hello in three words"}],
            system, lambda _s: None, profile="FAST", timings=tm,
        )
        first = tm.spans.get("ollama first token")
        total = tm.total()
        detail = f"first token {_fmt(first)}" if first else (reply[:40] or "no reply")
        rows.append(("Fast chat", _fmt(total), detail))
    except Exception as e:
        rows.append(("Fast chat", "—", f"failed: {e}"))

    return rows
