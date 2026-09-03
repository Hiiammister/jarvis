"""
bella.config — one place that reads the environment.

`.env` is loaded here (from the repo root) on import, so importing anything in
the `bella` package is enough to pick it up. Existing modules that also call
`load_dotenv` themselves keep working — it's safe to call twice.

Environment variables (all optional; defaults preserve current behaviour):

  OLLAMA_MODEL              model name              (default: qwen3:8b)
  CTX_SIZE                  Ollama num_ctx          (default: 4096; never varied per-turn)
  MAX_TOKENS               DEEP profile output cap  (default: 2048)
  BELLA_NORMAL_TOKENS      NORMAL profile cap       (default: 512)
  BELLA_FAST_TOKENS        FAST profile cap         (default: 96)
  MAX_TOOL_ITERATIONS      tool round-trips / turn (default: 12)
  OLLAMA_THINK             keep <think> preamble   (default: off)
  OLLAMA_KEEP_ALIVE        model residency          (default: 30m)
  BELLA_WARMUP             warm the model on start  (default: on)
  BELLA_HOME / JARVIS_HOME state dir               (default: ~/.jarvis)
  SERVER_HOST              FastAPI bind host       (default: 127.0.0.1)
  SERVER_PORT              FastAPI bind port       (default: 8765)
  JARVIS_TOKEN             remote auth bearer token (required only for `serve`)
  JARVIS_VOICE             spoken replies on       (default: off)
  ELEVENLABS_API_KEY       TTS + STT key
  ELEVENLABS_VOICE_ID / ELEVENLABS_MODEL / ELEVENLABS_STT_MODEL / ELEVENLABS_STT_RMS_THRESHOLD
  ALLOW_UNCONFIRMED_WRITE  run WRITE tool calls without a prompt  (default: true)
  ALLOW_DANGEROUS          run DANGEROUS tool calls               (default: true)
  SERPAPI_KEY / SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET  (used by their tools)
"""

from __future__ import annotations

import os
import platform as _platform
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# Load .env once, from the repo root.
try:  # pragma: no cover - trivial
    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")
except Exception:
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


DEFAULT_MODEL = "qwen3:8b"


@dataclass(frozen=True)
class VoiceConfig:
    output_enabled: bool = False
    elevenlabs_api_key: str | None = None
    elevenlabs_voice_id: str = "EXAVITQu4vr4xnSDxMaL"
    elevenlabs_model: str = "eleven_turbo_v2_5"
    stt_model: str = "scribe_v1"
    stt_rms_threshold: int = 300

    @property
    def output_available(self) -> bool:
        return bool(self.elevenlabs_api_key)

    @property
    def input_available(self) -> bool:
        if not self.elevenlabs_api_key:
            return False
        try:
            import sounddevice  # noqa: F401
        except Exception:
            return False
        return True


#: Generation profiles — num_predict (output cap) ONLY. num_ctx never changes
#: between turns (that would force an Ollama KV-cache realloc). FAST for trivial
#: chat, NORMAL for ordinary questions, DEEP for coding / research / long work.
@dataclass(frozen=True)
class GenProfiles:
    fast: int = 96
    normal: int = 512
    deep: int = 2048

    def tokens(self, profile: str) -> int:
        return {"FAST": self.fast, "NORMAL": self.normal, "DEEP": self.deep}.get(
            (profile or "DEEP").upper(), self.deep
        )


@dataclass(frozen=True)
class BellaConfig:
    # -- model / LLM loop --
    ollama_model: str = DEFAULT_MODEL
    ollama_ctx_size: int = 4096
    max_tokens: int = 2048            # == the DEEP profile / full-agent cap
    max_tool_iterations: int = 12
    think: bool = False

    # -- latency --
    profiles: GenProfiles = field(default_factory=GenProfiles)
    #: how long Ollama keeps the model resident after a request. "30m" by
    #: default so an active session doesn't repeatedly pay model-load cost.
    #: "0" disables (Ollama's own default is 5m); "-1" = forever.
    keep_alive: str = "30m"
    #: fire a tiny generation on startup so the first real request is warm
    warmup: bool = True

    # -- paths --
    home_dir: Path = field(default_factory=lambda: Path.home() / ".jarvis")

    # -- server --
    server_host: str = "127.0.0.1"
    server_port: int = 8765
    auth_token: str | None = None

    # -- permissions --
    allow_unconfirmed_write: bool = True
    allow_dangerous: bool = True

    # -- CLI --
    verbose: bool = False

    # -- voice --
    voice: VoiceConfig = field(default_factory=VoiceConfig)

    # -- misc integrations --
    serpapi_key: str | None = None
    spotify_client_id: str | None = None
    spotify_client_secret: str | None = None

    platform: str = field(default_factory=lambda: _platform.system() or "unknown")

    @property
    def base_options(self) -> dict:
        """Ollama `options` dict — kept identical across every call so the KV
        cache isn't dropped and rebuilt between turns."""
        return {"num_predict": self.max_tokens, "num_ctx": self.ollama_ctx_size}

    @property
    def history_file(self) -> Path:
        return self.home_dir / "prompt_history"


def load_config() -> BellaConfig:
    home = os.getenv("BELLA_HOME") or os.getenv("JARVIS_HOME")
    voice = VoiceConfig(
        output_enabled=_bool("JARVIS_VOICE", False),
        elevenlabs_api_key=os.getenv("ELEVENLABS_API_KEY"),
        elevenlabs_voice_id=os.getenv("ELEVENLABS_VOICE_ID", "EXAVITQu4vr4xnSDxMaL"),
        elevenlabs_model=os.getenv("ELEVENLABS_MODEL", "eleven_turbo_v2_5"),
        stt_model=os.getenv("ELEVENLABS_STT_MODEL", "scribe_v1"),
        stt_rms_threshold=_int("ELEVENLABS_STT_RMS_THRESHOLD", 300),
    )
    profiles = GenProfiles(
        fast=_int("BELLA_FAST_TOKENS", 96),
        normal=_int("BELLA_NORMAL_TOKENS", 512),
        deep=_int("MAX_TOKENS", 2048),  # DEEP reuses the existing MAX_TOKENS var
    )
    return BellaConfig(
        ollama_model=os.getenv("OLLAMA_MODEL", DEFAULT_MODEL),
        ollama_ctx_size=_int("CTX_SIZE", 4096),
        max_tokens=_int("MAX_TOKENS", 2048),
        max_tool_iterations=_int("MAX_TOOL_ITERATIONS", 12),
        think=_bool("OLLAMA_THINK", False),
        profiles=profiles,
        keep_alive=os.getenv("OLLAMA_KEEP_ALIVE", "30m"),
        warmup=_bool("BELLA_WARMUP", True),
        home_dir=Path(home).expanduser() if home else Path.home() / ".jarvis",
        server_host=os.getenv("SERVER_HOST", "127.0.0.1"),
        server_port=_int("SERVER_PORT", 8765),
        auth_token=os.getenv("JARVIS_TOKEN"),
        allow_unconfirmed_write=_bool("ALLOW_UNCONFIRMED_WRITE", True),
        allow_dangerous=_bool("ALLOW_DANGEROUS", True),
        verbose=_bool("BELLA_VERBOSE", False),
        voice=voice,
        serpapi_key=os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY"),
        spotify_client_id=os.getenv("SPOTIFY_CLIENT_ID"),
        spotify_client_secret=os.getenv("SPOTIFY_CLIENT_SECRET"),
    )


@lru_cache(maxsize=1)
def get_config() -> BellaConfig:
    """Process-wide singleton config. Call `get_config.cache_clear()` in tests
    to force a reload."""
    return load_config()
