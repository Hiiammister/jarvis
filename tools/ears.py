"""
tools/ears.py — mic input via ElevenLabs Speech-to-Text (Scribe). Trial
feature, the input-side counterpart to tools/voice.py.

Self-contained by design, so it's trivial to rip back out:
  - One new pip dependency beyond what jarvis already has: `sounddevice`
    (thin PortAudio binding; `pip uninstall sounddevice` removes it clean —
    numpy is already part of the environment).
  - Exactly one function meant to be called from outside, `listen()`,
    imported in exactly one place (jarvis.py — grep for "tools.ears").
    It's called from the REPL's "listen" mode (the default), which loops
    on it each turn instead of prompting for typed input.
  - Any failure (no mic permission, no key, network error, silence) returns
    "" rather than raising — a failed listen should feel like an empty
    prompt, never crash the REPL.

To remove voice input entirely:
  1. rm tools/ears.py
  2. In jarvis.py, delete the "from tools.ears import listen as listen_mic"
     import, drop the "listen" branch of run_repl's mode loop (mode can
     just stay "silent"), and drop the /listen slash command + spoken
     mode-switch phrases.
  3. pip uninstall sounddevice (optional — harmless to leave installed).

Local-only, same as tools/voice.py: recording and transcription both happen
on this machine. Deliberately NOT wired into server.py — a remote device
(Windows/iPad/WhatsApp) has no way to reach this Mac's mic anyway, so
there's nothing to route there.
"""

import json
import os
import time
import wave
from pathlib import Path

import numpy as np
import sounddevice as sd
import urllib.request

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
STT_MODEL = os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1")

SAMPLE_RATE = 16000
CHUNK_MS = 30                       # analysis window for silence detection
CHUNK_SIZE = SAMPLE_RATE * CHUNK_MS // 1000
MAX_SECONDS = 20                    # hard cap so a stuck mic can't hang forever
SILENCE_HANG_MS = 1200              # stop after this much quiet, once speech started
# Sustained loud audio needed, in a row, before we call it "speech". Natural
# speech has plenty of quiet gaps even mid-sentence (between syllables,
# breaths), so this needs to be short enough to catch inside one syllable —
# 300ms proved too strict in testing (real speech rarely stays continuously
# above threshold that long) and made detection unreliable.
MIN_SPEECH_MS = 150
# int16 RMS below this counts as quiet. ElevenLabs' STT will confidently
# hallucinate plausible-sounding text from pure room noise/silence — it does
# NOT reliably self-report low confidence on that (word logprobs and
# language_probability both came back high in testing on pure ambient hum).
# So the real safety net is architectural: never even call the STT API
# unless the mic clearly picked up sustained, speech-level loudness.
#
# This default is a guess, not a measurement — ambient noise floor varies a
# lot by machine/room (mic gain, fan noise, etc). Run
# `python tools/ears.py --calibrate` once in your actual setup: it measures
# your room's quiet baseline vs. your normal speaking volume and suggests a
# real number. Override via ELEVENLABS_STT_RMS_THRESHOLD in .env.
SPEECH_RMS_THRESHOLD = int(os.getenv("ELEVENLABS_STT_RMS_THRESHOLD", "300"))


def _record_until_silence(stop_event=None, max_seconds: float = None) -> tuple[np.ndarray, bool]:
    """Record from the default input device until the user stops talking
    (or max_seconds elapses — defaults to module-level MAX_SECONDS).
    Returns (int16 mono samples at SAMPLE_RATE, whether sustained
    speech-level audio was ever actually detected) — hitting the time cap
    without that means it was just ambient noise.

    `stop_event` (a threading.Event), if given, is checked once per ~30ms
    chunk — setting it from another thread aborts the recording within one
    chunk's latency, e.g. so a keyboard press can cut off mic capture
    immediately instead of waiting out the full cycle."""
    if max_seconds is None:
        max_seconds = MAX_SECONDS
    chunks: list[np.ndarray] = []
    silence_run_ms = 0
    speech_run_ms = 0
    speech_started = False
    start = time.monotonic()

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
        while time.monotonic() - start < max_seconds:
            if stop_event is not None and stop_event.is_set():
                break
            data, _ = stream.read(CHUNK_SIZE)
            chunks.append(data.copy())
            rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))

            if rms >= SPEECH_RMS_THRESHOLD:
                speech_run_ms += CHUNK_MS
                silence_run_ms = 0
                if speech_run_ms >= MIN_SPEECH_MS:
                    speech_started = True
            else:
                silence_run_ms += CHUNK_MS
                if not speech_started:
                    speech_run_ms = 0

            if speech_started and silence_run_ms >= SILENCE_HANG_MS:
                break

    audio = np.concatenate(chunks).flatten() if chunks else np.zeros(0, dtype=np.int16)
    return audio, speech_started


