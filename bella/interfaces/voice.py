"""
bella.interfaces.voice — everything the CLI's "listen" mode needs.

  - wake-word detection ("Bella, ...") and stripping
  - the wake-or-Enter race (idle-listen for the wake word while watching the
    keyboard for a plain Enter — whichever comes first wins)
  - active command capture once Bella has been addressed
  - Spotify ducking so playback audio doesn't bleed into the mic
  - spoken mode-switch phrases ("silent mode", "tts mode", "listen mode")
  - the per-turn voice-output toggle

The actual TTS/STT primitives stay in the self-contained trial modules
tools/voice.py (`speak`) and tools/ears.py (`listen`) — this module only
orchestrates them. See those files' docstrings for how to remove voice
entirely; the only change here is the import location.
"""

from __future__ import annotations

import json
import re
import select
import sys
import threading

from tools import voice as _voice_mod
from tools.ears import listen as listen_mic
from tools.registry import get_registry

# ── wake word ───────────────────────────────────────────────────────────────

# Requires the wake word as a PREFIX (re.match, not search) — "bella" showing
# up mid-sentence in unrelated speech/lyrics still doesn't count as being
# addressed. The greeting word is optional ("Bella, ..." works too).
_WAKE_WORD_RE = re.compile(r"^(hey|hi|ok|okay)?[\s,]*bella\b[\s,.:!?]*", re.IGNORECASE)


def strip_wake_word(transcript: str) -> tuple[bool, str]:
    """Returns (was_addressed_to_bella, remaining_text_after_the_wake_word)."""
    m = _WAKE_WORD_RE.match(transcript.strip())
    if not m:
        return False, transcript
    return True, transcript[m.end():].strip()


# Spoken equivalents of /silent, /tts, /listen — recognised only while in listen
# mode (the one mode where typing a slash command isn't an option). Exact-phrase
# match after light normalization — deliberately not fuzzy.
_MODE_VOICE_PHRASES = {
    "silent": {"silent mode", "switch to silent mode", "switch to silent",
               "go silent", "text mode", "switch to text mode",
               "stop listening", "text only"},
    "tts": {"tts mode", "switch to tts mode", "switch to tts",
            "text to speech mode", "voice reply mode", "talk mode"},
    "listen": {"listen mode", "switch to listen mode", "voice mode",
               "switch to voice mode", "start listening"},
}


def match_mode_phrase(transcript: str) -> str | None:
    t = transcript.strip().lower().strip(".!?")
    for mode, phrases in _MODE_VOICE_PHRASES.items():
        if t in phrases:
            return mode
    return None


# ── voice output toggle ─────────────────────────────────────────────────────

def set_voice_output(enabled: bool) -> None:
    """Per-turn toggle for spoken replies (listen/tts vs silent)."""
    _voice_mod.VOICE_ENABLED = enabled


# ── Spotify ducking ─────────────────────────────────────────────────────────

def _spotify(action: str) -> dict:
    try:
        return json.loads(get_registry().execute("spotify", {"action": action}))
    except Exception:
        return {}


def duck_spotify() -> bool:
    """Pause Spotify if it's currently playing, so its own audio can't bleed
    into the mic during a listen cycle. Returns whether it was actually playing
    (i.e. whether the caller should resume it afterwards)."""
    if _spotify("current").get("player_state") != "playing":
        return False
    _spotify("pause")
    return True


def unduck_spotify() -> None:
    _spotify("play")  # no query/uri = resume


# ── mic capture ─────────────────────────────────────────────────────────────

# Shorter than the idle wake-word cycle's 20s default — this capture only starts
# once the user has already signalled intent, so give up sooner if nothing
# coherent follows rather than looking hung for the full 20s.
_COMMAND_CAPTURE_SECONDS = 10.0


def capture_command(spinner_factory=None, timings=None) -> str:
    """Duck Spotify, actively record one command from the mic, unduck. Used once
    Bella's already been addressed (wake word confirmed, or a blank Enter).

    `spinner_factory()` should return an object with .start()/.stop() (the CLI
    passes its _Spinner("listening...")); if None, capture runs without a
    visible indicator. `timings` (bella.latency.Timings) attributes record/STT.
    """
    was_playing = duck_spotify()
    stop_event = threading.Event()
    result = {"text": ""}

    def _worker():
        result["text"] = listen_mic(stop_event=stop_event,
                                    max_seconds=_COMMAND_CAPTURE_SECONDS,
                                    timings=timings)

    worker = threading.Thread(target=_worker, daemon=True)
    spinner = spinner_factory() if spinner_factory else None
    if spinner:
        spinner.start()
    worker.start()
    try:
        while worker.is_alive():
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                sys.stdin.readline()  # consume — pressing Enter here means "never mind"
                stop_event.set()
                break
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        worker.join(timeout=3.0)
        if spinner:
            spinner.stop()
        if was_playing:
            unduck_spotify()
    return result["text"]


def wait_for_wake_or_enter() -> tuple[str | None, str | None]:
    """Idle-listen for "Bella ..." while simultaneously watching the keyboard
    for a plain Enter press — whichever comes first wins. Returns
    (heard_transcript, typed_line): exactly one is non-None on a normal return
    (both None only if interrupted).

    The mic side is a real blocking read cancellable via tools.ears's stop_event
    (checked every ~30ms); the keyboard side only ever *polls* stdin
    non-blockingly (`select`, every 100ms), so there's never a stuck call to
    cancel there.
    """
    stop_event = threading.Event()
    result: dict = {"heard": None}

    def _mic_worker():
        result["heard"] = listen_mic(stop_event=stop_event)

    mic_thread = threading.Thread(target=_mic_worker, daemon=True)
    mic_thread.start()

    try:
        while True:
            ready, _, _ = select.select([sys.stdin], [], [], 0.1)
            if ready:
                typed = sys.stdin.readline().rstrip("\n")
                stop_event.set()
                mic_thread.join(timeout=2.0)
                return None, typed
            if not mic_thread.is_alive():
                return result["heard"], None
    except KeyboardInterrupt:
        stop_event.set()
        mic_thread.join(timeout=2.0)
        raise
