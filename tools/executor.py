"""
tools/executor.py — Tool implementations for Jarvis.

Tools:
  - shell       : run a shell command
  - web_search  : DuckDuckGo search
  - read_file   : read a local file
  - write_file  : write/create a local file
  - open_browser: open a URL in the default browser
  - volume      : read/set/adjust/mute the macOS system output volume
  - spotify     : control the Spotify desktop app (open/search/play/pause/next/…)
  - todo        : interact with ~/todo/todos.json (local list, NOT Apple Reminders)
  - reminder    : add/list/complete/delete reminders in the real macOS Reminders app
  - memory      : add/replace/remove/read persistent memory
  - recall      : FTS5 search across session history
"""

import json
import os
import subprocess
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

from memory.core import (
    memory_add, memory_replace, memory_remove, memory_read, search_history
)

TODO_FILE = Path.home() / "todo" / "todos.json"


# ── Tool schemas (for Claude tool_use) ────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "shell",
        "description": "Run a shell command on the user's machine. Use for file system ops, running code, git, etc. Always prefer non-destructive commands. Ask before deleting files or running risky operations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run."},
                "timeout": {"type": "integer", "description": "Timeout in seconds (default 30)."},
            },
            "required": ["command"],
        },
    },
    {
        "name": "web_search",
        "description": "Search the web. Uses Google (via SerpAPI) if configured, otherwise DuckDuckGo. Use for current events, docs, anything outside training data.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query."},
                "max_results": {"type": "integer", "description": "Number of results (default 5, max 10)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "read_file",
        "description": "Read the contents of a local file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~-relative file path."},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "Write or create a local file. Creates parent directories if missing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~-relative file path."},
                "content": {"type": "string", "description": "File content to write."},
                "append": {"type": "boolean", "description": "Append instead of overwrite (default false)."},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "open_browser",
        "description": "Open a URL in the user's default browser. Use this whenever the user wants to visit a website, open a link, or navigate to a page.",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to open."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "volume",
        "description": (
            "Read or change the macOS system output volume (0–100). Use this for ANY "
            "request about 'volume', 'louder', 'quieter', 'turn it up/down', 'mute', "
            "'unmute' that is NOT explicitly about a specific app's own volume slider.\n"
            "Actions:\n"
            "  - get    : return the current output volume and mute state\n"
            "  - set    : set the absolute volume to `level` (0–100)\n"
            "  - adjust : add `delta` to the current volume (delta may be negative). "
            "For 'increase by 50%' / 'raise it 50%', pass percent=50 instead of delta.\n"
            "  - mute   : mute output\n"
            "  - unmute : unmute output\n"
            "Every action returns the resulting volume, so you always know the real state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["get", "set", "adjust", "mute", "unmute"],
                    "description": "The volume operation.",
                },
                "level": {"type": "integer", "description": "Absolute target volume 0–100 (for action=set)."},
                "delta": {"type": "integer", "description": "Points to add to the current volume, may be negative (for action=adjust)."},
                "percent": {"type": "number", "description": "Relative change as a percentage of the current volume, e.g. 50 raises 40→60, -25 lowers it a quarter (for action=adjust)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "spotify",
        "description": (
            "Control the Spotify desktop app on macOS. Use this instead of writing "
            "AppleScript by hand — Spotify's AppleScript dictionary has NO search command.\n"
            "Actions:\n"
            "  - open      : launch/focus the Spotify app\n"
            "  - play      : play a specific track. Pass `query` with the song name "
            "(and artist if known), e.g. query='Do I Wanna Know Arctic Monkeys'. "
            "The tool searches Spotify, plays the top match, and returns the real "
            "track it started. With NO query and NO uri it just resumes playback.\n"
            "  - search    : look up tracks for `query` and return their names + URIs "
            "(also opens the Spotify search page). Use this if the user wants choices.\n"
            "  - pause     : pause playback\n"
            "  - next      : skip to next track\n"
            "  - previous  : go to previous track\n"
            "  - current   : report the currently playing track\n"
            "  - quit      : quit/close the Spotify app entirely (not just pause)\n"
            "NEVER invent a `uri` — only pass one that came back from a previous "
            "search/play result. To play by name, use `query`, not `uri`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["open", "search", "play", "pause", "stop", "next", "previous", "current", "quit"],
                    "description": "The Spotify operation. 'stop' is treated as 'pause'.",
                },
                "query": {"type": "string", "description": "Song name / artist to search for (for action=play or action=search)."},
                "uri": {"type": "string", "description": "A Spotify URI that came from an earlier search/play result. Do NOT fabricate this — use `query` to play by name."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "reminder",
        "description": (
            "Create, list, complete, or delete reminders in the real macOS Reminders app "
            "(Apple's Reminders.app — syncs to iPhone/iPad/Apple Watch via iCloud). Use this "
            "for ANY request to 'remind me', 'set a reminder', 'add a reminder' or similar — "
            "NOT the `todo` tool, which is a separate local list Reminders.app never sees.\n"
            "Actions:\n"
            "  - add      : create a reminder. `text` is required. If the user gives a time "
            "('at 8pm', 'tomorrow morning', 'in an hour'), compute the absolute due_date "
            "yourself from the current date/time given in your system context and pass it "
            "as due_date='YYYY-MM-DD HH:MM' (24-hour). Omit due_date for an undated reminder.\n"
            "  - list     : list upcoming (incomplete) reminders. Optional `list_name`.\n"
            "  - complete : mark a reminder done. `text` is a (sub)string matching its name.\n"
            "  - delete   : remove a reminder. `text` is a (sub)string matching its name.\n"
            "If `text` matches more than one reminder for complete/delete, the tool returns "
            "the candidates instead of guessing — ask the user which one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "list", "complete", "delete"],
                    "description": "The reminder operation.",
                },
                "text": {"type": "string", "description": "Reminder title (for add), or a name substring to match (for complete/delete)."},
                "due_date": {"type": "string", "description": "Absolute due date/time as 'YYYY-MM-DD HH:MM' (24-hour), for action=add."},
                "list_name": {"type": "string", "description": "Name of the Reminders list to use (default: the user's default list)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "todo",
        "description": "Manage the user's local todo list (NOT Apple Reminders — use the `reminder` tool for that). Actions: list, add, insert, delete, modify, done, undone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "add", "insert", "delete", "modify", "done", "undone"],
                    "description": "The todo operation.",
                },
                "text": {"type": "string", "description": "Task text (for add, insert, modify)."},
                "position": {"type": "integer", "description": "1-based position (for insert, delete, modify, done, undone)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "memory",
        "description": (
            "Manage your persistent memory. Two stores:\n"
            "  - 'memory': your personal notes (environment facts, learned conventions, completed work)\n"
            "  - 'user': user profile (preferences, communication style, name/timezone)\n"
            "Actions: add, replace, remove, read.\n"
            "Save proactively when you learn something worth remembering across sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["add", "replace", "remove", "read"],
                    "description": "Memory operation.",
                },
                "target": {
                    "type": "string",
                    "enum": ["memory", "user"],
                    "description": "Which store to modify.",
                },
                "content": {"type": "string", "description": "New entry content (for add and replace)."},
                "old_text": {"type": "string", "description": "Unique substring identifying the entry to replace or remove."},
            },
            "required": ["action", "target"],
        },
    },
    {
        "name": "github",
        "description": (
            "Back up THIS jarvis project (this exact codebase, not any other folder — "
            "there is no `path` parameter) to the user's own GitHub account. Use this for "
            "'upload to GitHub', 'push to GitHub', 'back this up', 'sync to GitHub', or similar.\n"
            "Actions:\n"
            "  - status : report whether there are uncommitted changes and what the "
            "remote repo URL is (if any) — use this to check before/without pushing.\n"
            "  - upload : commit any pending changes (auto-generated message unless "
            "`message` is given) and push. If no GitHub repo exists yet for this project, "
            "creates one on the user's account first (name defaults to the project folder "
            "name; pass `private=false` for a public repo, defaults to private) and pushes "
            "to it. Requires `gh auth login` to have already been run once by the user — if "
            "not authenticated, this fails with a clear error; tell the user to run that, "
            "don't try to work around it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["status", "upload"],
                    "description": "The GitHub operation.",
                },
                "message": {"type": "string", "description": "Commit message (action=upload). Optional — a timestamped default is used if omitted."},
                "private": {"type": "boolean", "description": "Repo visibility if creating one for the first time (action=upload). Defaults to true (private)."},
            },
            "required": ["action"],
        },
    },
    {
        "name": "recall",
        "description": "Search past conversation history using full-text search (FTS5). Use to recall what was discussed in previous sessions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms."},
                "limit": {"type": "integer", "description": "Max results (default 5)."},
            },
            "required": ["query"],
        },
    },
]


