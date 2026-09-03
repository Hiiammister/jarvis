"""
bella.latency — lightweight timing instrumentation.

A `Timings` object collects named spans for one request. It is threaded through
the runtime / LLM / voice paths and rendered only in verbose/debug mode
(`Renderer.latency`), so normal output stays clean.

Usage:
    t = Timings()
    with t.measure("route"):
        decision = classify(text)
    t.mark("ollama first token")          # one-shot event, from request start
    ...
    print(t.render())

Overhead is a couple of `time.perf_counter()` calls per span — negligible next
to anything it measures.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field


@dataclass
class Timings:
    #: ordered {label: seconds}
    spans: dict[str, float] = field(default_factory=dict)
    _t0: float = field(default_factory=time.perf_counter)
    _open: dict[str, float] = field(default_factory=dict)

    # -- span timing -------------------------------------------------------

    def start(self, label: str) -> None:
        self._open[label] = time.perf_counter()

    def stop(self, label: str) -> None:
        t = self._open.pop(label, None)
        if t is not None:
            self.add(label, time.perf_counter() - t)

    @contextmanager
    def measure(self, label: str):
        s = time.perf_counter()
        try:
            yield
        finally:
            self.add(label, time.perf_counter() - s)

    def add(self, label: str, seconds: float) -> None:
        # If a label repeats (e.g. two tool calls), keep them distinct.
        if label in self.spans:
            i = 2
            while f"{label} #{i}" in self.spans:
                i += 1
            label = f"{label} #{i}"
        self.spans[label] = seconds

    def mark(self, label: str) -> None:
        """Record a one-shot event as elapsed-since-start (e.g. time to first
        token)."""
        self.spans[label] = time.perf_counter() - self._t0

    # -- output ---------------------------------------------------------

    def total(self) -> float:
        return time.perf_counter() - self._t0

    def as_rows(self, include_total: bool = True) -> list[tuple[str, float]]:
        rows = list(self.spans.items())
        if include_total:
            rows.append(("total", self.total()))
        return rows

    @staticmethod
    def fmt(seconds: float) -> str:
        if seconds < 1.0:
            return f"{seconds * 1000:.1f} ms"
        return f"{seconds:.2f} s"

    def render(self, title: str = "latency") -> str:
        rows = self.as_rows()
        if not rows:
            return ""
        width = max(len(k) for k, _ in rows)
        lines = [f"[{title}]"]
        lines += [f"{k.ljust(width)}   {self.fmt(v)}" for k, v in rows]
        return "\n".join(lines)


#: a no-op Timings for hot paths that don't want instrumentation
class _NullTimings(Timings):
    def add(self, label: str, seconds: float) -> None:  # noqa: D401
        pass

    def mark(self, label: str) -> None:
        pass


NULL_TIMINGS = _NullTimings()
