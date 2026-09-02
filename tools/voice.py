"""
tools/voice.py — ElevenLabs text-to-speech, spoken out of THIS machine's
speakers. Trial feature.

Self-contained by design, so it's trivial to rip back out:
  - No new pip dependency (stdlib urllib/subprocess/tempfile only) — nothing
    to `pip uninstall`.
  - Exactly one function, `speak()`, imported from exactly one place
    (jarvis.py's REPL — grep for "tools.voice"). The REPL's silent/tts/listen
    modes toggle output on or off per-turn by setting this module's
    VOICE_ENABLED attribute directly (`_voice_mod.VOICE_ENABLED = ...`);
    they don't change how speak() itself works.
  - Any failure (no key, network error, no speakers) is swallowed; jarvis
    behaves identically to before this file existed if you remove it.

To remove voice entirely:
  1. rm tools/voice.py
  2. In jarvis.py, delete the "from tools import voice as _voice_mod" and
     "from tools.voice import speak" imports, the two `_spawn_bg(speak, ...)`
     call sites, and the `_voice_mod.VOICE_ENABLED = ...` lines in run_repl's
     mode loop.
  3. Remove ELEVENLABS_* / JARVIS_VOICE from .env (optional, harmless to
     leave).

Deliberately NOT wired into server.py: voice is local-only. A message
handled for a remote device (Windows/iPad/WhatsApp via server.py) never
triggers audio, and no audio is ever sent back over the network — it only
plays here, out loud, when you're driving jarvis directly from this Mac.
"""

import json
import os
import subprocess
import tempfile
import urllib.request
from pathlib import Path

# jarvis.py already loads .env before importing this module, but this file
# is also meant to be run standalone (`python tools/voice.py` to list
# voices) — load it here too so that works. Safe to call twice.
from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
# Default voice = "Bella", one of ElevenLabs' original classic premade
# voices. On a free-plan account, most Voice Library voices (including
# their own "Rachel" default) 402 via the API with "paid_plan_required" —
# only a handful of the old premade set (Bella, Antoni, Arnold, Adam) are
# confirmed to still work unpaid; Bella is the female one of those four.
# Upgrading the plan lifts this entirely; override with any voice_id from
# https://elevenlabs.io/app/voice-library once you do.
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL")
ELEVENLABS_MODEL = os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

VOICE_ENABLED = os.getenv("JARVIS_VOICE", "").strip().lower() in ("1", "true", "yes")


def speak(text: str) -> None:
    """Speak `text` out loud via ElevenLabs TTS + `afplay`. Best-effort and
    silent on failure — voice is a nice-to-have, never something that
    should break a turn or spam errors into the REPL."""
    if not VOICE_ENABLED:
        return
    text = (text or "").strip()
    if not text or not ELEVENLABS_API_KEY:
        return

    path = None
    try:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}"
        payload = json.dumps({
            "text": text,
            "model_id": ELEVENLABS_MODEL,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={
                "xi-api-key": ELEVENLABS_API_KEY,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            audio = resp.read()

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio)
            path = f.name
        subprocess.run(["afplay", path], timeout=60, capture_output=True)
    except Exception:
        pass  # best-effort — never raise into the caller
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass


def list_voices() -> list[dict]:
    """Fetch the voices already in this ElevenLabs account (GET /v1/voices).
    Run `python tools/voice.py` to print them; copy a voice_id you like into
    ELEVENLABS_VOICE_ID in .env."""
    if not ELEVENLABS_API_KEY:
        raise SystemExit("ELEVENLABS_API_KEY is not set in .env")
    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": ELEVENLABS_API_KEY},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    return data.get("voices", [])


if __name__ == "__main__":
    for v in list_voices():
        labels = v.get("labels", {}) or {}
        descr = ", ".join(x for x in (labels.get("gender"), labels.get("accent"), labels.get("age")) if x)
        print(f"{v['voice_id']}  {v['name']:<20} {descr}")
