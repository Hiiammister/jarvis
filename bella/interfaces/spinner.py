"""
bella.interfaces.spinner — the persistent per-turn activity indicator.

One `Spinner` lives for the length of a turn. It shows a single line that
repaints in place:

    Bella ◌ thinking
    Bella ◈ searching web        (while a tool runs)
    Bella ◌ thinking  4s         (elapsed appears after a few seconds)

It is *paused* (line cleared) while assistant text is streaming and while a
tool's own output is being printed, then *resumed*, so nothing ever interleaves
with the animation. All stdout writes go through one lock.

The animation is deliberately calm: the marker (◌ / ◈) is steady, only a
trailing ellipsis cycles. This matches the requested mock and avoids the
"hacker terminal" strobe, while still proving liveness during long
`osascript` / subprocess calls (an elapsed counter backs that up after 3s).
"""

from __future__ import annotations

import sys
import threading
import time

from bella.interfaces import theme


class Spinner:
    _DOTS = ["", ".", "..", "..."]

    def __init__(self, label: str = "thinking", marker: str = theme.THINKING,
                 marker_style: str = theme.ANSI_MUTED, who: str = "Bella"):
        self.label = label
        self.marker = marker
        self.marker_style = marker_style
        self.who = who  # name shown before the marker ("Bella"); "" hides it
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> "Spinner":
        self._t0 = time.monotonic()
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()

    # -- state -------------------------------------------------------------

    def set_activity(self, label: str, *, marker: str = theme.TOOL,
                     marker_style: str = theme.ANSI_ACCENT) -> None:
        """Switch to a labelled activity (e.g. a running tool)."""
        with self._lock:
            self.label = label
            self.marker = marker
            self.marker_style = marker_style

    def thinking(self) -> None:
        self.set_activity("thinking", marker=theme.THINKING, marker_style=theme.ANSI_MUTED)

    def pause(self) -> None:
        """Hide the spinner and clear its line. Idempotent."""
        if self._paused.is_set():
            return
        self._paused.set()
        with self._lock:
            sys.stdout.write(theme.ANSI_CLEAR_LINE)
            sys.stdout.flush()

    def resume(self) -> None:
        self._paused.clear()

    def write(self, text: str) -> None:
        """Emit text on the spinner's lock so nothing garbles."""
        with self._lock:
            sys.stdout.write(text)
            sys.stdout.flush()

    # -- animation -------------------------------------------------------

    def _frame(self, tick: int) -> str:
        dots = self._DOTS[tick % len(self._DOTS)]
        elapsed = time.monotonic() - self._t0
        tail = f"  {theme.ANSI_MUTED}{int(elapsed)}s{theme.ANSI_RESET}" if elapsed >= 3 else ""
        who = f"{theme.ANSI_BELLA}{self.who}{theme.ANSI_RESET} " if self.who else ""
        return (
            f"\r{who}"
            f"{self.marker_style}{self.marker}{theme.ANSI_RESET} "
            f"{theme.ANSI_MUTED}{self.label}{dots}{theme.ANSI_RESET}{tail}"
            f"\033[K"
        )

    def _run(self) -> None:
        tick = 0
        # `wait` (not sleep) so stop() interrupts immediately — a trivial
        # LOCAL/DIRECT turn must not pay a spin-cycle of teardown latency.
        while not self._stop.wait(0.33 if tick else 0.0):
            with self._lock:
                if not self._paused.is_set():
                    sys.stdout.write(self._frame(tick))
                    sys.stdout.flush()
            tick += 1
        if not self._paused.is_set():
            with self._lock:
                sys.stdout.write(theme.ANSI_CLEAR_LINE)
                sys.stdout.flush()
