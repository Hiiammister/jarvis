"""
bella.doctor — lightweight diagnostics for `bella doctor` / `/doctor`.

Independent checks grouped into Core / Capabilities / Server. One failing
optional component never fails the whole command; the exit code is non-zero
only if a *required* check (Python, memory, tools) fails.

Rendering lives in bella.interfaces.renderer (`Renderer.doctor`); this module
only produces the `Check` list.
"""

from __future__ import annotations

import shutil
import socket
import sqlite3
import subprocess
import sys
from dataclasses import dataclass

from bella.config import BellaConfig, get_config

OK = "ok"
WARN = "warn"
FAIL = "fail"
INACTIVE = "inactive"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    group: str = "Core"
    required: bool = False


# ── individual check groups ────────────────────────────────────────────────

def _core_checks(config: BellaConfig) -> list[Check]:
    out: list[Check] = [
        Check("Python", OK if sys.version_info >= (3, 9) else FAIL,
              sys.version.split()[0], "Core", required=True),
    ]

    # Ollama + model
    try:
        import ollama

        data = ollama.list()
        models = [m.get("model") or m.get("name") for m in data.get("models", [])]
        out.append(Check("Ollama", OK, f"reachable · {len(models)} models", "Core"))
        wanted = config.ollama_model
        if any(m == wanted or (m or "").split(":")[0] == wanted.split(":")[0] for m in models):
            resident = False
            try:
                ps = ollama.ps()
                resident = any(
                    (m.get("model") or "").split(":")[0] == wanted.split(":")[0]
                    for m in getattr(ps, "models", []) or []
                )
            except Exception:
                pass
            detail = "installed · resident" if resident else f"installed · not resident (keep_alive {config.keep_alive})"
            out.append(Check(wanted, OK if resident else INACTIVE, detail, "Core"))
        else:
            out.append(Check(wanted, WARN, f"not installed — run `ollama pull {wanted}`", "Core"))
    except Exception as e:
        out.append(Check("Ollama", FAIL, f"unreachable — is `ollama serve` running? ({e})", "Core"))
        out.append(Check(config.ollama_model, INACTIVE, "can't check — Ollama unreachable", "Core"))

    # Memory + SQLite
    try:
        from memory.core import DB_FILE, MEMORY_FILE, USER_FILE, _ensure_home

        _ensure_home()
        d = MEMORY_FILE.parent
        probe = d / ".bella_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        shared = "  (shared with Hermes)" if ".hermes" in str(MEMORY_FILE) else ""
        files_ok = MEMORY_FILE.exists() and USER_FILE.exists()
        out.append(Check("Memory", OK if files_ok else FAIL, f"{d}{shared}", "Core", required=True))
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.execute("SELECT count(*) FROM sqlite_master")
            conn.close()
            out.append(Check("SQLite", OK, DB_FILE.name, "Core", required=True))
        except Exception as e:
            out.append(Check("SQLite", FAIL, str(e), "Core", required=True))
    except Exception as e:
        out.append(Check("Memory", FAIL, str(e), "Core", required=True))

    # Tools
    try:
        from tools.registry import get_registry

        r = get_registry()
        avail = r.available()
        out.append(Check("Tools", OK if avail else FAIL,
                         f"{len(avail)}/{len(r.all())} available", "Core", required=True))
    except Exception as e:
        out.append(Check("Tools", FAIL, str(e), "Core", required=True))

    return out


def _capability_checks(config: BellaConfig) -> list[Check]:
    out: list[Check] = []

    # Microphone
    try:
        import sounddevice as sd

        ins = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        out.append(Check("Microphone", OK if ins else WARN,
                         f"{len(ins)} input device(s)" if ins else "no input devices", "Capabilities"))
    except Exception as e:
        out.append(Check("Microphone", WARN, f"unavailable: {e}", "Capabilities"))

    # TTS
    if config.voice.elevenlabs_api_key:
        out.append(Check("TTS", OK, "ElevenLabs key present", "Capabilities"))
    else:
        out.append(Check("TTS", WARN, "ELEVENLABS_API_KEY not set — spoken replies off", "Capabilities"))

    # Platform-native tools
    from tools.platform import IS_MACOS, PLATFORM_NAME

    if IS_MACOS:
        try:
            from tools.platform import macos

            present = macos.spotify_app_present()
            out.append(Check("Spotify", OK if present else WARN,
                             "installed" if present else "not found — will open the web page", "Capabilities"))
        except Exception as e:
            out.append(Check("Spotify", WARN, str(e), "Capabilities"))
    else:
        out.append(Check("Native integrations", WARN,
                         f"volume / Spotify / Reminders are macOS-only ({PLATFORM_NAME})", "Capabilities"))

    # Git / GitHub CLI
    out.append(Check("Git", OK if shutil.which("git") else WARN,
                     shutil.which("git") or "not on PATH", "Capabilities"))
    if shutil.which("gh"):
        try:
            authed = subprocess.run(["gh", "auth", "status"], capture_output=True, timeout=10).returncode == 0
            out.append(Check("GitHub CLI", OK if authed else WARN,
                             "authenticated" if authed else "not logged in — run `gh auth login`", "Capabilities"))
        except Exception as e:
            out.append(Check("GitHub CLI", WARN, str(e), "Capabilities"))
    else:
        out.append(Check("GitHub CLI", WARN, "not installed — github upload unavailable", "Capabilities"))

    return out


def _server_checks(config: BellaConfig) -> list[Check]:
    running = server_running(config)
    out = [Check("Remote server", OK if running else INACTIVE,
                 f"{config.server_host}:{config.server_port}" if running else "not running", "Server")]
    if config.auth_token:
        out.append(Check("Auth token", OK, "set", "Server"))
    else:
        out.append(Check("Auth token", WARN, "JARVIS_TOKEN not set — `bella serve` will refuse to start", "Server"))
    return out


def server_running(config: BellaConfig) -> bool:
    host = "127.0.0.1" if config.server_host in ("0.0.0.0", "") else config.server_host
    try:
        with socket.create_connection((host, config.server_port), timeout=0.3):
            return True
    except OSError:
        return False


# ── public API ─────────────────────────────────────────────────────────────

def run_diagnostics(config: BellaConfig | None = None) -> list[Check]:
    config = config or get_config()
    return _core_checks(config) + _capability_checks(config) + _server_checks(config)


def main(config: BellaConfig | None = None) -> int:
    config = config or get_config()
    from bella.interfaces.renderer import Renderer

    checks = run_diagnostics(config)
    Renderer(verbose=config.verbose).doctor(checks)
    return 1 if any(c.status == FAIL and c.required for c in checks) else 0