# ── Executors ──────────────────────────────────────────────────────────────────

def run_shell(command: str, timeout: int = 30) -> dict:
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": str(e)}


def run_web_search(query: str, max_results: int = 5) -> dict:
    """Google search via SerpAPI if key is set, otherwise DuckDuckGo."""
    serpapi_key = os.getenv("SERPAPI_KEY") or os.getenv("SERPAPI_API_KEY")
    if serpapi_key:
        try:
            from serpapi import GoogleSearch
            search = GoogleSearch({
                "q": query,
                "num": min(max_results, 10),
                "api_key": serpapi_key,
            })
            data = search.get_dict()
            results = []
            for r in data.get("organic_results", [])[:max_results]:
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("link", ""),
                    "snippet": r.get("snippet", ""),
                })
            return {"results": results, "count": len(results), "engine": "google"}
        except Exception as e:
            # Fall through to DDG
            pass

    # Fallback: DuckDuckGo (ddgs, formerly duckduckgo-search)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=min(max_results, 10)):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", "") or r.get("link", ""),
                    "snippet": r.get("body", ""),
                })
        return {"results": results, "count": len(results), "engine": "duckduckgo"}
    except Exception as e:
        return {"error": str(e)}


def run_read_file(path: str) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            return {"error": f"File not found: {p}"}
        content = p.read_text(encoding="utf-8", errors="replace")
        return {"content": content, "path": str(p), "size": len(content)}
    except Exception as e:
        return {"error": str(e)}


