"""
tools/platform/macos.py — macOS-native backends: system output volume,
Spotify desktop control, and the Reminders app. All driven through
`osascript` / `open`.

Moved verbatim from the old monolithic tools/executor.py — behaviour is
unchanged. Non-macOS platforms never import this module (see
tools/platform/__init__.py); the corresponding tools register as
unavailable instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
from datetime import datetime


def osascript(script: str) -> dict:
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=15,
        )
        return {"stdout": r.stdout.strip(), "stderr": r.stderr.strip(), "exit_code": r.returncode}
    except Exception as e:
        return {"error": str(e)}


# ── volume ────────────────────────────────────────────────────────────────────

def get_volume() -> dict:
    """Return {'volume': int 0-100, 'muted': bool} or {'error': ...}."""
    res = osascript(
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
    current = get_volume()
    if "error" in current:
        return current

    if action == "get":
        return {"success": True, **current}

    if action == "mute":
        osascript("set volume with output muted")
        out = get_volume()
        return {"success": "error" not in out, "action": "mute", **out}

    if action == "unmute":
        osascript("set volume without output muted")
        out = get_volume()
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
    res = osascript(f"set volume output volume {target}")
    if res.get("exit_code", 1) != 0:
        return {"error": res.get("stderr") or "failed to set volume"}
    # Setting a non-zero volume also unmutes on macOS; reflect real state.
    if target > 0:
        osascript("set volume without output muted")
    out = get_volume()
    return {
        "success": "error" not in out,
        "action": action,
        "previous_volume": current["volume"],
        **out,
    }


# ── Spotify ───────────────────────────────────────────────────────────────────

# Spotify Web API token cache (Client Credentials flow — no user login needed).
_SPOTIFY_TOKEN: dict = {"value": None, "expires_at": 0.0}


def _spotify_token() -> dict:
    """Fetch/cache an app access token. Returns {'token': str} or {'error': str}."""
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
    res = osascript('application "Spotify" is running')
    return res.get("stdout", "").strip() == "true"


def _spotify_now_playing() -> dict:
    if not _spotify_is_running():
        return {"track": "", "player_state": "not running"}
    res = osascript(
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
        res = osascript('tell application "Spotify" to quit')
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
            res = osascript(f'tell application "Spotify" to play track "{uri}"')
            ok = res.get("exit_code") == 0
            if not ok:
                return {"success": False, "action": "play", "uri": uri,
                        "error": res.get("stderr") or "AppleScript play failed"}
            time.sleep(0.6)
            now = _spotify_now_playing()
            return {"success": now.get("player_state") == "playing",
                    "action": "play", "requested_uri": uri, **now}

        res = osascript('tell application "Spotify" to play')
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
        res = osascript(script)
        res["success"] = res.get("exit_code") == 0
        if res["success"]:
            res.update(_spotify_now_playing())
        return res

    if action == "current":
        return {"success": True, **_spotify_now_playing()}

    return {"error": f"Unknown spotify action: {action}"}


# ── Reminders ─────────────────────────────────────────────────────────────────

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
        res = osascript(script)
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
        res = osascript(script)
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
        res = osascript(find_script)
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
        res2 = osascript(act_script)
        if "error" in res2 or res2.get("exit_code") != 0:
            return {"success": False, "error": res2.get("stderr") or res2.get("error") or "AppleScript failed"}
        return {"success": True, action: names[0]}

    return {"error": f"Unknown reminder action: {action}"}


def spotify_app_present() -> bool:
    """Best-effort check that the Spotify app is installed, without launching it."""
    res = osascript(
        'tell application "Finder" to return (exists application file id "com.spotify.client")'
    )
    if res.get("stdout", "").strip() == "true":
        return True
    # Fallback: `open -Ra` succeeds (exit 0) iff the app is registered.
    try:
        return subprocess.run(["open", "-Ra", "Spotify"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False
