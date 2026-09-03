# Bella as a hub, your devices as clients

The MacBook runs **one** Bella — the model (Ollama), the shared memory, the
conversation, the routing. Every other device (Windows, iPad, a browser) is a
thin **client**: it connects to the hub, says what it can do (run a shell,
open a browser, show a notification…), and does those things when Bella asks.

```
                    BELLA SERVER  (MacBook)
                   shared BellaRuntime
                  model · memory · routing
                          │  /ws  (WebSocket protocol)
        ┌─────────────────┼──────────────────┐
     server-mac        Gaming PC            iPad
   (this Mac's tools)  (Windows client)   (web / PWA)
```

One brain. One memory. One conversation across devices. Device-local actions
run on **the device you're using** unless you say otherwise
("open VS Code" → here; "…on Windows" → the PC).

---

## 1. Start the hub (on the Mac)

```bash
bella serve            # or:  python server.py
```

```
BELLA SERVER

  Brain        qwen3:8b
  Memory       ready
  WebSocket    ready
  Auth         enabled

  Listening
    LAN        192.168.1.42:8765
    Tailscale  100.x.y.z:8765
    Local      localhost:8765

  Waiting for Bella clients…
```

It needs `JARVIS_TOKEN` in `.env` (it can run shell commands on this Mac
**and** on connected devices — it must not be reachable without a secret):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # → JARVIS_TOKEN=...
```

Set `SERVER_HOST=0.0.0.0` in `.env` for LAN/Tailscale clients (default
`127.0.0.1` is local-only).

### Auto-start on login (macOS)

```bash
pkill -f "server.py"
cp com.jarvis.server.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.jarvis.server.plist
```

Logs → `server.log`. Stop for good: `launchctl unload ~/Library/LaunchAgents/com.jarvis.server.plist`.
First `osascript` under launchd may pop a macOS Automation/Reminders permission
dialog — click Allow once.

---

## 2. Connect a Windows (or Linux, or another Mac) client

On the other machine, copy the repo (or just `clients/` + `protocol/`), then:

```bash
pip install -r clients/requirements.txt          # just: websockets
python client.py --server ws://YOUR-MAC.local:8765 --token <JARVIS_TOKEN>
# or set BELLA_SERVER / BELLA_TOKEN and:  python client.py
```

```
BELLA  desktop client
  device   Gaming PC  (windows-gaming-pc)
  can do   shell, browser, filesystem, notifications, screenshot
  type to talk to Bella · Ctrl-C to quit
```

The Mac hub logs `● Gaming PC connected`. Now type to Bella from the PC — it's
the same conversation as on the Mac. `"open Google"` opens it **on the PC**;
`"run git status"` runs **on the PC**.

Windows never imports macOS code — each capability module branches on the OS.

---

## 3. Connect from an iPad / phone / any browser (PWA)

Open **`http://YOUR-MAC.local:8765/`** (or the LAN / Tailscale address). Enter
the token once. That's the full Bella console — streaming replies, tool
activity, a connected-devices panel on wide screens.

iPad: Safari → Share → **Add to Home Screen** → it installs as an app (offline
shell, standalone window). It registers as a `web` device with `browser`,
`notifications`, `clipboard` — so `"send this link to my iPad"` opens it here.

The old `web/chat.html` still works at `/chat.html` (plain POST, no streaming).

---

## 4. From anywhere (not just home Wi-Fi) — Tailscale

The LAN address only works on the same network. **Tailscale** (free, private
mesh VPN, no port-forwarding) gives every device a stable address:

1. Install Tailscale on the Mac + every client, sign in with the same account.
2. The Mac becomes reachable as `your-mac` (MagicDNS) from any device on the
   tailnet, over cellular, anywhere.
3. Clients: `ws://your-mac:8765` (desktop) / `http://your-mac:8765/` (web).

`bella serve` prints the Tailscale address in its banner when it detects one.
No code changes — pure network setup. **Do not** port-forward to the public
internet; the hub executes shell commands.

---

## Endpoints

| path | auth | purpose |
|------|------|---------|
| `GET /` | – | the web client |
| `GET /health` | – | liveness + `{model, connected_devices, uptime, protocol_version}` (no secrets) |
| `GET /status` | Bearer | model, tool count, device list, active requests |
| `GET /devices` | Bearer | connected device list |
| `POST /message` | Bearer | one-shot turn (backward compatible, no streaming) |
| `WS /ws?token=…` | token in query | the real-time protocol (preferred) |

## Security

- Every connection — WebSocket and HTTP — is authenticated against
  `JARVIS_TOKEN`. `device_id` from an unauthenticated peer is never trusted.
- Event types and tool arguments are validated; oversized frames are rejected.
- The existing permission/safety checks (`ALLOW_UNCONFIRMED_WRITE`,
  `ALLOW_DANGEROUS`, dangerous-command detection) still apply to every tool
  call, wherever it runs.
- Device tool calls have a 30 s timeout; an offline/slow device returns a
  clean error, never a hang.
- Tokens are never logged. The web asset route can't traverse out of `web/`.
- Auth is a single abstraction (`bella/server/auth.py`) ready for per-device
  tokens (`authenticator.register_device_token(...)`).