def _transcribe(audio: np.ndarray) -> str:
    if audio.size == 0 or not ELEVENLABS_API_KEY:
        return ""

    import io
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    wav_bytes = buf.getvalue()

    boundary = "----jarvisSTT"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="model_id"\r\n\r\n{STT_MODEL}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="mic.wav"\r\n'
        f"Content-Type: audio/wav\r\n\r\n"
    ).encode("utf-8") + wav_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    req = urllib.request.Request(
        "https://api.elevenlabs.io/v1/speech-to-text",
        data=body, method="POST",
        headers={
            "xi-api-key": ELEVENLABS_API_KEY,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return (result.get("text") or "").strip()


def listen(stop_event=None, max_seconds: float = None) -> str:
    """Record one command from the mic and transcribe it. Returns the
    transcript, or "" on any failure (no mic access, no key, network error,
    nothing but silence/ambient noise, or `stop_event` cancelled it) —
    never raises.

    Deliberately skips calling the STT API at all when no sustained
    speech-level audio was detected, rather than trusting the API's own
    confidence signals — see SPEECH_RMS_THRESHOLD's comment for why. Also
    skips it when cancelled — no point transcribing audio nobody wants."""
    if not ELEVENLABS_API_KEY:
        return ""
    try:
        audio, speech_detected = _record_until_silence(stop_event=stop_event, max_seconds=max_seconds)
        if stop_event is not None and stop_event.is_set():
            return ""
        if not speech_detected:
            return ""
        return _transcribe(audio)
    except Exception:
        return ""


def _measure_rms(seconds: float) -> np.ndarray:
    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16") as stream:
        n_chunks = int(seconds * 1000 / CHUNK_MS)
        rmss = np.zeros(n_chunks)
        for i in range(n_chunks):
            data, _ = stream.read(CHUNK_SIZE)
            rmss[i] = np.sqrt(np.mean(data.astype(np.float64) ** 2))
    return rmss


def calibrate() -> None:
    """Measure this room/mic's quiet baseline vs. normal speaking volume,
    and suggest a SPEECH_RMS_THRESHOLD that sits safely between them.

    Compares quiet's *ceiling* against speech's *upper* range (p75), not its
    low end — natural speech has plenty of near-silent gaps (between words,
    breaths) even while "speaking normally" for 3s, so the bottom of that
    range is close to the noise floor and isn't a useful signal. The voiced
    peaks are what the detector actually needs to catch."""
    input("Stay quiet, then press Enter — measuring 3s of room noise...")
    quiet = _measure_rms(3.0)
    print(f"  quiet:  mean={quiet.mean():.0f}  p95={np.percentile(quiet, 95):.0f}  max={quiet.max():.0f}")

    input("Now press Enter, then speak normally for 3s...")
    loud = _measure_rms(3.0)
    print(f"  speech: mean={loud.mean():.0f}  p75={np.percentile(loud, 75):.0f}  p95={np.percentile(loud, 95):.0f}  max={loud.max():.0f}")

    quiet_ceiling = np.percentile(quiet, 95)
    speech_peak = np.percentile(loud, 75)
    if speech_peak <= quiet_ceiling * 1.5:
        print(
            "\nCouldn't find a clean gap between quiet and speech — try again "
            "closer to the mic, or louder. Not suggesting a threshold."
        )
        return
    # Sit close to the noise floor with modest headroom, rather than midway
    # to the speech peak — MIN_SPEECH_MS only needs a handful of consecutive
    # loud chunks, which a threshold near the peak makes too hard to hit.
    suggested = int(quiet_ceiling + 0.25 * (speech_peak - quiet_ceiling))
    print(f"\nSuggested: ELEVENLABS_STT_RMS_THRESHOLD={suggested}  (add this to .env)")


if __name__ == "__main__":
    import sys
    if "--calibrate" in sys.argv:
        calibrate()
    else:
        print(f"Listening (threshold={SPEECH_RMS_THRESHOLD}) — speak now (stops after ~1.2s of silence)...")
        audio, speech_detected = _record_until_silence()
        if not speech_detected:
            print("No speech-level audio detected — nothing sent to the API. "
                  "Run with --calibrate if this seems wrong.")
        else:
            print("Transcribing...")
            print("Heard:", repr(_transcribe(audio)) or "(API returned nothing)")