def run_write_file(path: str, content: str, append: bool = False) -> dict:
    try:
        p = Path(path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(p, mode, encoding="utf-8") as f:
            f.write(content)
        return {"success": True, "path": str(p), "bytes": len(content.encode())}
    except Exception as e:
        return {"error": str(e)}


def _load_todos() -> list:
    if not TODO_FILE.exists():
        return []
    try:
        return json.loads(TODO_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_todos(todos: list) -> None:
    TODO_FILE.parent.mkdir(parents=True, exist_ok=True)
    TODO_FILE.write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")


def run_open_browser(url: str) -> dict:
    import subprocess
    try:
        subprocess.Popen(["open", url])
        return {"success": True, "url": url}
    except Exception as e:
        return {"error": str(e)}


def _osascript(script: str) -> dict:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        return {"stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "exit_code": r.returncode}
    except Exception as e:
        return {"error": str(e)}


def _get_volume() -> dict:
    """Return {'volume': int 0-100, 'muted': bool} or {'error': ...}."""
    res = _osascript(
        'set s to (get volume settings)\n'
        'return (output volume of s as text) & "," & (output muted of s as text)'
    )
    if res.get("error") or res.get("exit_code", 1) != 0:
        return {"error": res.get("stderr") or res.get("error") or "osascript failed"}
    try:
        vol_str, muted_str = res["stdout"].split(",")
        return {"volume": int(vol_str.strip()), "muted": muted_str.strip().lower() == "true"}
    except Exception as e:
        return {"error": f"could not parse volume: {res.get('stdout')!r} ({e})"}


def run_volume(action: str, level: int = None, delta: int = None,
               percent: float = None) -> dict:
    current = _get_volume()
    if "error" in current:
        return current

    if action == "get":
        return {"success": True, **current}

    if action == "mute":
        res = _osascript("set volume with output muted")
        out = _get_volume()
        return {"success": "error" not in out, "action": "mute", **out}

    if action == "unmute":
        res = _osascript("set volume without output muted")
        out = _get_volume()
        return {"success": "error" not in out, "action": "unmute", **out}

    if action == "set":
        if level is None:
            return {"error": "level is required for action=set"}
        target = level

    elif action == "adjust":
        if percent is not None:
            target = round(current["volume"] * (1 + percent / 100.0))
        elif delta is not None:
            target = current["volume"] + delta
        else:
            return {"error": "adjust needs either delta or percent"}

    else:
        return {"error": f"Unknown volume action: {action}"}

    target = max(0, min(100, int(target)))
    res = _osascript(f"set volume output volume {target}")
    if res.get("exit_code", 1) != 0:
        return {"error": res.get("stderr") or "failed to set volume"}
    # Setting a non-zero volume also unmutes on macOS; reflect real state.
    if target > 0:
        _osascript("set volume without output muted")
    out = _get_volume()
    return {
        "success": "error" not in out,
        "action": action,
        "previous_volume": current["volume"],
        **out,
    }


# Spotify Web API token cache (Client Credentials flow — no user login needed).
_SPOTIFY_TOKEN: dict = {"value": None, "expires_at": 0.0}


def _spotify_token() -> dict:
    """Fetch/cache an app access token. Returns {'token': str} or {'error': str}."""
    import time

    if _SPOTIFY_TOKEN["value"] and time.time() < _SPOTIFY_TOKEN["expires_at"]:
        return {"token": _SPOTIFY_TOKEN["value"]}

    cid = os.getenv("SPOTIFY_CLIENT_ID")
    secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return {"error": "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET not set in .env "
                         "(create an app at https://developer.spotify.com/dashboard)"}

    import base64
    import urllib.request

    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token",
        data=body,
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Spotify auth failed: {e}"}

    tok = data.get("access_token")
    if not tok:
        return {"error": f"Spotify auth response had no token: {data}"}
    _SPOTIFY_TOKEN["value"] = tok
    _SPOTIFY_TOKEN["expires_at"] = time.time() + data.get("expires_in", 3600) - 60
    return {"token": tok}


def _spotify_search_tracks(query: str, limit: int = 5) -> dict:
    """Return {'tracks': [{name, artist, album, uri}, ...]} or {'error': str}."""
    tok = _spotify_token()
    if "error" in tok:
        return tok

    import urllib.request

    url = "https://api.spotify.com/v1/search?" + urllib.parse.urlencode(
        {"q": query, "type": "track", "limit": limit}
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok['token']}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            data = json.loads(r.read().decode())
    except Exception as e:
        return {"error": f"Spotify search failed: {e}"}

    items = data.get("tracks", {}).get("items", [])
    tracks = [
        {
            "name": it.get("name", ""),
            "artist": ", ".join(a.get("name", "") for a in it.get("artists", [])),
            "album": it.get("album", {}).get("name", ""),
            "uri": it.get("uri", ""),
        }
        for it in items if it.get("uri")
    ]
    if not tracks:
        return {"error": f"No Spotify tracks found for {query!r}"}
    return {"tracks": tracks}


def _spotify_is_running() -> bool:
    """`application "Spotify" is running` is the one AppleScript idiom that
    checks without launching it — anything using `tell application
    "Spotify"` launches it as a side effect if it wasn't already open."""
    res = _osascript('application "Spotify" is running')
    return res.get("stdout", "").strip() == "true"


def _spotify_now_playing() -> dict:
    if not _spotify_is_running():
        return {"track": "", "player_state": "not running"}
    res = _osascript(
        'tell application "Spotify" to return '
        '(name of current track) & " — " & (artist of current track) '
        '& " | " & (player state as text)'
    )
    out = res.get("stdout", "")
    track, _, state = out.partition(" | ")
    return {"track": track, "player_state": state}


def run_spotify(action: str, query: str = None, uri: str = None) -> dict:
    # Aliases so both the model and the REPL fast-path can use natural verbs.
    action = {"stop": "pause", "resume": "play", "unpause": "play", "close": "quit"}.get(action, action)

    if action == "quit":
        if not _spotify_is_running():
            return {"success": True, "action": "quit", "note": "wasn't running"}
        res = _osascript('tell application "Spotify" to quit')
        ok = res.get("exit_code") == 0
        result = {"success": ok, "action": "quit"}
        if not ok:
            result["error"] = res.get("stderr") or res.get("error") or "AppleScript failed"
        return result

    if action == "open":
        try:
            subprocess.run(["open", "-a", "Spotify"], timeout=15)
            return {"success": True, "action": "open"}
        except Exception as e:
            return {"error": str(e)}

    if action == "search":
        if not query:
            return {"error": "query is required for action=search"}
        url = "spotify:search:" + urllib.parse.quote(query)
        try:
            subprocess.run(["open", url], timeout=15)
        except Exception:
            pass
        found = _spotify_search_tracks(query)
        if "error" in found:
            return {"success": True, "action": "search", "query": query,
                    "opened_ui": True, "note": found["error"]}
        return {"success": True, "action": "search", "query": query,
                "opened_ui": True, "results": found["tracks"]}

    if action == "play":
        # Play by name: resolve the query to a real URI first.
        if query and not uri:
            found = _spotify_search_tracks(query)
            if "error" in found:
                # Fall back to opening the search page so the user isn't stuck.
                try:
                    subprocess.run(["open", "spotify:search:" + urllib.parse.quote(query)], timeout=15)
                except Exception:
                    pass
                return {"success": False, "action": "play", "query": query,
                        "error": found["error"],
                        "note": "Could not resolve the song to a track URI; opened Spotify search instead."}
            uri = found["tracks"][0]["uri"]

        if uri:
            res = _osascript(f'tell application "Spotify" to play track "{uri}"')
            ok = res.get("exit_code") == 0
            if not ok:
                return {"success": False, "action": "play", "uri": uri,
                        "error": res.get("stderr") or "AppleScript play failed"}
            import time as _t
            _t.sleep(0.6)
            now = _spotify_now_playing()
            return {"success": now.get("player_state") == "playing",
                    "action": "play", "requested_uri": uri, **now}

        res = _osascript('tell application "Spotify" to play')
        ok = res.get("exit_code") == 0
        now = _spotify_now_playing()
        return {"success": ok and now.get("player_state") == "playing",
                "action": "play (resume)", **now}

    if action in ("pause", "next", "previous", "playpause"):
        script = {
            "pause": 'tell application "Spotify" to pause',
            "playpause": 'tell application "Spotify" to playpause',
            "next": 'tell application "Spotify" to next track',
            "previous": 'tell application "Spotify" to previous track',
        }[action]
        res = _osascript(script)
        res["success"] = res.get("exit_code") == 0
        if res["success"]:
            res.update(_spotify_now_playing())
        return res

    if action == "current":
        return {"success": True, **_spotify_now_playing()}

    return {"error": f"Unknown spotify action: {action}"}


def run_todo(action: str, text: str = None, position: int = None) -> dict:
    todos = _load_todos()

    if action == "list":
        return {"todos": todos, "count": len(todos)}

    elif action == "add":
        if not text:
            return {"error": "text is required for add"}
        todos.append({"text": text, "done": False})
        _save_todos(todos)
        return {"success": True, "added": text, "position": len(todos)}

    elif action == "insert":
        if not text or position is None:
            return {"error": "text and position are required for insert"}
        if position < 1 or position > len(todos) + 1:
            return {"error": f"Position {position} out of range (1–{len(todos)+1})"}
        todos.insert(position - 1, {"text": text, "done": False})
        _save_todos(todos)
        return {"success": True, "inserted": text, "at": position}

    elif action == "delete":
        if position is None:
            return {"error": "position is required for delete"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1–{len(todos)})"}
        removed = todos.pop(position - 1)
        _save_todos(todos)
        return {"success": True, "deleted": removed["text"]}

    elif action == "modify":
        if position is None or not text:
            return {"error": "position and text are required for modify"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1–{len(todos)})"}
        old = todos[position - 1]["text"]
        todos[position - 1]["text"] = text
        _save_todos(todos)
        return {"success": True, "old": old, "new": text}

    elif action in ("done", "undone"):
        if position is None:
            return {"error": f"position is required for {action}"}
        if position < 1 or position > len(todos):
            return {"error": f"Position {position} out of range (1–{len(todos)})"}
        todos[position - 1]["done"] = (action == "done")
        _save_todos(todos)
        return {"success": True, action: todos[position - 1]["text"]}

    return {"error": f"Unknown todo action: {action}"}


def _as_str(s: str) -> str:
    """Escape a string for embedding in an AppleScript string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def run_reminder(action: str, text: str = None, due_date: str = None, list_name: str = None) -> dict:
    list_ref = (
        f'list "{_as_str(list_name)}" of application "Reminders"'
        if list_name else 'default list of application "Reminders"'
    )

    if action == "add":
        if not text:
            return {"error": "text is required for action=add"}
        date_setup = ""
        if due_date:
            try:
                dt = datetime.strptime(due_date, "%Y-%m-%d %H:%M")
            except ValueError:
                return {"error": f"due_date must be 'YYYY-MM-DD HH:MM', got: {due_date!r}"}
            date_setup = (
                "set theDate to current date\n"
                f"set year of theDate to {dt.year}\n"
                f"set month of theDate to {dt.month}\n"
                f"set day of theDate to {dt.day}\n"
                f"set hours of theDate to {dt.hour}\n"
                f"set minutes of theDate to {dt.minute}\n"
                "set seconds of theDate to 0\n"
            )
        script = (
            'tell application "Reminders"\n'
            f'{date_setup}'
            f'  set r to make new reminder in {list_ref} with properties {{name:"{_as_str(text)}"}}\n'
            + ('  set remind me date of r to theDate\n' if due_date else '')
            + 'end tell\n'
        )
        res = _osascript(script)
        if "error" in res or res.get("exit_code") != 0:
            return {"success": False, "error": res.get("stderr") or res.get("error") or "AppleScript failed"}
        return {"success": True, "added": text, "due_date": due_date, "list": list_name or "default"}

    if action == "list":
        script = (
            'tell application "Reminders"\n'
            '  set output to ""\n'
            f'  repeat with r in (reminders of {list_ref} whose completed is false)\n'
            '    set d to due date of r\n'
            '    set dStr to ""\n'
            '    if d is not missing value then set dStr to (d as string)\n'
            '    set output to output & (name of r) & "|||" & dStr & "###"\n'
            '  end repeat\n'
            'end tell\n'
            'return output\n'
        )
        res = _osascript(script)
        if "error" in res or res.get("exit_code") != 0:
            return {"success": False, "error": res.get("stderr") or res.get("error") or "AppleScript failed"}
        items = []
        for chunk in res.get("stdout", "").split("###"):
            if not chunk:
                continue
            name, _, due = chunk.partition("|||")
            items.append({"name": name, "due": due or None})
        return {"success": True, "reminders": items, "count": len(items)}

    if action in ("complete", "delete"):
        if not text:
            return {"error": f"text is required for action={action}"}
        find_script = (
            'tell application "Reminders"\n'
            f'  set matches to (reminders of {list_ref} whose name contains "{_as_str(text)}")\n'
            '  set names to ""\n'
            '  set c to (count of matches)\n'
            '  repeat with r in matches\n'
            '    set names to names & (name of r) & "###"\n'
            '  end repeat\n'
            '  return (c as string) & "|||" & names\n'
            'end tell\n'
        )
        res = _osascript(find_script)
        if "error" in res or res.get("exit_code") != 0:
            return {"success": False, "error": res.get("stderr") or res.get("error") or "AppleScript failed"}
        count_str, _, names_raw = res.get("stdout", "0|||").partition("|||")
        try:
            count = int(count_str)
        except ValueError:
            count = 0
        names = [n for n in names_raw.split("###") if n]
        if count == 0:
            return {"success": False, "error": f"No reminder found matching {text!r}"}
        if count > 1:
            return {"success": False, "error": "Multiple reminders match — be more specific.", "candidates": names}

        verb = "set completed of r to true" if action == "complete" else "delete r"
        act_script = (
            'tell application "Reminders"\n'
            f'  set matches to (reminders of {list_ref} whose name contains "{_as_str(text)}")\n'
            '  set r to item 1 of matches\n'
            f'  {verb}\n'
            'end tell\n'
        )
        res2 = _osascript(act_script)
        if "error" in res2 or res2.get("exit_code") != 0:
            return {"success": False, "error": res2.get("stderr") or res2.get("error") or "AppleScript failed"}
        return {"success": True, action: names[0]}

    return {"error": f"Unknown reminder action: {action}"}


def run_memory(action: str, target: str, content: str = None, old_text: str = None) -> dict:
    if action == "add":
        if not content:
            return {"error": "content is required for add"}
        return memory_add(target, content)
    elif action == "replace":
        if not old_text or not content:
            return {"error": "old_text and content are required for replace"}
        return memory_replace(target, old_text, content)
    elif action == "remove":
        if not old_text:
            return {"error": "old_text is required for remove"}
        return memory_remove(target, old_text)
    elif action == "read":
        return memory_read(target)
    return {"error": f"Unknown memory action: {action}"}


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _git(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=timeout,
    )


def _gh_authenticated() -> bool:
    try:
        return subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True, timeout=15,
        ).returncode == 0
    except FileNotFoundError:
        return False


def run_github(action: str, message: str = None, private: bool = None) -> dict:
    if action == "status":
        status = _git(["status", "--porcelain"])
        remote = _git(["remote", "get-url", "origin"])
        return {
            "success": True,
            "has_uncommitted_changes": bool(status.stdout.strip()),
            "changed_files": len(status.stdout.strip().splitlines()) if status.stdout.strip() else 0,
            "remote_url": remote.stdout.strip() if remote.returncode == 0 else None,
        }

    if action == "upload":
        try:
            subprocess.run(["gh", "--version"], capture_output=True, timeout=5)
        except FileNotFoundError:
            return {"success": False, "error": "GitHub CLI (`gh`) is not installed."}
        if not _gh_authenticated():
            return {"success": False, "error": "Not logged into GitHub — run `gh auth login` first (needs a browser)."}

        status = _git(["status", "--porcelain"])
        had_changes = bool(status.stdout.strip())
        if had_changes:
            add = _git(["add", "-A"])
            if add.returncode != 0:
                return {"success": False, "error": add.stderr or "git add failed"}
            commit_msg = message or f"Update via Bella — {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            commit = _git(["commit", "-m", commit_msg])
            if commit.returncode != 0:
                return {"success": False, "error": commit.stderr or commit.stdout or "git commit failed"}

        remote = _git(["remote", "get-url", "origin"])
        has_remote = remote.returncode == 0

        if not has_remote:
            visibility = "--public" if private is False else "--private"
            create = subprocess.run(
                ["gh", "repo", "create", PROJECT_ROOT.name, visibility,
                 "--source=.", "--remote=origin", "--push"],
                cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60,
            )
            if create.returncode != 0:
                return {"success": False, "error": create.stderr or create.stdout or "gh repo create failed"}
            url = _git(["remote", "get-url", "origin"]).stdout.strip()
            return {"success": True, "action": "created_and_pushed", "repo_url": url, "committed": had_changes}

        push = _git(["push", "origin", "HEAD"], timeout=60)
        if push.returncode != 0:
            return {"success": False, "error": push.stderr or push.stdout or "git push failed"}
        return {"success": True, "action": "pushed", "repo_url": remote.stdout.strip(), "committed": had_changes}

    return {"error": f"Unknown github action: {action}"}


def run_recall(query: str, limit: int = 5) -> dict:
    results = search_history(query, limit=limit)
    return {"results": results, "count": len(results)}


# ── Dispatch ───────────────────────────────────────────────────────────────────

def execute_tool(name: str, inputs: dict) -> str:
    """Execute a tool by name and return a JSON string result."""
    dispatch = {
        "shell":        lambda: run_shell(inputs["command"], inputs.get("timeout", 30)),
        "web_search":   lambda: run_web_search(inputs["query"], inputs.get("max_results", 5)),
        "read_file":    lambda: run_read_file(inputs["path"]),
        "write_file":   lambda: run_write_file(inputs["path"], inputs["content"], inputs.get("append", False)),
        "open_browser": lambda: run_open_browser(inputs["url"]),
        "volume":       lambda: run_volume(inputs["action"], inputs.get("level"), inputs.get("delta"), inputs.get("percent")),
        "spotify":      lambda: run_spotify(inputs["action"], inputs.get("query"), inputs.get("uri")),
        "todo":         lambda: run_todo(inputs["action"], inputs.get("text"), inputs.get("position")),
        "reminder":     lambda: run_reminder(inputs["action"], inputs.get("text"), inputs.get("due_date"), inputs.get("list_name")),
        "memory":       lambda: run_memory(inputs["action"], inputs["target"], inputs.get("content"), inputs.get("old_text")),
        "github":       lambda: run_github(inputs["action"], inputs.get("message"), inputs.get("private")),
        "recall":       lambda: run_recall(inputs["query"], inputs.get("limit", 5)),
    }
    fn = dispatch.get(name)
    if fn is None:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        result = fn()
        return json.dumps(result, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)})
